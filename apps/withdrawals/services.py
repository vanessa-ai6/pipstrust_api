from django.db import transaction
from django.utils import timezone

from apps.investments.models import Transaction
from apps.investments.services import lock_profile
from apps.notifications.email_utils import send_email_after_commit
from apps.notifications.models import Notification

from .models import Withdrawal


class WithdrawalNotActionable(Exception):
    """The withdrawal does not exist or is not in a state that allows this action."""


def complete_withdrawal(withdrawal_id, admin_note=''):
    """Mark an OTP-verified/processing withdrawal as paid out."""
    with transaction.atomic():
        w = (
            Withdrawal.objects.select_for_update().select_related('user')
            .filter(id=withdrawal_id, status__in=['otp_verified', 'processing']).first()
        )
        if not w:
            raise WithdrawalNotActionable()

        w.status = 'completed'
        w.admin_note = admin_note or ''
        w.processed_at = timezone.now()
        w.save(update_fields=['status', 'admin_note', 'processed_at', 'updated_at'])

        profile = lock_profile(w.user)
        profile.total_withdrawn += w.amount
        profile.save(update_fields=['total_withdrawn', 'updated_at'])

        Transaction.objects.filter(reference=str(w.id), type='withdrawal').update(status='success')

        Notification.objects.create(
            user=w.user, title='Withdrawal Processed',
            message=f'Your withdrawal of ${w.amount} has been processed.', type='success',
        )
        send_email_after_commit(
            subject='Withdrawal Processed - PipsTrust',
            template_name='withdrawal_processed',
            context={
                'user': w.user,
                'user_name': w.user.full_name,
                'amount': w.amount,
                'method': w.get_method_display(),
                'payout_address': w.payout_address or 'N/A',
                'admin_note': admin_note,
            },
            recipient_list=[w.user.email],
        )
    return w


def reject_withdrawal(withdrawal_id, admin_note=''):
    """Reject a withdrawal and return the held amount to the user's wallet."""
    with transaction.atomic():
        w = (
            Withdrawal.objects.select_for_update().select_related('user')
            .filter(id=withdrawal_id, status__in=['pending', 'otp_verified', 'processing']).first()
        )
        if not w:
            raise WithdrawalNotActionable()

        w.status = 'rejected'
        w.admin_note = admin_note or ''
        w.save(update_fields=['status', 'admin_note', 'updated_at'])

        profile = lock_profile(w.user)
        profile.wallet_balance += w.amount
        profile.save(update_fields=['wallet_balance', 'updated_at'])

        Transaction.objects.filter(reference=str(w.id), type='withdrawal').update(status='failed')

        reason = f' Reason: {admin_note}' if admin_note else ''
        Notification.objects.create(
            user=w.user, title='Withdrawal Rejected',
            message=f'Your withdrawal of ${w.amount} was rejected and refunded to your wallet.{reason}',
            type='warning',
        )
        send_email_after_commit(
            subject='Withdrawal Rejected - PipsTrust',
            template_name='withdrawal_rejected',
            context={
                'user': w.user,
                'user_name': w.user.full_name,
                'amount': w.amount,
                'refund_amount': w.amount,
                'method': w.get_method_display(),
                'admin_note': admin_note,
            },
            recipient_list=[w.user.email],
        )
    return w
