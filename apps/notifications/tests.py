from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.users.models import OTP, User, UserProfile

from .email_utils import get_admin_emails, retry_email, send_html_email
from .models import EmailLog


def make_user(email='u@example.com', staff=False):
    user = User.objects.create_user(email=email, password='StrongPass123!', first_name='Uma', last_name='User',
                                    is_verified=True, is_staff=staff)
    UserProfile.objects.create(user=user)
    return user


class EmailLoggingTests(TestCase):
    def test_success_is_logged(self):
        user = make_user()
        self.assertTrue(send_html_email('Hi', 'broadcast', {'user_name': 'Uma', 'message': 'hello'}, [user.email]))
        log = EmailLog.objects.get()
        self.assertEqual((log.status, log.recipient, log.user_id), ('sent', user.email, user.id))
        self.assertEqual(len(mail.outbox), 1)

    def test_failure_is_recorded_not_swallowed(self):
        user = make_user()
        with patch('django.core.mail.EmailMultiAlternatives.send',
                   side_effect=OSError('(535) Username and Password not accepted')):
            ok = send_html_email('Hi', 'broadcast', {'user_name': 'Uma', 'message': 'hello'}, [user.email])
        self.assertFalse(ok)
        log = EmailLog.objects.get()
        self.assertEqual(log.status, 'failed')
        self.assertIn('535', log.error)
        self.assertTrue(log.retryable)

    def test_failed_email_can_be_retried(self):
        user = make_user()
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('boom')):
            send_html_email('Deposit Confirmed', 'deposit_confirmed',
                            {'user': user, 'user_name': 'Uma', 'amount': '10.00', 'payment_method': 'Bitcoin'},
                            [user.email])
        log = EmailLog.objects.get()
        self.assertTrue(retry_email(log))
        log.refresh_from_db()
        self.assertEqual((log.status, log.attempts), ('sent', 2))
        self.assertIn('$10.00', mail.outbox[-1].alternatives[0][0])  # template re-rendered with stored context

    def test_otp_emails_never_store_the_code_and_are_not_retryable(self):
        from apps.users.otp_utils import create_otp, send_otp_email
        user = make_user()
        otp = create_otp(user, 'withdrawal')
        send_otp_email(user, otp)
        log = EmailLog.objects.get()
        self.assertEqual(log.context, {})
        self.assertNotIn(otp.code, str(log.__dict__))
        self.assertIn(otp.code, mail.outbox[0].body)
        self.assertIn('Uma User', mail.outbox[0].alternatives[0][0])  # name now rendered in the HTML

    def test_send_otp_raises_when_delivery_fails(self):
        from apps.users.otp_utils import create_otp, send_otp_email
        user = make_user()
        otp = create_otp(user, 'withdrawal')
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('down')):
            with self.assertRaises(OSError):
                send_otp_email(user, otp)
        self.assertEqual(EmailLog.objects.get().status, 'failed')

    @override_settings(ADMIN_EMAIL='a@example.com, b@example.com')
    def test_admin_recipients_include_staff_and_dedupe(self):
        make_user('staff@example.com', staff=True)
        make_user('A@example.com', staff=True)
        self.assertEqual(sorted(e.lower() for e in get_admin_emails()),
                         ['a@example.com', 'b@example.com', 'staff@example.com'])


@override_settings(ADMIN_EMAIL='boss@example.com')
class EmailHealthEndpointTests(TestCase):
    def setUp(self):
        self.admin = make_user('admin@example.com', staff=True)
        self.user = make_user('regular@example.com')
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def test_admin_sees_logs_summary_and_config_without_password(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('(535) bad credentials')):
            send_html_email('X', 'broadcast', {'user_name': 'U', 'message': 'm'}, [self.user.email])
        data = self.api.get('/api/admin/email-logs/?status=failed').data['data']
        self.assertEqual(data['summary']['failed_24h'], 1)
        self.assertIn('App Password', data['summary']['last_failure_hint'])
        self.assertEqual(data['logs'][0]['status'], 'failed')
        self.assertNotIn('password', {k for k in data['config'] if k != 'password_set'})
        self.assertIn('warnings', data['config'])

    def test_admin_can_retry_from_api(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('x')):
            send_html_email('X', 'broadcast', {'user_name': 'U', 'message': 'm'}, [self.user.email])
        log = EmailLog.objects.get()
        res = self.api.post(f'/api/admin/email-logs/{log.id}/retry/')
        self.assertEqual(res.status_code, 200, res.data)
        log.refresh_from_db()
        self.assertEqual(log.status, 'sent')

    def test_test_email_reports_exact_error(self):
        ok = self.api.post('/api/admin/email-test/', {'to': 'me@example.com'}, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(mail.outbox[-1].to, ['me@example.com'])
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('Connection refused')):
            bad = self.api.post('/api/admin/email-test/', {}, format='json')
        self.assertEqual(bad.status_code, 502)
        self.assertIn('Connection refused', bad.data['message'])
        self.assertTrue(bad.data['data']['hint'])

    def test_email_tools_are_admin_only(self):
        regular = APIClient()
        regular.force_authenticate(self.user)
        self.assertIn(regular.get('/api/admin/email-logs/').status_code, (401, 403))
        self.assertIn(regular.post('/api/admin/email-test/', {}, format='json').status_code, (401, 403))

    def test_user_sees_only_own_email_status(self):
        send_html_email('Mine', 'broadcast', {'user_name': 'U', 'message': 'm'}, [self.user.email])
        send_html_email('Theirs', 'broadcast', {'user_name': 'A', 'message': 'm'}, [self.admin.email])
        regular = APIClient()
        regular.force_authenticate(self.user)
        data = regular.get('/api/notifications/email-log/').data['data']
        self.assertEqual([e['subject'] for e in data['emails']], ['Mine'])
        self.assertNotIn('error', data['emails'][0])
        self.assertEqual(data['failed_count'], 0)

    def test_user_failed_count_ignores_otp_and_counts_real_failures(self):
        from apps.users.otp_utils import create_otp, send_otp_email
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('down')):
            send_html_email('Deposit Confirmed', 'broadcast', {'user_name': 'U', 'message': 'm'}, [self.user.email])
            try:
                send_otp_email(self.user, create_otp(self.user, 'login'))
            except OSError:
                pass
        regular = APIClient()
        regular.force_authenticate(self.user)
        self.assertEqual(regular.get('/api/notifications/email-log/').data['data']['failed_count'], 1)


class OtpEmailFlowTests(TestCase):
    def test_register_survives_smtp_failure_and_says_so(self):
        api = APIClient()
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            res = api.post('/api/auth/register/', {
                'email': 'new@example.com', 'first_name': 'N', 'last_name': 'U',
                'password': 'StrongPass123!', 'password2': 'StrongPass123!',
            }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertFalse(res.data['data']['email_sent'])
        self.assertIn("couldn't send", res.data['message'])
        self.assertTrue(User.objects.filter(email='new@example.com').exists())
        self.assertEqual(EmailLog.objects.filter(status='failed').count(), 1)

    def test_resend_returns_503_when_email_fails(self):
        make_user('x@example.com')
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            res = APIClient().post('/api/auth/otp/resend/', {'email': 'x@example.com', 'purpose': 'email_verification'}, format='json')
        self.assertEqual(res.status_code, 503)

    def test_referral_bonus_paid_once_on_first_verification(self):
        referrer = make_user('ref@example.com')
        api = APIClient()
        api.post('/api/auth/register/', {
            'email': 'kid@example.com', 'first_name': 'K', 'last_name': 'D', 'password': 'StrongPass123!',
            'password2': 'StrongPass123!', 'referral_code': referrer.referral_code,
        }, format='json')
        otp = OTP.objects.get(user__email='kid@example.com')
        for _ in range(2):  # a second verify/login-style save must not pay again
            api.post('/api/auth/otp/verify-email/', {'email': 'kid@example.com', 'code': otp.code}, format='json')
            User.objects.get(email='kid@example.com').save()
        from apps.investments.models import Transaction
        self.assertEqual(Transaction.objects.filter(user=referrer, type='referral_bonus').count(), 1)
        self.assertTrue(any(m.to == [referrer.email] and 'Referral' in m.subject for m in mail.outbox))
