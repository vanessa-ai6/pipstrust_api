"""Email helpers.

Every email goes through ``send_html_email``. Unlike the previous version it:

* never swallows errors: each attempt is stored in ``EmailLog`` (sent / failed + the
  real error text) and written to the server log;
* reports template problems instead of silently falling back to plain text;
* can be told to raise (``raise_on_error=True``) for emails the user cannot do without,
  such as one-time codes;
* sends one message per recipient so admins do not see each other's addresses.

A failure to *write the log* can never break the business flow that triggered the email.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

# Context keys that are not worth repeating in the plain-text version.
_INTERNAL_KEYS = ('subject', 'frontend_url', 'date', 'user', 'user_name')


# ─── Recipients ───────────────────────────────────────────────────────────────
def get_admin_emails():
    """Addresses that should receive admin alerts.

    ADMIN_EMAIL (may hold several comma-separated addresses) plus every active staff
    user, so alerts still arrive if ADMIN_EMAIL is not a real mailbox.
    """
    from apps.users.models import User

    found = []
    for address in str(getattr(settings, 'ADMIN_EMAIL', '') or '').split(','):
        address = address.strip()
        if address:
            found.append(address)
    try:
        found.extend(
            User.objects.filter(is_staff=True, is_active=True)
            .exclude(email='').values_list('email', flat=True)
        )
    except Exception:  # pragma: no cover - DB unavailable
        logger.exception('Could not load staff emails')
    return _unique(found)


def _unique(addresses):
    seen, result = set(), []
    for address in addresses or []:
        key = (address or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(address.strip())
    return result


# ─── Logging helpers ──────────────────────────────────────────────────────────
def _loggable_context(context):
    """JSON-safe copy of the context (no model instances)."""
    clean = {}
    for key, value in (context or {}).items():
        if key == 'user':
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
        else:
            clean[key] = str(value)
    return clean


def _create_log(subject, template_name, context, recipient):
    from .models import EmailLog

    user = context.get('user')
    try:
        from apps.users.models import User

        if not isinstance(user, User):
            user = User.objects.filter(email__iexact=recipient).first()
        sensitive = template_name in EmailLog.NON_RETRYABLE_TEMPLATES
        # Savepoint, so a missing table (migration not applied yet) cannot poison
        # an enclosing transaction.
        with transaction.atomic():
            return EmailLog.objects.create(
                user=user,
                recipient=recipient,
                subject=subject[:255],
                template=template_name,
                status='failed',  # flipped to 'sent' once the SMTP call succeeds
                context={} if sensitive else _loggable_context(context),
            )
    except Exception:
        logger.exception('Could not create EmailLog for %s', recipient)
        return None


def _save_log(log, status, error=''):
    if log is None:
        return
    try:
        log.status = status
        log.error = (error or '')[:4000]
        with transaction.atomic():
            log.save()
    except Exception:
        logger.exception('Could not update EmailLog %s', getattr(log, 'pk', None))


# ─── Sending ──────────────────────────────────────────────────────────────────
def _plain_text(context, text_body=None):
    if text_body:
        return text_body
    lines = [f"Hello {context.get('user_name') or 'there'},", '']
    if context.get('message'):
        lines.append(str(context['message']))
    else:
        for key, value in context.items():
            if key not in _INTERNAL_KEYS:
                lines.append(f"{key.replace('_', ' ').title()}: {value}")
    lines += ['', 'PipsTrust Team', 'support@pipstrust.com']
    return '\n'.join(lines)


def _send_one(subject, template_name, context, recipient, log, text_body=None):
    """Render and send one message. Updates ``log``. Raises on failure."""
    warning = ''
    html_content = None
    try:
        html_content = render_to_string(f'emails/{template_name}.html', context)
    except Exception as exc:  # template missing / bad variable
        warning = f'Template "{template_name}" failed to render ({type(exc).__name__}: {exc}); sent plain text.'
        logger.warning(warning)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=_plain_text(context, text_body),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
    )
    if html_content:
        msg.attach_alternative(html_content, 'text/html')

    try:
        msg.send(fail_silently=False)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        logger.error('Email "%s" to %s FAILED: %s', subject, recipient, error)
        _save_log(log, 'failed', error)
        raise
    _save_log(log, 'sent', warning)
    logger.info('Email "%s" sent to %s', subject, recipient)


def send_html_email(subject, template_name, context, recipient_list,
                    raise_on_error=False, text_body=None):
    """Send a styled HTML email. Returns True only if every recipient succeeded."""
    context = dict(context or {})
    context.setdefault('subject', subject)
    context.setdefault('frontend_url', settings.FRONTEND_URL)
    context.setdefault('date', timezone.now().strftime('%B %d, %Y %I:%M %p UTC'))

    recipients = _unique(recipient_list)
    if not recipients:
        logger.warning('Email "%s" has no recipients - nothing sent', subject)
        return False

    all_ok = True
    for recipient in recipients:
        log = _create_log(subject, template_name, context, recipient)
        try:
            _send_one(subject, template_name, context, recipient, log, text_body)
        except Exception:
            all_ok = False
            if raise_on_error:
                raise
    return all_ok


def send_email_after_commit(*args, **kwargs):
    """Like send_html_email, but waits for the surrounding DB transaction to commit.

    Use inside ``transaction.atomic()``: the email is only sent if the money movement
    really happened, and a slow SMTP server cannot hold database row locks.
    """
    transaction.on_commit(lambda: send_html_email(*args, **kwargs))


def notify_admins(subject, message):
    """Email every admin recipient. Returns True if all were delivered."""
    recipients = get_admin_emails()
    if not recipients:
        logger.warning('Admin alert "%s" not sent: no admin recipients configured', subject)
        return False
    return send_html_email(
        subject=subject,
        template_name='broadcast',
        context={'user_name': 'Admin', 'message': message},
        recipient_list=recipients,
    )


def notify_admins_after_commit(subject, message):
    transaction.on_commit(lambda: notify_admins(subject, message))


def retry_email(log):
    """Re-send a failed EmailLog row. Returns True on success."""
    if not log.retryable:
        raise ValueError('This email cannot be re-sent (it already succeeded or contains a one-time code).')
    context = dict(log.context or {})
    if log.user_id:
        context['user'] = log.user
        context.setdefault('user_name', log.user.full_name)
    context.setdefault('frontend_url', settings.FRONTEND_URL)
    log.attempts += 1
    try:
        _send_one(log.subject, log.template, context, log.recipient, log)
    except Exception:
        return False
    return True


# ─── Diagnostics ──────────────────────────────────────────────────────────────
def email_config_report():
    """Describe the email setup (never includes the password) and flag likely problems."""
    backend = settings.EMAIL_BACKEND
    host = getattr(settings, 'EMAIL_HOST', '')
    from_email = settings.DEFAULT_FROM_EMAIL
    smtp_user = getattr(settings, 'EMAIL_HOST_USER', '')
    admin_emails = get_admin_emails()
    warnings = []

    if not backend.endswith('smtp.EmailBackend'):
        warnings.append(
            f'EMAIL_BACKEND is "{backend}": emails are NOT delivered to real inboxes. '
            'Use django.core.mail.backends.smtp.EmailBackend in production.'
        )
    else:
        if not smtp_user or not getattr(settings, 'EMAIL_HOST_PASSWORD', ''):
            warnings.append('EMAIL_HOST_USER / EMAIL_HOST_PASSWORD is empty - the SMTP server will reject every email.')
        if 'gmail' in host and smtp_user and smtp_user.lower() not in from_email.lower():
            warnings.append(
                f'Gmail overwrites the From address with {smtp_user} unless "{from_email}" is a verified '
                '"Send mail as" alias. Mail can also land in spam. Consider a transactional provider '
                '(Brevo, Mailgun, SES, Resend) with a verified domain.'
            )
        if 'gmail' in host and settings.EMAIL_PORT == 587 and not settings.EMAIL_USE_TLS:
            warnings.append('Gmail on port 587 needs EMAIL_USE_TLS=True.')
        if getattr(settings, 'EMAIL_USE_SSL', False) and settings.EMAIL_USE_TLS:
            warnings.append('EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be True.')
    if not admin_emails:
        warnings.append('No admin recipients: set ADMIN_EMAIL or mark a user as staff, otherwise admin alerts go nowhere.')
    if 'localhost' in settings.FRONTEND_URL and not settings.DEBUG:
        warnings.append('FRONTEND_URL points at localhost, so links in emails will not work for users.')

    return {
        'backend': backend,
        'host': host,
        'port': getattr(settings, 'EMAIL_PORT', None),
        'use_tls': getattr(settings, 'EMAIL_USE_TLS', None),
        'use_ssl': getattr(settings, 'EMAIL_USE_SSL', False),
        'timeout_seconds': getattr(settings, 'EMAIL_TIMEOUT', None),
        'smtp_user': smtp_user,
        'password_set': bool(getattr(settings, 'EMAIL_HOST_PASSWORD', '')),
        'from_email': from_email,
        'admin_recipients': admin_emails,
        'frontend_url': settings.FRONTEND_URL,
        'warnings': warnings,
    }


def explain_smtp_error(error_text):
    """Turn a raw SMTP error into a hint a non-developer can act on."""
    text = (error_text or '').lower()
    if '535' in text or 'username and password not accepted' in text or 'badcredentials' in text:
        return ('The SMTP server rejected the login. For Gmail you must use a 16-character App Password '
                '(Google Account > Security > 2-Step Verification > App passwords), not your normal password.')
    if '534' in text or 'application-specific password' in text:
        return 'Gmail requires an App Password because 2-Step Verification is on.'
    if 'timed out' in text or 'timeout' in text:
        return 'The SMTP server did not answer in time. Check EMAIL_HOST / EMAIL_PORT and that the host allows outbound SMTP.'
    if 'connection refused' in text or 'name or service not known' in text or 'getaddrinfo' in text:
        return 'Could not reach the SMTP server. Check EMAIL_HOST, EMAIL_PORT and your internet/firewall.'
    if 'starttls' in text or 'ssl' in text:
        return 'TLS/SSL mismatch. Gmail: port 587 with EMAIL_USE_TLS=True, or port 465 with EMAIL_USE_SSL=True.'
    if '550' in text or '553' in text or '554' in text:
        return 'The server refused the sender or recipient address. Check DEFAULT_FROM_EMAIL and the recipient.'
    return ''
