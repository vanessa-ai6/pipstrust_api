# from decimal import Decimal

# from django.db import transaction
# from django.db.models import Sum
# from django.utils import timezone

# from apps.notifications.models import Notification
# from apps.notifications.email_utils import send_email_after_commit, send_html_email
# from apps.users.models import UserProfile

# from .models import Deposit, Investment, Transaction


# class DepositNotPending(Exception):
#     """The deposit does not exist or was already approved/rejected."""


# def lock_profile(user):
#     """Return the user's profile row locked for update (creating it if it is missing).

#     Staff accounts created with ``createsuperuser`` have no profile; without this a
#     wallet operation on them would crash.
#     """
#     UserProfile.objects.get_or_create(user=user)
#     return UserProfile.objects.select_for_update().get(user=user)


# def confirm_deposit(deposit_id, admin_note=''):
#     """Approve a pending deposit and credit the user's WALLET.

#     This is the single place where a deposit becomes spendable money. Both the admin
#     API and the Django admin call it, so the two can never disagree again. Everything
#     happens in one transaction: either the deposit is confirmed *and* the wallet is
#     credited, or nothing changes. Raises DepositNotPending if it was already processed
#     (which also makes double-approval impossible).
#     """
#     with transaction.atomic():
#         deposit = (
#             Deposit.objects.select_for_update().select_related('user')
#             .filter(id=deposit_id, status='pending').first()
#         )
#         if not deposit:
#             raise DepositNotPending()

#         now = timezone.now()
#         deposit.status = 'confirmed'
#         deposit.confirmed_at = now
#         deposit.admin_note = admin_note or ''
#         deposit.save(update_fields=['status', 'confirmed_at', 'admin_note', 'updated_at'])

#         profile = lock_profile(deposit.user)
#         profile.wallet_balance += deposit.amount
#         profile.total_deposited += deposit.amount
#         profile.save(update_fields=['wallet_balance', 'total_deposited', 'updated_at'])

#         updated = Transaction.objects.filter(reference=str(deposit.id), type='deposit').update(status='success')
#         if not updated:  # deposit created outside the API (e.g. Django admin)
#             Transaction.objects.create(
#                 user=deposit.user, type='deposit', amount=deposit.amount, status='success',
#                 description=f'Deposit via {deposit.get_payment_method_display()}',
#                 reference=str(deposit.id),
#             )

#         Notification.objects.create(
#             user=deposit.user,
#             title='Deposit Confirmed',
#             message=f'Your deposit of ${deposit.amount} has been confirmed and added to your wallet balance.',
#             type='success',
#         )
#         send_email_after_commit(
#             subject='Deposit Confirmed - PipsTrust',
#             template_name='deposit_confirmed',
#             context={
#                 'user': deposit.user,
#                 'user_name': deposit.user.full_name,
#                 'amount': deposit.amount,
#                 'payment_method': deposit.get_payment_method_display(),
#                 'new_balance': profile.wallet_balance,
#             },
#             recipient_list=[deposit.user.email],
#         )
#     return deposit


# def reject_deposit(deposit_id, admin_note=''):
#     """Reject a pending deposit. No money moves."""
#     with transaction.atomic():
#         deposit = (
#             Deposit.objects.select_for_update().select_related('user')
#             .filter(id=deposit_id, status='pending').first()
#         )
#         if not deposit:
#             raise DepositNotPending()

#         deposit.status = 'rejected'
#         deposit.admin_note = admin_note or ''
#         deposit.save(update_fields=['status', 'admin_note', 'updated_at'])

#         Transaction.objects.filter(reference=str(deposit.id), type='deposit').update(status='failed')

#         reason = f' Reason: {admin_note}' if admin_note else ''
#         Notification.objects.create(
#             user=deposit.user,
#             title='Deposit Rejected',
#             message=f'Your deposit of ${deposit.amount} was rejected.{reason}',
#             type='error',
#         )
#         send_email_after_commit(
#             subject='Deposit Rejected - PipsTrust',
#             template_name='deposit_rejected',
#             context={
#                 'user': deposit.user,
#                 'user_name': deposit.user.full_name,
#                 'amount': deposit.amount,
#                 'payment_method': deposit.get_payment_method_display(),
#                 'reason': admin_note,
#             },
#             recipient_list=[deposit.user.email],
#         )
#     return deposit


# def expected_wallet_balance(user):
#     """What the wallet should hold according to the ledger (deposits, ROI, investments, withdrawals).

#     Used by ``manage.py reconcile_wallets`` to find wallets that drifted, e.g. a deposit
#     that was marked confirmed without being credited.
#     """
#     from apps.withdrawals.models import Withdrawal

#     def total(queryset):
#         return queryset.aggregate(s=Sum('amount'))['s'] or Decimal('0')

#     confirmed_deposits = total(Deposit.objects.filter(user=user, status='confirmed'))
#     credits = total(Transaction.objects.filter(
#         user=user, type__in=['roi', 'referral_bonus'], status='success',
#     ))
#     invested = total(Investment.objects.filter(user=user).exclude(status='cancelled'))
#     withdrawn = total(Withdrawal.objects.filter(
#         user=user, status__in=['pending', 'otp_verified', 'processing', 'completed'],
#     ))
#     return confirmed_deposits + credits - invested - withdrawn


# def settle_matured_investments(user=None):
#     """Atomically complete every matured active investment and credit it once.

#     This function is intentionally safe to call from Celery, dashboard reads, and
#     investment list reads. Row locks plus the active-status check prevent duplicate
#     ROI credits when two requests run at the same time.
#     """
#     now = timezone.now()
#     query = Investment.objects.filter(status='active', ends_at__lte=now)
#     if user is not None:
#         query = query.filter(user=user)

#     investment_ids = list(query.values_list('id', flat=True))
#     completed = []

#     for investment_id in investment_ids:
#         with transaction.atomic():
#             investment = (
#                 Investment.objects.select_for_update()
#                 .select_related('user', 'plan')
#                 .filter(id=investment_id, status='active', ends_at__lte=now)
#                 .first()
#             )
#             if not investment:
#                 continue

#             profile = UserProfile.objects.select_for_update().get(user=investment.user)
#             expected_return = Decimal(investment.expected_return)
#             profit = expected_return - Decimal(investment.amount)

#             profile.wallet_balance += expected_return
#             profile.total_earned += profit
#             profile.save(update_fields=['wallet_balance', 'total_earned', 'updated_at'])

#             investment.status = 'completed'
#             investment.actual_return = expected_return
#             investment.completed_at = now
#             investment.save(update_fields=['status', 'actual_return', 'completed_at'])

#             Transaction.objects.get_or_create(
#                 user=investment.user,
#                 type='roi',
#                 reference=str(investment.id),
#                 defaults={
#                     'amount': expected_return,
#                     'status': 'success',
#                     'description': f'ROI credited for investment #{str(investment.id)[:8]}',
#                 },
#             )

#             Notification.objects.create(
#                 user=investment.user,
#                 title='Investment Completed',
#                 message=(
#                     f'Your {investment.plan.name} investment has completed. '
#                     f'${expected_return} has been credited to your wallet.'
#                 ),
#                 type='success',
#             )

#             def send_completion_email(investment, expected_return=expected_return):
#                 try:
#                     send_html_email(
#                         subject='Investment Completed - Return Credited',
#                         template_name='investment_completed',
#                         context={
#                             'user': investment.user,
#                             'user_name': investment.user.full_name,
#                             'plan_name': investment.plan.name,
#                             'amount': investment.amount,
#                             'expected_return': expected_return,
#                             'profit': profit,
#                         },
#                         recipient_list=[investment.user.email],
#                     )
#                 except Exception:
#                     pass
        
#             transaction.on_commit(send_completion_email)
#             completed.append(investment)

#     return completed


from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.notifications.models import Notification
from apps.notifications.email_utils import send_email_after_commit
from apps.users.models import UserProfile

from .models import Deposit, Investment, Transaction


class DepositNotPending(Exception):
    """The deposit does not exist or was already approved/rejected."""


def lock_profile(user):
    """Return the user's profile row locked for update (creating it if it is missing).

    Staff accounts created with ``createsuperuser`` have no profile; without this a
    wallet operation on them would crash.
    """
    UserProfile.objects.get_or_create(user=user)
    return UserProfile.objects.select_for_update().get(user=user)


def confirm_deposit(deposit_id, admin_note=''):
    """Approve a pending deposit and credit the user's WALLET.

    This is the single place where a deposit becomes spendable money. Both the admin
    API and the Django admin call it, so the two can never disagree again. Everything
    happens in one transaction: either the deposit is confirmed *and* the wallet is
    credited, or nothing changes. Raises DepositNotPending if it was already processed
    (which also makes double-approval impossible).
    """
    with transaction.atomic():
        deposit = (
            Deposit.objects.select_for_update().select_related('user')
            .filter(id=deposit_id, status='pending').first()
        )
        if not deposit:
            raise DepositNotPending()

        now = timezone.now()
        deposit.status = 'confirmed'
        deposit.confirmed_at = now
        deposit.admin_note = admin_note or ''
        deposit.save(update_fields=['status', 'confirmed_at', 'admin_note', 'updated_at'])

        profile = lock_profile(deposit.user)
        profile.wallet_balance += deposit.amount
        profile.total_deposited += deposit.amount
        profile.save(update_fields=['wallet_balance', 'total_deposited', 'updated_at'])

        updated = Transaction.objects.filter(reference=str(deposit.id), type='deposit').update(status='success')
        if not updated:  # deposit created outside the API (e.g. Django admin)
            Transaction.objects.create(
                user=deposit.user, type='deposit', amount=deposit.amount, status='success',
                description=f'Deposit via {deposit.get_payment_method_display()}',
                reference=str(deposit.id),
            )

        Notification.objects.create(
            user=deposit.user,
            title='Deposit Confirmed',
            message=f'Your deposit of ${deposit.amount} has been confirmed and added to your wallet balance.',
            type='success',
        )
        send_email_after_commit(
            subject='Deposit Confirmed - PipsTrust',
            template_name='deposit_confirmed',
            context={
                'user': deposit.user,
                'user_name': deposit.user.full_name,
                'amount': deposit.amount,
                'payment_method': deposit.get_payment_method_display(),
                'new_balance': profile.wallet_balance,
            },
            recipient_list=[deposit.user.email],
        )
    return deposit


def reject_deposit(deposit_id, admin_note=''):
    """Reject a pending deposit. No money moves."""
    with transaction.atomic():
        deposit = (
            Deposit.objects.select_for_update().select_related('user')
            .filter(id=deposit_id, status='pending').first()
        )
        if not deposit:
            raise DepositNotPending()

        deposit.status = 'rejected'
        deposit.admin_note = admin_note or ''
        deposit.save(update_fields=['status', 'admin_note', 'updated_at'])

        Transaction.objects.filter(reference=str(deposit.id), type='deposit').update(status='failed')

        reason = f' Reason: {admin_note}' if admin_note else ''
        Notification.objects.create(
            user=deposit.user,
            title='Deposit Rejected',
            message=f'Your deposit of ${deposit.amount} was rejected.{reason}',
            type='error',
        )
        send_email_after_commit(
            subject='Deposit Rejected - PipsTrust',
            template_name='deposit_rejected',
            context={
                'user': deposit.user,
                'user_name': deposit.user.full_name,
                'amount': deposit.amount,
                'payment_method': deposit.get_payment_method_display(),
                'reason': admin_note,
            },
            recipient_list=[deposit.user.email],
        )
    return deposit


def expected_wallet_balance(user):
    """What the wallet should hold according to the ledger (deposits, ROI, investments, withdrawals).

    Used by ``manage.py reconcile_wallets`` to find wallets that drifted, e.g. a deposit
    that was marked confirmed without being credited.
    """
    from apps.withdrawals.models import Withdrawal

    def total(queryset):
        return queryset.aggregate(s=Sum('amount'))['s'] or Decimal('0')

    confirmed_deposits = total(Deposit.objects.filter(user=user, status='confirmed'))
    credits = total(Transaction.objects.filter(
        user=user, type__in=['roi', 'referral_bonus'], status='success',
    ))
    invested = total(Investment.objects.filter(user=user).exclude(status='cancelled'))
    withdrawn = total(Withdrawal.objects.filter(
        user=user, status__in=['pending', 'otp_verified', 'processing', 'completed'],
    ))
    return confirmed_deposits + credits - invested - withdrawn


def settle_matured_investments(user=None):
    """Atomically complete every matured active investment and credit it once.

    This function is intentionally safe to call from Celery, dashboard reads, and
    investment list reads. Row locks plus the active-status check prevent duplicate
    ROI credits when two requests run at the same time.
    """
    now = timezone.now()
    query = Investment.objects.filter(status='active', ends_at__lte=now)
    if user is not None:
        query = query.filter(user=user)

    investment_ids = list(query.values_list('id', flat=True))
    completed = []

    for investment_id in investment_ids:
        with transaction.atomic():
            investment = (
                Investment.objects.select_for_update()
                .select_related('user', 'plan')
                .filter(id=investment_id, status='active', ends_at__lte=now)
                .first()
            )
            if not investment:
                continue

            profile = UserProfile.objects.select_for_update().get(user=investment.user)
            expected_return = Decimal(investment.expected_return)
            profit = expected_return - Decimal(investment.amount)

            profile.wallet_balance += expected_return
            profile.total_earned += profit
            profile.save(update_fields=['wallet_balance', 'total_earned', 'updated_at'])

            investment.status = 'completed'
            investment.actual_return = expected_return
            investment.completed_at = now
            investment.save(update_fields=['status', 'actual_return', 'completed_at'])

            Transaction.objects.get_or_create(
                user=investment.user,
                type='roi',
                reference=str(investment.id),
                defaults={
                    'amount': expected_return,
                    'status': 'success',
                    'description': f'ROI credited for investment #{str(investment.id)[:8]}',
                },
            )

            Notification.objects.create(
                user=investment.user,
                title='Investment Completed',
                message=(
                    f'Your {investment.plan.name} investment has completed. '
                    f'${expected_return} has been credited to your wallet.'
                ),
                type='success',
            )

            # Sent only after the credit has committed. (This used to register a function that
            # needed an argument with on_commit(), which calls it with none: TypeError, so the
            # email never went out and the request that triggered settlement crashed.)
            send_email_after_commit(
                subject='Investment Completed - Return Credited',
                template_name='investment_completed',
                context={
                    'user': investment.user,
                    'user_name': investment.user.full_name,
                    'plan_name': investment.plan.name,
                    'amount': investment.amount,
                    'expected_return': expected_return,
                    'profit': profit,
                },
                recipient_list=[investment.user.email],
            )
            completed.append(investment)

    return completed
