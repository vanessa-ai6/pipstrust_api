"""The whole money journey: deposit -> invest -> matures -> withdraw."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.notifications.models import Notification
from apps.users.models import User, UserProfile

from .models import Deposit, Investment, InvestmentPlan, Transaction
from .services import confirm_deposit


def api_for(user):
    client = APIClient()
    client.force_authenticate(User.objects.get(pk=user.pk))  # fresh from the database, like a real request
    return client


class InvestmentLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='vanessa@example.com', password='StrongPass123!',
            first_name='Vanessa', last_name='T', is_verified=True,
        )
        UserProfile.objects.create(user=self.user, bitcoin_address='bc1-test')
        self.plan = InvestmentPlan.objects.create(
            name='Starter', min_amount=Decimal('50'), roi_percentage=Decimal('10'),
            duration=1, duration_unit='days',
        )

    def wallet(self):
        return UserProfile.objects.get(user=self.user)

    def test_funds_and_profit_return_to_the_main_balance_when_the_investment_completes(self):
        # 1. deposit is approved -> main balance
        deposit = Deposit.objects.create(user=self.user, amount=Decimal('200'), payment_method='bitcoin')
        confirm_deposit(deposit.id)
        self.assertEqual(self.wallet().wallet_balance, Decimal('200.00'))

        # 2. invest 150 -> main balance drops to 50, the money is "in" the investment
        res = api_for(self.user).post('/api/investments/create/', {'plan_id': str(self.plan.id), 'amount': '150.00'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        investment = Investment.objects.get(user=self.user)
        self.assertEqual(self.wallet().wallet_balance, Decimal('50.00'))
        self.assertEqual(investment.status, 'active')
        self.assertEqual(investment.expected_return, Decimal('165.00'))   # 150 + 10%

        # 3. while it is running nothing comes back yet
        dash = api_for(self.user).get('/api/investments/dashboard/').data['data']
        self.assertEqual(Decimal(str(dash['wallet_balance'])), Decimal('50.00'))
        self.assertEqual(dash['active_investments'], 1)

        # 4. time passes; the user opens the dashboard (or the Celery job runs)
        Investment.objects.filter(pk=investment.pk).update(ends_at=timezone.now() - timedelta(minutes=1))
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            dash = api_for(self.user).get('/api/investments/dashboard/').data['data']

        # 5. principal + profit are back in the main balance
        profile = self.wallet()
        self.assertEqual(profile.wallet_balance, Decimal('215.00'))      # 50 + 150 + 15
        self.assertEqual(Decimal(str(dash['wallet_balance'])), Decimal('215.00'))
        self.assertEqual(profile.total_earned, Decimal('15.00'))          # only the profit counts as "earned"
        self.assertEqual(profile.total_deposited, Decimal('200.00'))      # history unchanged
        investment.refresh_from_db()
        self.assertEqual(investment.status, 'completed')
        self.assertEqual(dash['completed_investments'], 1)
        self.assertEqual(dash['active_investments'], 0)
        self.assertTrue(Transaction.objects.filter(user=self.user, type='roi', status='success').exists())
        self.assertTrue(Notification.objects.filter(user=self.user, title='Investment Completed').exists())
        done = [m for m in mail.outbox if m.to == [self.user.email] and 'Investment Completed' in m.subject]
        self.assertEqual(len(done), 1)

        # 6. loading the dashboard again must not pay a second time
        api_for(self.user).get('/api/investments/dashboard/')
        api_for(self.user).get('/api/investments/')
        self.assertEqual(self.wallet().wallet_balance, Decimal('215.00'))
        self.assertEqual(Transaction.objects.filter(user=self.user, type='roi').count(), 1)

        # 7. the whole balance can be withdrawn
        res = api_for(self.user).post('/api/withdrawals/request/', {'amount': '215.00', 'method': 'bitcoin'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.wallet().wallet_balance, Decimal('0.00'))

        # 8. and the wallet-repair command agrees with every step above (it must not "fix" a healthy account)
        out = StringIO()
        call_command('reconcile_wallets', stdout=out)
        self.assertIn('All wallets match', out.getvalue(), out.getvalue())

    def test_reconcile_agrees_while_an_investment_is_still_running(self):
        deposit = Deposit.objects.create(user=self.user, amount=Decimal('200'), payment_method='bitcoin')
        confirm_deposit(deposit.id)
        api_for(self.user).post('/api/investments/create/', {'plan_id': str(self.plan.id), 'amount': '150.00'}, format='json')
        out = StringIO()
        call_command('reconcile_wallets', stdout=out)
        self.assertIn('All wallets match', out.getvalue(), out.getvalue())
