"""Sending email through a provider's HTTPS API (Brevo, Resend, ...) instead of SMTP.

A small fake provider server runs on this machine, so the tests send real HTTP requests
through the same code path the site uses (send_html_email -> EmailLog) without needing accounts.
"""
import json
import re
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from pipstrust.email_provider import load_email_settings

from .email_utils import (
    email_config_report, explain_smtp_error, icon_base_url, retry_email, send_html_email,
)
from .models import EmailLog


# ─── a fake provider ──────────────────────────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.requests.append({
            'path': self.path,
            'headers': {k.lower(): v for k, v in self.headers.items()},
            'json': json.loads(body or b'{}'),
        })
        status, payload = self.server.replies.pop(0) if self.server.replies else self.server.default
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class FakeProvider:
    def __init__(self, ok_reply):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.requests, self.server.replies, self.server.default = [], [], ok_reply
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    @property
    def requests(self):
        return self.server.requests

    def reply_next(self, status, payload):
        self.server.replies.append((status, payload))

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeUser:
    full_name = 'Vanessa Test'
    email = 'vanessa@example.com'


CONTEXT = {'user': FakeUser(), 'user_name': 'Vanessa Test', 'amount': Decimal('150.00'),
           'payment_method': 'Bitcoin', 'new_balance': Decimal('215.00')}
FROM = 'PipsTrust <noreply@pipstrust.com>'


def send(subject='Deposit Confirmed - PipsTrust', template='deposit_confirmed', **kwargs):
    return send_html_email(subject, template, dict(CONTEXT), ['vanessa@example.com'], **kwargs)


# ─── choosing the provider from environment variables ─────────────────────────
class ProviderSwitchTests(SimpleTestCase):
    def test_default_is_plain_smtp_and_changes_nothing(self):
        self.assertEqual(load_email_settings({}), {})
        self.assertEqual(load_email_settings({'EMAIL_PROVIDER': 'smtp', 'EMAIL_HOST_USER': 'me@gmail.com'}), {})

    def test_brevo_and_resend_pick_their_backend_and_key(self):
        brevo = load_email_settings({'EMAIL_PROVIDER': 'Brevo', 'BREVO_API_KEY': '  xkeysib-1  '})
        self.assertEqual(brevo['EMAIL_BACKEND'], 'anymail.backends.brevo.EmailBackend')
        self.assertEqual(brevo['ANYMAIL']['BREVO_API_KEY'], 'xkeysib-1')   # spaces from copy/paste are removed
        resend = load_email_settings({'EMAIL_PROVIDER': 'resend', 'RESEND_API_KEY': 're_123'})
        self.assertEqual(resend['EMAIL_BACKEND'], 'anymail.backends.resend.EmailBackend')
        self.assertEqual(resend['ANYMAIL']['RESEND_API_KEY'], 're_123')

    def test_mailgun_needs_its_sending_domain_too(self):
        cfg = load_email_settings({'EMAIL_PROVIDER': 'mailgun', 'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.com'})
        self.assertEqual(cfg['ANYMAIL'], {'MAILGUN_API_KEY': 'k', 'MAILGUN_SENDER_DOMAIN': 'mg.example.com', 'REQUESTS_TIMEOUT': 15})

    def test_a_request_never_waits_longer_than_the_timeout(self):
        self.assertEqual(load_email_settings({'EMAIL_PROVIDER': 'brevo'})['ANYMAIL']['REQUESTS_TIMEOUT'], 15)

    def test_any_other_anymail_provider_can_be_used_without_code_changes(self):
        cfg = load_email_settings({
            'EMAIL_BACKEND': 'anymail.backends.mailjet.EmailBackend',
            'ANYMAIL_MAILJET_API_KEY': 'public', 'ANYMAIL_MAILJET_SECRET_KEY': 'secret',
        })
        self.assertEqual(cfg['EMAIL_BACKEND'], 'anymail.backends.mailjet.EmailBackend')
        self.assertEqual(cfg['ANYMAIL']['MAILJET_API_KEY'], 'public')
        self.assertEqual(cfg['ANYMAIL']['MAILJET_SECRET_KEY'], 'secret')

    def test_a_typo_in_the_provider_name_fails_loudly_instead_of_silently_using_smtp(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'sendgrd'):
            load_email_settings({'EMAIL_PROVIDER': 'sendgrd'})


# ─── where the icons come from ────────────────────────────────────────────────
@override_settings(FRONTEND_URL='https://app.example.com')
class IconAddressTests(SimpleTestCase):
    def test_default_is_the_website_folder(self):
        self.assertEqual(icon_base_url(), 'https://app.example.com/email-icons')

    @override_settings(FRONTEND_URL='https://app.example.com/')
    def test_trailing_slash_is_ignored(self):
        self.assertEqual(icon_base_url(), 'https://app.example.com/email-icons')

    @override_settings(EMAIL_ICON_BASE_URL='https://cdn.example.com/i/')
    def test_can_be_hosted_anywhere_else(self):
        self.assertEqual(icon_base_url(), 'https://cdn.example.com/i')

    @override_settings(EMAIL_ICON_BASE_URL='none')
    def test_can_be_switched_off(self):
        self.assertEqual(icon_base_url(), '')

    @override_settings(FRONTEND_URL='http://localhost:3000')
    def test_an_address_nobody_else_can_open_is_not_used(self):
        self.assertEqual(icon_base_url(), '')


# ─── sending through the APIs ─────────────────────────────────────────────────
class ProviderTestsMixin:
    """Everything that must hold for any provider; run by one concrete class per provider below."""
    provider = None          # 'brevo' | 'resend'
    key_name = None
    ok_reply = None
    url_suffix = ''

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeProvider(cls.ok_reply)

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()
        super().tearDownClass()

    def setUp(self):
        self.fake.requests.clear()
        self.fake.server.replies.clear()
        self.override = override_settings(
            EMAIL_BACKEND=f'anymail.backends.{self.provider}.EmailBackend',
            DEFAULT_FROM_EMAIL=FROM, FRONTEND_URL='https://app.example.com', EMAIL_ICON_BASE_URL='',
            ANYMAIL={f'{self.key_name}': 'test-key-123', f'{self.provider.upper()}_API_URL': self.fake.url + self.url_suffix,
                     'REQUESTS_TIMEOUT': 5},
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

    def sent_html(self):
        payload = self.fake.requests[-1]['json']
        return payload.get('htmlContent') or payload.get('html')

    # -- behaviour shared by every provider
    def test_the_email_is_delivered_over_https_and_logged(self):
        self.assertTrue(send())
        self.assertEqual(len(self.fake.requests), 1)
        log = EmailLog.objects.get()
        self.assertEqual((log.status, log.recipient, log.template, log.error), ('sent', 'vanessa@example.com', 'deposit_confirmed', ''))
        self.assertEqual(mail.outbox, [])   # nothing went out over SMTP

    def test_icons_are_public_links_not_embedded_images(self):
        send()
        html = self.sent_html()
        self.assertNotIn('cid:', html)
        self.assertIn('src="https://app.example.com/email-icons/check.png"', html)
        self.assertIn('src="https://app.example.com/email-icons/alert.png"', html)
        payload = self.fake.requests[-1]['json']
        self.assertFalse({'attachment', 'attachments'} & set(payload), 'icons must not travel as attachments')

    def test_icons_move_to_another_address_when_configured(self):
        with override_settings(EMAIL_ICON_BASE_URL='https://cdn.example.com/i'):
            send()
        self.assertIn('src="https://cdn.example.com/i/check.png"', self.sent_html())

    def test_without_a_public_address_the_icons_are_left_out_cleanly(self):
        with override_settings(FRONTEND_URL='http://localhost:3000'):
            send()
        html = self.sent_html()
        self.assertNotIn('cid:', html)
        self.assertNotIn('email-icons', html)
        self.assertNotIn('<img', html)
        self.assertIn('Deposit Confirmed', html)   # the email itself is intact

    def test_wrong_key_is_logged_with_the_providers_own_words_and_a_hint(self):
        self.fake.reply_next(401, {'message': 'Key not found', 'code': 'unauthorized'})
        self.assertFalse(send())
        log = EmailLog.objects.get()
        self.assertEqual(log.status, 'failed')
        self.assertIn('401', log.error)
        self.assertIn('Key not found', log.error)
        self.assertIn('API key', explain_smtp_error(log.error))

    def test_a_failed_email_can_be_retried_once_the_problem_is_fixed(self):
        self.fake.reply_next(401, {'message': 'Key not found', 'code': 'unauthorized'})
        send()
        log = EmailLog.objects.get()
        self.assertTrue(log.retryable)
        self.assertTrue(retry_email(log))          # the fake provider now answers normally
        log.refresh_from_db()
        self.assertEqual((log.status, log.attempts), ('sent', 2))
        self.assertIn('check.png', self.sent_html())

    def test_one_time_code_emails_raise_when_the_provider_fails(self):
        self.fake.reply_next(500, {'message': 'oops'})
        with self.assertRaises(Exception):
            send('Verify Your Email', 'broadcast', raise_on_error=True)
        self.assertEqual(EmailLog.objects.get().status, 'failed')

    def test_provider_down_is_reported_not_swallowed(self):
        with override_settings(ANYMAIL={self.key_name: 'k', f'{self.provider.upper()}_API_URL': 'http://127.0.0.1:9/', 'REQUESTS_TIMEOUT': 3}):
            self.assertFalse(send())
        log = EmailLog.objects.get()
        self.assertEqual(log.status, 'failed')
        self.assertIn('Could not reach the provider', explain_smtp_error(log.error))

    def test_missing_api_key_is_reported_with_a_hint(self):
        with override_settings(ANYMAIL={}):
            self.assertFalse(send())
        log = EmailLog.objects.get()
        self.assertEqual(log.status, 'failed')
        self.assertIn('API key', explain_smtp_error(log.error))

    def test_the_admin_report_describes_the_provider(self):
        report = email_config_report()
        self.assertEqual((report['provider'], report['transport'], report['port']), (self.provider, 'api', 443))
        self.assertTrue(report['password_set'])
        self.assertNotIn('test-key-123', json.dumps(report))        # the key is never shown
        with override_settings(ANYMAIL={}):
            missing = email_config_report()
        self.assertFalse(missing['password_set'])
        self.assertTrue(any('API key is missing' in w for w in missing['warnings']))


class BrevoTests(ProviderTestsMixin, TestCase):
    provider, key_name, url_suffix = 'brevo', 'BREVO_API_KEY', '/v3/'
    ok_reply = (201, {'messageId': '<202610101200.1@smtp-relay.example>'})

    def test_request_matches_brevos_api(self):
        send()
        request = self.fake.requests[-1]
        payload = request['json']
        self.assertEqual(request['path'], '/v3/smtp/email')
        self.assertEqual(request['headers']['api-key'], 'test-key-123')
        self.assertEqual(payload['sender'], {'email': 'noreply@pipstrust.com', 'name': 'PipsTrust'})
        self.assertEqual(payload['to'], [{'email': 'vanessa@example.com'}])
        self.assertEqual(payload['subject'], 'Deposit Confirmed - PipsTrust')
        self.assertIn('Hello Vanessa Test', payload['textContent'])
        self.assertIn('Deposit Confirmed', payload['htmlContent'])


class ResendTests(ProviderTestsMixin, TestCase):
    provider, key_name, url_suffix = 'resend', 'RESEND_API_KEY', '/'
    ok_reply = (200, {'id': '49a3999c-0ce1-4ea6-ab68-afcd6dc2e794'})

    def test_request_matches_resends_api(self):
        send()
        request = self.fake.requests[-1]
        payload = request['json']
        self.assertEqual(request['path'], '/emails')
        self.assertEqual(request['headers']['authorization'], 'Bearer test-key-123')
        self.assertEqual(payload['from'], FROM)
        self.assertEqual(payload['to'], ['vanessa@example.com'])
        self.assertEqual(payload['subject'], 'Deposit Confirmed - PipsTrust')
        self.assertIn('Hello Vanessa Test', payload['text'])
        self.assertIn('Deposit Confirmed', payload['html'])

    def test_unverified_domain_gets_a_clear_hint(self):
        self.fake.reply_next(403, {'statusCode': 403, 'name': 'validation_error',
                                   'message': 'The pipstrust.com domain is not verified. Please, add and verify your domain on https://resend.com/domains'})
        send()
        hint = explain_smtp_error(EmailLog.objects.get().error)
        self.assertIn('verify the sender or domain', hint)

    def test_rate_limit_gets_a_clear_hint(self):
        self.fake.reply_next(429, {'statusCode': 429, 'name': 'rate_limit_exceeded', 'message': 'Too many requests'})
        send()
        self.assertIn('sending limit', explain_smtp_error(EmailLog.objects.get().error))


# ─── checks that apply whatever the provider is ───────────────────────────────
@override_settings(DEFAULT_FROM_EMAIL=FROM, FRONTEND_URL='https://app.example.com', ADMIN_EMAIL='boss@example.com', DEBUG=False)
class DiagnosticsTests(TestCase):
    def test_resend_test_sender_and_free_mailbox_senders_are_flagged(self):
        creds = {'RESEND_API_KEY': 'k'}
        with override_settings(EMAIL_BACKEND='anymail.backends.resend.EmailBackend', ANYMAIL=creds,
                               DEFAULT_FROM_EMAIL='PipsTrust <onboarding@resend.dev>'):
            self.assertTrue(any('test domain' in w for w in email_config_report()['warnings']))
        with override_settings(EMAIL_BACKEND='anymail.backends.brevo.EmailBackend', ANYMAIL={'BREVO_API_KEY': 'k'},
                               DEFAULT_FROM_EMAIL='PipsTrust <someone@gmail.com>'):
            self.assertTrue(any('gmail.com' in w for w in email_config_report()['warnings']))

    def test_a_correct_api_setup_has_no_warnings(self):
        with override_settings(EMAIL_BACKEND='anymail.backends.brevo.EmailBackend', ANYMAIL={'BREVO_API_KEY': 'k'}):
            self.assertEqual(email_config_report()['warnings'], [])

    def test_icons_that_cannot_be_hosted_are_reported_in_production(self):
        with override_settings(EMAIL_BACKEND='anymail.backends.brevo.EmailBackend', ANYMAIL={'BREVO_API_KEY': 'k'},
                               FRONTEND_URL='http://localhost:3000'):
            self.assertTrue(any('icons' in w for w in email_config_report()['warnings']))
        with override_settings(EMAIL_BACKEND='anymail.backends.brevo.EmailBackend', ANYMAIL={'BREVO_API_KEY': 'k'},
                               FRONTEND_URL='http://localhost:3000', EMAIL_ICON_BASE_URL='none'):
            self.assertFalse(any('icons' in w for w in email_config_report()['warnings']))

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', EMAIL_HOST='smtp.gmail.com', EMAIL_PORT=587,
                       EMAIL_USE_TLS=True, EMAIL_HOST_USER='me@gmail.com', EMAIL_HOST_PASSWORD='x')
    def test_smtp_reports_are_unchanged(self):
        report = email_config_report()
        self.assertEqual((report['transport'], report['host'], report['port']), ('smtp', 'smtp.gmail.com', 587))

    def test_smtp_hints_still_work(self):
        self.assertIn('App Password', explain_smtp_error('SMTPAuthenticationError: (535) Username and Password not accepted'))
        self.assertIn('EMAIL_PROVIDER', explain_smtp_error('TimeoutError: timed out'))   # points to the HTTPS alternative

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend', FRONTEND_URL='http://localhost:3000')
    def test_smtp_still_embeds_the_icons_in_the_email(self):
        send()
        message = mail.outbox[-1].message()
        self.assertEqual(message.get_content_type(), 'multipart/related')
        ids = {p['Content-ID'].strip('<>') for p in message.walk() if p.get_content_type() == 'image/png'}
        self.assertEqual(ids, {'icon-check', 'icon-alert'})
        self.assertEqual(set(re.findall(r'cid:(icon-[a-z0-9-]+)', mail.outbox[-1].alternatives[0][0])), ids)
