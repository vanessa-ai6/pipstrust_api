import logging
from decimal import Decimal

from celery import shared_task
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


@shared_task
def send_notification_email(subject, message, recipient_list=None):
    """Send a plain notification (broadcast template). Defaults to the admin recipients.

    Delivery problems are recorded in EmailLog by send_html_email, so they are visible
    to admins instead of disappearing into a Celery retry loop.
    """
    from apps.notifications.email_utils import get_admin_emails, send_html_email

    recipients = recipient_list or get_admin_emails()
    return send_html_email(
        subject=subject,
        template_name='broadcast',
        context={'user_name': 'there', 'message': message},
        recipient_list=recipients,
    )


@shared_task
def process_matured_investments():
    """Periodic safety net for completing matured investments."""
    from apps.investments.services import settle_matured_investments

    settle_matured_investments()


@shared_task
def send_referral_bonus(referrer_id, new_user_email, bonus_amount):
    """Credit the referral bonus to the referrer's wallet (once) and email them."""
    from apps.investments.models import Transaction
    from apps.notifications.email_utils import send_html_email
    from apps.notifications.models import Notification
    from apps.users.models import User, UserProfile

    bonus = Decimal(str(bonus_amount))
    description = f'Referral bonus for inviting {new_user_email}'

    try:
        with transaction.atomic():
            referrer = User.objects.get(id=referrer_id)
            UserProfile.objects.get_or_create(user=referrer)
            profile = UserProfile.objects.select_for_update().get(user=referrer)

            # Idempotent: never pay the same referral twice.
            if Transaction.objects.filter(
                user=referrer, type='referral_bonus', description=description
            ).exists():
                return

            profile.wallet_balance += bonus
            profile.total_earned += bonus
            profile.save(update_fields=['wallet_balance', 'total_earned', 'updated_at'])

            Transaction.objects.create(
                user=referrer, type='referral_bonus', amount=bonus,
                status='success', description=description,
            )
            Notification.objects.create(
                user=referrer, title='Referral Bonus Credited',
                message=f'You earned ${bonus} for inviting {new_user_email}.', type='success',
            )
    except Exception:
        logger.exception('Referral bonus failed for referrer %s', referrer_id)
        return

    send_html_email(
        subject='Referral Bonus Credited - PipsTrust',
        template_name='broadcast',
        context={
            'user': referrer,
            'user_name': referrer.full_name,
            'message': (
                f'You earned a referral bonus of ${bonus} for inviting {new_user_email} to PipsTrust!\n\n'
                'It has been added to your wallet balance.'
            ),
        },
        recipient_list=[referrer.email],
    )
