"""Regression tests for the deposit -> wallet flow and its emails."""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.notifications.models import EmailLog, Notification
from apps.users.models import User, UserProfile

from .models import Deposit, InvestmentPlan, Transaction
from .services import DepositNotPending, confirm_deposit, expected_wallet_balance


def make_user(email='vanessa@example.com', staff=False, **profile_kwargs):
    user = User.objects.create_user(
        email=email, password='StrongPass123!', first_name='Vanessa', last_name='Test',
        is_verified=True, is_staff=staff,
    )
    UserProfile.objects.create(user=user, **profile_kwargs)
    return user


@override_settings(ADMIN_EMAIL='boss@example.com')
class DepositToWalletTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.admin = make_user('admin@example.com', staff=True)
        self.client_user = APIClient()
        self.client_user.force_authenticate(self.user)
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)

    def _submit_deposit(self, amount='200.00'):
        res = self.client_user.post('/api/investments/deposits/create/', {
            'amount': amount, 'payment_method': 'bitcoin', 'transaction_hash': 'abc123',
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return Deposit.objects.get(id=res.data['data']['id'])

    def test_submitting_a_deposit_notifies_user_and_admins(self):
        deposit = self._submit_deposit()
        self.assertEqual(deposit.status, 'pending')
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn(self.user.email, recipients)          # "Deposit received"
        self.assertIn('boss@example.com', recipients)        # ADMIN_EMAIL
        self.assertIn(self.admin.email, recipients)          # staff users are alerted too
        self.assertTrue(Notification.objects.filter(user=self.user, title='Deposit Received').exists())
        self.assertEqual(EmailLog.objects.filter(status='sent').count(), len(mail.outbox))

    def test_approving_via_admin_api_credits_the_wallet(self):
        """THE reported bug: confirmed deposit must land in Wallet Balance, not only in Total Deposited."""
        deposit = self._submit_deposit('200.00')
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client_admin.post(f'/api/admin/deposits/{deposit.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)

        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.wallet_balance, Decimal('200.00'))
        self.assertEqual(profile.total_deposited, Decimal('200.00'))
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, 'confirmed')
        self.assertEqual(Transaction.objects.get(reference=str(deposit.id), type='deposit').status, 'success')

        # what the user's dashboard shows
        dash = self.client_user.get('/api/investments/dashboard/').data['data']
        self.assertEqual(Decimal(str(dash['wallet_balance'])), Decimal('200.00'))
        self.assertEqual(Decimal(str(dash['total_deposited'])), Decimal('200.00'))

        # user is emailed and notified
        self.assertTrue(any(m.to == [self.user.email] and 'Deposit Confirmed' in m.subject for m in mail.outbox))
        self.assertTrue(Notification.objects.filter(user=self.user, title='Deposit Confirmed').exists())

    def test_user_can_invest_and_withdraw_after_approval(self):
        deposit = self._submit_deposit('200.00')
        confirm_deposit(deposit.id)
        # a real request loads the user fresh from the database
        fresh = APIClient()
        fresh.force_authenticate(User.objects.get(pk=self.user.pk))

        plan = InvestmentPlan.objects.create(
            name='Starter', min_amount=Decimal('50'), roi_percentage=Decimal('10'), duration=1, duration_unit='days',
        )
        res = fresh.post('/api/investments/create/', {'plan_id': str(plan.id), 'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual(profile.wallet_balance, Decimal('50.00'))
        self.assertEqual(profile.total_deposited, Decimal('200.00'))  # investing must not change it

        # ...and the remaining wallet balance can be withdrawn
        UserProfile.objects.filter(user=self.user).update(bitcoin_address='bc1-test')
        fresh = APIClient()
        fresh.force_authenticate(User.objects.get(pk=self.user.pk))
        res = fresh.post('/api/withdrawals/request/', {'amount': '50.00', 'method': 'bitcoin'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(UserProfile.objects.get(user=self.user).wallet_balance, Decimal('0.00'))

    def test_approving_twice_does_not_double_credit(self):
        deposit = self._submit_deposit('200.00')
        confirm_deposit(deposit.id)
        with self.assertRaises(DepositNotPending):
            confirm_deposit(deposit.id)
        res = self.client_admin.post(f'/api/admin/deposits/{deposit.id}/action/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 404)
        self.assertEqual(UserProfile.objects.get(user=self.user).wallet_balance, Decimal('200.00'))

    def test_rejecting_does_not_touch_wallet_and_emails_user(self):
        deposit = self._submit_deposit('75.00')
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client_admin.post(f'/api/admin/deposits/{deposit.id}/action/',
                                         {'action': 'reject', 'admin_note': 'Hash not found'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(UserProfile.objects.get(user=self.user).wallet_balance, Decimal('0'))
        rejected = [m for m in mail.outbox if m.to == [self.user.email] and 'Rejected' in m.subject]
        self.assertEqual(len(rejected), 1)
        self.assertIn('Hash not found', rejected[0].alternatives[0][0])

    def test_non_admin_cannot_approve(self):
        deposit = self._submit_deposit()
        res = self.client_user.post(f'/api/admin/deposits/{deposit.id}/action/', {'action': 'approve'}, format='json')
        self.assertIn(res.status_code, (401, 403))
        self.assertEqual(UserProfile.objects.get(user=self.user).wallet_balance, Decimal('0'))

    def test_staff_without_profile_can_still_be_credited(self):
        bare = User.objects.create_user(email='noprofile@example.com', password='x', first_name='N', last_name='P')
        deposit = Deposit.objects.create(user=bare, amount=Decimal('10'), payment_method='usdt')
        confirm_deposit(deposit.id)
        self.assertEqual(bare.profile.wallet_balance, Decimal('10.00'))


class DjangoAdminDepositTests(TestCase):
    """Staff using /admin/ (not the API) must get the same result."""

    def setUp(self):
        from django.test import Client
        self.user = make_user()
        self.superuser = User.objects.create_superuser(email='root@example.com', password='StrongPass123!',
                                                       first_name='Root', last_name='User')
        self.web = Client()
        self.web.force_login(self.superuser)
        self.deposit = Deposit.objects.create(user=self.user, amount=Decimal('200'), payment_method='bitcoin')

    def test_approve_action_credits_wallet_and_emails_user(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self.web.post('/admin/investments/deposit/', {
                'action': 'approve_deposits', '_selected_action': [str(self.deposit.id)],
            }, follow=True)
        self.assertEqual(res.status_code, 200)
        profile = UserProfile.objects.get(user=self.user)
        self.assertEqual((profile.wallet_balance, profile.total_deposited), (Decimal('200.00'), Decimal('200.00')))
        self.assertTrue(any(m.to == [self.user.email] and 'Deposit Confirmed' in m.subject for m in mail.outbox))

    def test_status_cannot_be_edited_by_hand(self):
        """Editing status directly used to confirm a deposit without crediting the wallet."""
        res = self.web.get(f'/admin/investments/deposit/{self.deposit.id}/change/')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('name="status"', res.content.decode())  # rendered read-only, not an input


class ReconcileTests(TestCase):
    def test_repairs_a_confirmed_deposit_that_never_reached_the_wallet(self):
        """Vanessa's state in the screenshot: $200 confirmed, wallet $0."""
        user = make_user()
        Deposit.objects.create(user=user, amount=Decimal('200'), payment_method='bitcoin', status='confirmed')
        self.assertEqual(expected_wallet_balance(user), Decimal('200'))

        out = StringIO()
        call_command('reconcile_wallets', stdout=out)  # dry run
        self.assertIn('vanessa@example.com', out.getvalue())
        self.assertEqual(UserProfile.objects.get(user=user).wallet_balance, Decimal('0'))

        call_command('reconcile_wallets', '--email', 'vanessa@example.com', '--apply', stdout=StringIO())
        profile = UserProfile.objects.get(user=user)
        self.assertEqual(profile.wallet_balance, Decimal('200.00'))
        self.assertEqual(profile.total_deposited, Decimal('200.00'))
        self.assertTrue(Notification.objects.filter(user=user, title='Wallet Updated').exists())

        out = StringIO()
        call_command('reconcile_wallets', stdout=out)
        self.assertIn('All wallets match', out.getvalue())

    def test_ledger_accounts_for_investments_and_withdrawals(self):
        from apps.withdrawals.models import Withdrawal
        user = make_user()
        Deposit.objects.create(user=user, amount=Decimal('500'), payment_method='bitcoin', status='confirmed')
        Withdrawal.objects.create(user=user, amount=Decimal('100'), method='bitcoin', status='completed')
        Withdrawal.objects.create(user=user, amount=Decimal('40'), method='bitcoin', status='rejected')  # refunded
        self.assertEqual(expected_wallet_balance(user), Decimal('400'))
