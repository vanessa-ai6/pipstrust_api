from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from apps.investments.models import Deposit
from apps.investments.services import expected_wallet_balance, lock_profile
from apps.notifications.models import Notification
from apps.users.models import User


class Command(BaseCommand):
    help = (
        'Find (and optionally fix) wallets that do not match the ledger, e.g. a deposit that '
        'was marked confirmed but never credited to the wallet.\n'
        'Dry run by default:   python manage.py reconcile_wallets\n'
        'Fix one user:         python manage.py reconcile_wallets --email user@example.com --apply\n'
        'Fix everyone:         python manage.py reconcile_wallets --apply'
    )

    def add_arguments(self, parser):
        parser.add_argument('--email', help='Only check this user.')
        parser.add_argument('--apply', action='store_true',
                            help='Write the corrections. Without this flag nothing is changed.')

    def handle(self, *args, **options):
        users = User.objects.all()
        if options['email']:
            users = users.filter(email__iexact=options['email'])
            if not users.exists():
                self.stderr.write(self.style.ERROR(f"No user with email {options['email']}"))
                return

        problems = 0
        for user in users.iterator():
            profile = getattr(user, 'profile', None)
            actual_wallet = profile.wallet_balance if profile else Decimal('0')
            actual_deposited = profile.total_deposited if profile else Decimal('0')
            expected_wallet = expected_wallet_balance(user)
            expected_deposited = (
                Deposit.objects.filter(user=user, status='confirmed').aggregate(s=Sum('amount'))['s'] or Decimal('0')
            )

            wallet_diff = expected_wallet - actual_wallet
            deposited_diff = expected_deposited - actual_deposited
            if wallet_diff == 0 and deposited_diff == 0:
                continue

            problems += 1
            self.stdout.write(
                f'{user.email}: wallet {actual_wallet} -> {expected_wallet} ({wallet_diff:+}), '
                f'total_deposited {actual_deposited} -> {expected_deposited} ({deposited_diff:+})'
            )
            if expected_wallet < 0:
                self.stdout.write(self.style.WARNING('  ! expected balance is negative - skipped, check this account by hand'))
                continue

            if options['apply']:
                with transaction.atomic():
                    locked = lock_profile(user)
                    locked.wallet_balance = expected_wallet
                    locked.total_deposited = expected_deposited
                    locked.save(update_fields=['wallet_balance', 'total_deposited', 'updated_at'])
                    if wallet_diff > 0:
                        Notification.objects.create(
                            user=user, title='Wallet Updated',
                            message=f'${wallet_diff} from your confirmed deposit(s) has been added to your wallet balance.',
                            type='success',
                        )
                self.stdout.write(self.style.SUCCESS('  fixed'))

        if not problems:
            self.stdout.write(self.style.SUCCESS('All wallets match the ledger.'))
        elif not options['apply']:
            self.stdout.write(self.style.WARNING(
                f'\n{problems} account(s) differ. Nothing was changed. Review the list, then re-run with --apply.\n'
                'Note: a balance you deliberately set by hand in the admin also shows up as a difference.'
            ))
