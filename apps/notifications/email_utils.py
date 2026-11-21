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
import re
from email.mime.image import MIMEImage
from pathlib import Path

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)

# Context keys that are not worth repeating in the plain-text version.
_INTERNAL_KEYS = ('subject', 'frontend_url', 'date', 'user', 'user_name')

# Icons used in the email templates (<img src="cid:icon-check">). They are PNG files kept
# next to this module and attached to each email, so they show up without any public URL
# and without the recipient having to "load remote images".
ICON_DIR = Path(__file__).resolve().parent / 'email_icons'
_ICON_REF = re.compile(r'cid:icon-([a-z0-9-]+)')


def _attach_inline_icons(msg, html_content):
    """Attach every icon the rendered HTML refers to as an inline image."""
    attached = 0
    for name in sorted(set(_ICON_REF.findall(html_content))):
        path = ICON_DIR / f'{name}.png'
        if not path.is_file():
            logger.warning('Email icon "%s" is used in a template but %s does not exist', name, path)
            continue
        image = MIMEImage(path.read_bytes(), _subtype='png')
        image.add_header('Content-ID', f'<icon-{name}>')
        image.add_header('Content-Disposition', 'inline', filename=f'{name}.png')
        msg.attach(image)
        attached += 1
    if attached:
        msg.mixed_subtype = 'related'  # images belong to the HTML part, not "attachments"


# Email providers that are called over HTTPS (Brevo, Resend, ... through django-anymail) instead of SMTP.
# Their APIs cannot embed images (Brevo does not support it at all), so the icons are loaded from a public
# address instead: EMAIL_ICON_BASE_URL, by default FRONTEND_URL + /email-icons.
_ICON_TAG = re.compile(r'<img\b[^>]*\bsrc="cid:icon-[a-z0-9-]+"[^>]*>', re.I)
_LOCAL_URL = re.compile(r'^https?://(localhost|127\.|0\.0\.0\.0|\[::1\])', re.I)


def _uses_http_api():
    return str(getattr(settings, 'EMAIL_BACKEND', '')).startswith('anymail.')


def _api_provider():
    """'brevo' for anymail.backends.brevo.EmailBackend; '' when mail goes out over SMTP."""
    parts = str(getattr(settings, 'EMAIL_BACKEND', '')).split('.')
    return parts[-2] if _uses_http_api() and len(parts) >= 2 else ''


def icon_base_url():
    """Public address of the icon files, or '' when there is none (icons are then left out)."""
    configured = (getattr(settings, 'EMAIL_ICON_BASE_URL', '') or '').strip().rstrip('/')
    if configured.lower() == 'none':
        return ''
    base = configured or f"{str(settings.FRONTEND_URL).rstrip('/')}/email-icons"
    return '' if _LOCAL_URL.match(base) else base


def _icons_as_links(html_content):
    """Point the icons at public https addresses. With no public address, leave the pictures out
    instead of sending broken images."""
    base = icon_base_url()
    if not base:
        return _ICON_TAG.sub('', html_content)
    return _ICON_REF.sub(lambda m: f'{base}/{m.group(1)}.png', html_content)


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
                status='failed',  # flipped to 'sent' once the provider accepts the message
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
        over_api = _uses_http_api()
        if over_api:
            html_content = _icons_as_links(html_content)
        msg.attach_alternative(html_content, 'text/html')
        if not over_api:
            _attach_inline_icons(msg, html_content)

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
_API_HOSTS = {
    'brevo': 'api.brevo.com', 'resend': 'api.resend.com', 'sendgrid': 'api.sendgrid.com',
    'mailgun': 'api.mailgun.net', 'postmark': 'api.postmarkapp.com', 'mailjet': 'api.mailjet.com',
}
_API_KEY_VARIABLES = {
    'brevo': 'BREVO_API_KEY', 'resend': 'RESEND_API_KEY', 'sendgrid': 'SENDGRID_API_KEY',
    'mailgun': 'MAILGUN_API_KEY', 'postmark': 'POSTMARK_SERVER_TOKEN', 'mailjet': 'MAILJET_API_KEY',
}
_FREE_MAILBOX_DOMAINS = ('gmail.com', 'googlemail.com', 'yahoo.com', 'outlook.com', 'hotmail.com', 'icloud.com')


def _api_key_is_set(provider):
    credentials = getattr(settings, 'ANYMAIL', {}) or {}
    return any(
        str(value).strip()
        for name, value in credentials.items()
        if name.startswith(provider.upper() + '_') and name.endswith(('API_KEY', 'SERVER_TOKEN', 'API_TOKEN', 'SECRET_KEY'))
    )


def email_config_report():
    """Describe the email setup (never includes a password or API key) and flag likely problems."""
    backend = settings.EMAIL_BACKEND
    from_email = settings.DEFAULT_FROM_EMAIL
    admin_emails = get_admin_emails()
    warnings = []
    provider = _api_provider()
    over_api = bool(provider)

    if over_api:
        key_is_set = _api_key_is_set(provider)
        host, port, use_tls, use_ssl = _API_HOSTS.get(provider, provider), 443, True, False
        login = f'{provider.capitalize()} API'
        timeout = (getattr(settings, 'ANYMAIL', {}) or {}).get('REQUESTS_TIMEOUT')
        if not key_is_set:
            variable = _API_KEY_VARIABLES.get(provider, 'the provider API key')
            warnings.append(f'{provider.capitalize()} API key is missing - the provider will reject every email. Set {variable}.')
        from_domain = from_email.rsplit('@', 1)[-1].strip(' >').lower()
        if provider == 'resend' and from_domain.endswith('resend.dev'):
            warnings.append(
                "The From address uses Resend's test domain (resend.dev), which can only deliver to your own account's "
                'email. Verify your domain in Resend and set DEFAULT_FROM_EMAIL to an address on it.'
            )
        if from_domain in _FREE_MAILBOX_DOMAINS:
            warnings.append(
                f'DEFAULT_FROM_EMAIL uses {from_domain}. Mail sent on behalf of a free mailbox through a sending '
                'service often fails sender checks and is rejected or lands in spam. Use an address on a domain you own '
                f'and have verified in {provider.capitalize()}.'
            )
        if not settings.DEBUG and not icon_base_url() and (getattr(settings, 'EMAIL_ICON_BASE_URL', '') or '').strip().lower() != 'none':
            warnings.append(
                'FRONTEND_URL is not a public address, so the small icons are left out of emails. Set EMAIL_ICON_BASE_URL '
                'to where the icons are hosted (or "none" to send emails without icons).'
            )
        smtp_user, password_set = login, key_is_set
    else:
        host = getattr(settings, 'EMAIL_HOST', '')
        port = getattr(settings, 'EMAIL_PORT', None)
        use_tls = getattr(settings, 'EMAIL_USE_TLS', None)
        use_ssl = getattr(settings, 'EMAIL_USE_SSL', False)
        timeout = getattr(settings, 'EMAIL_TIMEOUT', None)
        smtp_user = getattr(settings, 'EMAIL_HOST_USER', '')
        password_set = bool(getattr(settings, 'EMAIL_HOST_PASSWORD', ''))

        if not backend.endswith('smtp.EmailBackend'):
            warnings.append(
                f'EMAIL_BACKEND is "{backend}": emails are NOT delivered to real inboxes. '
                'Use django.core.mail.backends.smtp.EmailBackend (or an EMAIL_PROVIDER such as brevo or resend) in production.'
            )
        else:
            if not smtp_user or not password_set:
                warnings.append('EMAIL_HOST_USER / EMAIL_HOST_PASSWORD is empty - the SMTP server will reject every email.')
            if 'gmail' in host and smtp_user and smtp_user.lower() not in from_email.lower():
                warnings.append(
                    f'Gmail overwrites the From address with {smtp_user} unless "{from_email}" is a verified '
                    '"Send mail as" alias. Mail can also land in spam. Consider a transactional provider '
                    '(set EMAIL_PROVIDER to brevo, resend, ...) with a verified domain.'
                )
            if 'gmail' in host and port == 587 and not use_tls:
                warnings.append('Gmail on port 587 needs EMAIL_USE_TLS=True.')
            if use_ssl and use_tls:
                warnings.append('EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be True.')

    if not admin_emails:
        warnings.append('No admin recipients: set ADMIN_EMAIL or mark a user as staff, otherwise admin alerts go nowhere.')
    if 'localhost' in settings.FRONTEND_URL and not settings.DEBUG:
        warnings.append('FRONTEND_URL points at localhost, so links in emails will not work for users.')

    return {
        'backend': backend,
        'provider': provider or 'smtp',
        'transport': 'api' if over_api else 'smtp',
        'host': host,
        'port': port,
        'use_tls': use_tls,
        'use_ssl': use_ssl,
        'timeout_seconds': timeout,
        'smtp_user': smtp_user,
        'password_set': password_set,
        'from_email': from_email,
        'admin_recipients': admin_emails,
        'frontend_url': settings.FRONTEND_URL,
        'warnings': warnings,
    }


def explain_smtp_error(error_text):
    """Turn a raw email error (SMTP or provider API) into a hint a non-developer can act on."""
    text = (error_text or '').lower()

    # ---- errors from an HTTPS provider (Brevo, Resend, ...), raised by django-anymail
    if 'anymail' in text or 'api response' in text or _uses_http_api():
        if any(w in text for w in ('sender', 'domain', 'verif', 'from address', 'testing emails', 'not allowed to send')):
            return ('The provider does not accept the From address. In the provider dashboard verify the sender or domain, '
                    'then set DEFAULT_FROM_EMAIL to exactly that address. (Resend test mode only sends to your own account email.)')
        if any(w in text for w in ('401', 'unauthorized', 'api key', 'api-key', 'key not found', 'invalid token', 'must set', '_api_key', 'server_token')):
            return ('The provider rejected the API key (or none is set). Check the key variable (BREVO_API_KEY, RESEND_API_KEY, ...) '
                    'for typos and extra spaces. For Brevo use an API key (v3), not the SMTP key.')
        if any(w in text for w in ('429', 'rate limit', 'rate_limit', 'too many', 'quota', 'daily limit', 'limit reached')):
            return "You have reached the provider's sending limit. Wait for it to reset or upgrade the provider plan."
        if any(w in text for w in ('timed out', 'timeout', 'connection', 'name or service not known', 'getaddrinfo')):
            return 'Could not reach the provider. Check the internet connection of the server and that the provider is not down.'
        return 'The email provider refused the message. The exact reason is in the error text above.'

    # ---- SMTP errors
    if '535' in text or 'username and password not accepted' in text or 'badcredentials' in text:
        return ('The SMTP server rejected the login. For Gmail you must use a 16-character App Password '
                '(Google Account > Security > 2-Step Verification > App passwords), not your normal password.')
    if '534' in text or 'application-specific password' in text:
        return 'Gmail requires an App Password because 2-Step Verification is on.'
    if 'timed out' in text or 'timeout' in text:
        return ('The SMTP server did not answer in time. Check EMAIL_HOST / EMAIL_PORT. Some hosts (for example Render\'s free plan) '
                'block SMTP: use an HTTPS provider instead (EMAIL_PROVIDER=brevo or resend).')
    if 'connection refused' in text or 'name or service not known' in text or 'getaddrinfo' in text:
        return ('Could not reach the SMTP server. Check EMAIL_HOST, EMAIL_PORT and the firewall, or use an HTTPS provider '
                '(EMAIL_PROVIDER=brevo or resend).')
    if 'starttls' in text or 'ssl' in text:
        return 'TLS/SSL mismatch. Gmail: port 587 with EMAIL_USE_TLS=True, or port 465 with EMAIL_USE_SSL=True.'
    if '550' in text or '553' in text or '554' in text:
        return 'The server refused the sender or recipient address. Check DEFAULT_FROM_EMAIL and the recipient.'
    return ''
