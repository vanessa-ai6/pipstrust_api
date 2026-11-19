from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema

from .models import Withdrawal
from .serializers import WithdrawalRequestSerializer, WithdrawalSerializer, OTPWithdrawalVerifySerializer
from apps.users.models import UserProfile
from apps.users.otp_utils import create_otp, verify_otp, send_otp_email
from apps.investments.models import Transaction
from apps.notifications.email_utils import notify_admins, send_html_email
from apps.notifications.models import Notification
from apps.users.views import api_response


PAYOUT_FIELD_BY_METHOD = {
    'bitcoin': 'bitcoin_address',
    'ethereum': 'ethereum_address',
    'usdt': 'usdt_address',
    'usdt2': 'usdt_erc20_address',
}


@extend_schema(tags=['Withdrawals'])
class WithdrawalRequestView(APIView):
    """
    Step 1: User submits a withdrawal request.
    System deducts from wallet immediately (held), sends OTP for confirmation.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=WithdrawalRequestSerializer, summary='Request a withdrawal (sends OTP)')
    def post(self, request):
        serializer = WithdrawalRequestSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return api_response(
                message='Validation failed.', status_str='error', 
                errors=serializer.errors, http_status=400
            )

        user = request.user
        amount = serializer.validated_data['amount']
        method = serializer.validated_data['method']

        with db_transaction.atomic():
            profile = UserProfile.objects.select_for_update().get(user=user)

            # Re-check the balance after locking it. This prevents two browser tabs
            # from withdrawing the same funds simultaneously.
            if amount > profile.wallet_balance:
                return api_response(
                    message=f'Insufficient balance. Available: ${profile.wallet_balance}',
                    status_str='error', http_status=400
                )

            payout_address = getattr(profile, PAYOUT_FIELD_BY_METHOD[method], None)
            if not payout_address:
                return api_response(
                    message='Please add the selected payout address in your profile first.',
                    status_str='error', http_status=400
                )

            withdrawal = Withdrawal.objects.create(
                user=user, 
                amount=amount, 
                method=method,
                payout_address=payout_address.strip(),
            )

            profile.wallet_balance -= amount
            profile.save(update_fields=['wallet_balance', 'updated_at'])

            Transaction.objects.create(
                user=user,
                type='withdrawal',
                amount=amount,
                status='pending',
                description=f'Withdrawal via {withdrawal.get_method_display()}',
                reference=str(withdrawal.id),
            )

        try:
            otp = create_otp(user, 'withdrawal')
            send_otp_email(user, otp)
        except Exception:
            # If the OTP cannot be delivered, do not leave the user's money held
            # behind a request they cannot confirm.
            with db_transaction.atomic():
                held = Withdrawal.objects.select_for_update().filter(
                    id=withdrawal.id, user=user, status='pending'
                ).first()
                if held:
                    locked_profile = UserProfile.objects.select_for_update().get(user=user)
                    locked_profile.wallet_balance += held.amount
                    locked_profile.save(update_fields=['wallet_balance', 'updated_at'])
                    held.status = 'rejected'
                    held.admin_note = 'Automatically cancelled because the confirmation OTP could not be delivered.'
                    held.save(update_fields=['status', 'admin_note', 'updated_at'])
                    Transaction.objects.filter(
                        reference=str(held.id), type='withdrawal'
                    ).update(status='failed')
            return api_response(
                message='We could not send the confirmation code. No funds were deducted. Please try again.',
                status_str='error', http_status=503
            )

        Notification.objects.create(
            user=user,
            title='Withdrawal Pending',
            message=f'Your withdrawal request of ${withdrawal.amount} has been submitted and is pending confirmation.',
            type='info',
        )
        send_html_email(
            subject='Withdrawal Request Submitted - PipsTrust',
            template_name='withdrawal_pending',
            context={
                'user': user, 
                'user_name': user.full_name, 
                'amount': amount, 
                'method': withdrawal.get_method_display(), 
                'payout_address': withdrawal.payout_address
                },
            recipient_list=[user.email],
        )

        return api_response(
            data={'withdrawal_id': str(withdrawal.id)},
            message='Withdrawal request created. Please confirm with the OTP sent to your email.',
            http_status=201,
        )


@extend_schema(tags=['Withdrawals'])
class WithdrawalOTPVerifyView(APIView):
    """
    Step 2: User confirms withdrawal using OTP.
    Moves status to otp_verified — admin processes it manually.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=OTPWithdrawalVerifySerializer, summary='Confirm withdrawal with OTP')
    def post(self, request):
        serializer = OTPWithdrawalVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(
                message='Validation failed.', status_str='error', 
                errors=serializer.errors, http_status=400
            )

        withdrawal_id = serializer.validated_data['withdrawal_id']
        withdrawal = Withdrawal.objects.filter(
            id=withdrawal_id, user=request.user, status='pending'
        ).first()
        if not withdrawal:
            return api_response(message='Withdrawal not found or already processed.', status_str='error', http_status=404)
        
        success, error = verify_otp(request.user, serializer.validated_data['code'], 'withdrawal')
        if not success:
            return api_response(message=error, status_str='error', http_status=400)

        with db_transaction.atomic():
            withdrawal = Withdrawal.objects.select_for_update().filter(
                id=withdrawal_id, user=request.user, status='pending',
            ).first()
            if not withdrawal:
                return api_response(message='Withdrawal not found or already processed.', status_str='error', http_status=404)
            withdrawal.status = 'otp_verified'
            withdrawal.save(update_fields=['status', 'updated_at'])
            Transaction.objects.filter(reference=str(withdrawal.id), type='withdrawal').update(status='pending')
            Notification.objects.create(
                user=request.user, title='Withdrawal Confirmed',
                message=f'Your withdrawal of ${withdrawal.amount} has been confirmed. It will be processed shortly.', type='info',
            )

        notify_admins(
            subject='Withdrawal Awaiting Approval',
            message=(
                f'User {request.user.email} confirmed a withdrawal of ${withdrawal.amount} '
                f'via {withdrawal.get_method_display()}.\n'
                f'Payout address: {withdrawal.payout_address}\n\n'
                'Please review it in the admin panel and approve or reject it.'
            ),
        )
        return api_response(message='Withdrawal confirmed. It will be processed shortly.')


@extend_schema(tags=['Withdrawals'])
class WithdrawalResendOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        withdrawal = Withdrawal.objects.filter(id=pk, user=request.user, status='pending').first()
        if not withdrawal:
            return api_response(message='Withdrawal not found or no longer awaiting OTP', status_str='error', http_status=404)
        try:
            otp = create_otp(request.user, 'withdrawal')
            send_otp_email(request.user, otp)
        except Exception:
            return api_response(message='We could not send the confirmation code. Please try again later.', status_str='error', http_status=503)
        return api_response(message='A new OTP has been sent to your email.')
    

@extend_schema(tags=['Withdrawals'])
class WithdrawalListView(generics.ListAPIView):
    serializer_class = WithdrawalSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['status', 'method']
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        return Withdrawal.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Withdrawals'])
class WithdrawalDetailView(generics.RetrieveAPIView):
    serializer_class = WithdrawalSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Withdrawal.objects.filter(user=self.request.user)


@extend_schema(tags=['Withdrawals'])
class WithdrawalCancelView(APIView):
    """Cancel a pending (not yet OTP verified) withdrawal and refund wallet."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        with db_transaction.atomic():
            withdrawal = Withdrawal.objects.select_for_update().filter(
                id=pk, user=request.user, status='pending'
            ).first()
            if not withdrawal:
                return api_response(message='Withdrawal not found or cannot be cancelled.', status_str='error', http_status=404)

            profile = UserProfile.objects.select_for_update().get(user=request.user)
            withdrawal.status = 'rejected'
            withdrawal.admin_note = 'Cancelled by user.'
            withdrawal.save(update_fields=['status', 'admin_note', 'updated_at'])
            profile.wallet_balance += withdrawal.amount
            profile.save(update_fields=['wallet_balance', 'updated_at'])
            Transaction.objects.filter(reference=str(withdrawal.id), type='withdrawal').update(status='failed')
            Notification.objects.create(
                user=request.user, title='Withdrawal Cancelled - Funds Refunded',
                message=f'Your withdrawal of ${withdrawal.amount} has been cancelled and the amount refunded to your wallet.', type='info',
            )

        send_html_email(
            subject='Withdrawal Cancelled - Funds Refunded',
            template_name='withdrawal_refunded',
            context={'user': request.user, 'user_name': request.user.full_name, 'amount': withdrawal.amount, 'method': withdrawal.get_method_display(), 'message': f'Your withdrawal of ${withdrawal.amount} has been cancelled and the amount refunded to your wallet.'},
            recipient_list=[request.user.email],
        )
        return api_response(message='Withdrawal cancelled and amount refunded to your wallet.')