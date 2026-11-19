import random
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from apps.notifications.email_utils import send_html_email
from .models import OTP


def generate_otp_code():
    """Generate a 6-digit OTP code."""
    return str(random.randint(100000, 999999))


def create_otp(user, purpose):
    """Invalidate old OTPs and create a new one."""
    OTP.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)
    expiry = timezone.now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)
    code = generate_otp_code()
    otp = OTP.objects.create(user=user, code=code, purpose=purpose, expires_at=expiry)
    return otp


def verify_otp(user, code, purpose):
    """
    Verify OTP. Returns (True, None) on success or (False, error_message).
    """
    try:
        otp = OTP.objects.get(
            user=user, code=code, purpose=purpose, is_used=False
        )
    except OTP.DoesNotExist:
        return False, 'Invalid OTP code.'

    if otp.expires_at < timezone.now():
        return False, 'OTP has expired. Please request a new one.'

    otp.is_used = True
    otp.save()
    return True, None


def send_otp_email(user, otp):
    """Send the OTP email. Raises if it could not be delivered.

    Delivery is recorded in EmailLog (the code itself is never stored there).
    """
    purpose_labels = {
        'email_verification': 'Email Verification',
        'password_reset': 'Password Reset',
        'login': 'Login Verification',
        'withdrawal': 'Withdrawal Confirmation',
    }
    subject_map = {
        'email_verification': 'Verify Your Email - PipsTrust',
        'password_reset': 'Password Reset OTP - PipsTrust',
        'login': 'Login Verification Code - PipsTrust',
        'withdrawal': 'Withdrawal Confirmation OTP - PipsTrust',
    }
    purpose_label = purpose_labels.get(otp.purpose, 'Verification')

    text_body = (
        f"Hello {user.full_name},\n\n"
        f"Your OTP for {purpose_label} is: {otp.code}\n"
        f"This code expires in {settings.OTP_EXPIRY_MINUTES} minutes.\n\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"PipsTrust Team"
    )

    send_html_email(
        subject=subject_map.get(otp.purpose, 'OTP Code - PipsTrust'),
        template_name='otp',
        context={
            'user': user,
            'user_name': user.full_name,
            'otp_code': otp.code,
            'purpose_label': purpose_label,
            'expiry_minutes': settings.OTP_EXPIRY_MINUTES,
        },
        recipient_list=[user.email],
        raise_on_error=True,
        text_body=text_body,
    )
