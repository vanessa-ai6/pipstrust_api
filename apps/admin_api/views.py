from datetime import timedelta

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.filters import SearchFilter, OrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction as db_transaction
from django.db.models import Sum, Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema

from .permissions import IsAdminUser
from .serializers import (
    AdminUserSerializer, AdminUserUpdateSerializer,
    AdminDepositSerializer, AdminDepositActionSerializer,
    AdminWithdrawalSerializer, AdminWithdrawalActionSerializer,
    AdminInvestmentPlanSerializer, AdminPlatformStatsSerializer,
    AdminBroadcastSerializer, AdminCreditWalletSerializer,
    AdminEmailLogSerializer, AdminEmailTestSerializer,
)
from apps.users.models import User, UserProfile
from apps.investments.models import InvestmentPlan, Deposit, Investment, Transaction
from apps.investments.services import (
    DepositNotPending, confirm_deposit, lock_profile, reject_deposit, settle_matured_investments,
)
from apps.withdrawals.models import Withdrawal
from apps.withdrawals.services import WithdrawalNotActionable, complete_withdrawal, reject_withdrawal
from apps.notifications.models import EmailLog, Notification
from apps.notifications.email_utils import (
    email_config_report, explain_smtp_error, retry_email, send_email_after_commit, send_html_email,
)
from apps.users.views import api_response


# ─── Platform Stats ───────────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Stats'])
class AdminPlatformStatsView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(responses=AdminPlatformStatsSerializer, summary='Get platform-wide statistics')
    def get(self, request):
        settle_matured_investments()
        total_deposits = Deposit.objects.filter(status='confirmed').aggregate(
            total=Sum('amount')
        )['total'] or 0

        total_withdrawals = Withdrawal.objects.filter(status='completed').aggregate(
            total=Sum('amount')
        )['total'] or 0

        from apps.users.models import UserProfile
        total_platform_balance = UserProfile.objects.aggregate(
            total=Sum('wallet_balance')
        )['total'] or 0

        data = {
            'total_users': User.objects.count(),
            'verified_users': User.objects.filter(is_verified=True).count(),
            'active_users': User.objects.filter(is_active=True).count(),
            'total_deposits': total_deposits,
            'total_withdrawals': total_withdrawals,
            'total_active_investments': Investment.objects.filter(status='active').count(),
            'total_completed_investments': Investment.objects.filter(status='completed').count(),
            'pending_deposits': Deposit.objects.filter(status='pending').count(),
            'pending_withdrawals': Withdrawal.objects.filter(status__in=['pending', 'otp_verified']).count(),
            'total_platform_balance': total_platform_balance,
        }
        return api_response(data=AdminPlatformStatsSerializer(data).data)


# ─── User Management ──────────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Users'])
class AdminUserListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminUserSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active', 'is_verified', 'is_staff', 'country']
    search_fields = ['email', 'first_name', 'last_name', 'phone']
    ordering_fields = ['created_at', 'email']

    def get_queryset(self):
        return User.objects.select_related('profile').all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Admin — Users'])
class AdminUserDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAdminUser]
    queryset = User.objects.select_related('profile').all()

    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return AdminUserUpdateSerializer
        return AdminUserSerializer

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)


@extend_schema(tags=['Admin — Users'])
class AdminToggleUserStatusView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(summary='Toggle user active status (ban/unban)')
    def post(self, request, pk):
        try:
            user = User.objects.get(id=pk)
        except User.DoesNotExist:
            return api_response(message='User not found.', status_str='error', http_status=404)

        user.is_active = not user.is_active
        user.save()
        action = 'activated' if user.is_active else 'deactivated'
        return api_response(
            data={'is_active': user.is_active},
            message=f'User {action} successfully.',
        )


@extend_schema(tags=['Admin — Users'])
class AdminCreditWalletView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminCreditWalletSerializer, summary='Manually credit a user wallet')
    def post(self, request):
        serializer = AdminCreditWalletSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        try:
            user = User.objects.get(id=serializer.validated_data['user_id'])
        except User.DoesNotExist:
            return api_response(message='User not found.', status_str='error', http_status=404)

        amount = serializer.validated_data['amount']
        description = serializer.validated_data['description']

        with db_transaction.atomic():
            profile = lock_profile(user)
            profile.wallet_balance += amount
            profile.total_earned += amount
            profile.save(update_fields=['wallet_balance', 'total_earned', 'updated_at'])

            Transaction.objects.create(
                user=user,
                type='roi',
                amount=amount,
                status='success',
                description=description,
            )

            Notification.objects.create(
                user=user,
                title='Wallet Credited',
                message=f'${amount} has been credited to your wallet. {description}',
                type='success',
            )

            send_email_after_commit(
                subject='Wallet Credited - PipsTrust',
                template_name='wallet_created',
                context={
                    'user': user,
                    'user_name': user.full_name,
                    'amount': amount,
                    'description': description,
                    'new_balance': profile.wallet_balance,
                },
                recipient_list=[user.email],
            )

        return api_response(message=f'${amount} credited to {user.email} successfully.')


# ─── Deposit Management ───────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Deposits'])
class AdminDepositListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminDepositSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'payment_method']
    search_fields = ['user__email', 'transaction_hash']
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        return Deposit.objects.select_related('user').all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Admin — Deposits'])
class AdminDepositDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminDepositSerializer
    queryset = Deposit.objects.select_related('user').all()


@extend_schema(tags=['Admin — Deposits'])
class AdminDepositActionView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminDepositActionSerializer, summary='Approve or reject a deposit')
    def post(self, request, pk):
        serializer = AdminDepositActionSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        action = serializer.validated_data['action']
        admin_note = serializer.validated_data.get('admin_note', '')

        try:
            if action == 'approve':
                confirm_deposit(pk, admin_note)  # credits wallet_balance + total_deposited atomically
                return api_response(message='Deposit approved and credited to the user wallet.')
            reject_deposit(pk, admin_note)
            return api_response(message='Deposit rejected.')
        except DepositNotPending:
            return api_response(message='Deposit not found or already processed.', status_str='error', http_status=404)


# ─── Withdrawal Management ────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Withdrawals'])
class AdminWithdrawalListView(generics.ListAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminWithdrawalSerializer
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'method']
    search_fields = ['user__email']
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        return Withdrawal.objects.select_related('user').all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Admin — Withdrawals'])
class AdminWithdrawalDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminWithdrawalSerializer
    queryset = Withdrawal.objects.select_related('user').all()


@extend_schema(tags=['Admin — Withdrawals'])
class AdminWithdrawalActionView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminWithdrawalActionSerializer, summary='Approve or reject a withdrawal')
    def post(self, request, pk):
        serializer = AdminWithdrawalActionSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        action = serializer.validated_data['action']
        admin_note = serializer.validated_data.get('admin_note', '')

        try:
            if action == 'approve':
                complete_withdrawal(pk, admin_note)
                return api_response(message='Withdrawal approved and marked as completed.')
            reject_withdrawal(pk, admin_note)
            return api_response(message='Withdrawal rejected and amount refunded to user wallet.')
        except WithdrawalNotActionable:
            return api_response(message='Withdrawal not found or not ready for action', status_str='error', http_status=404)


# ─── Investment Plan Management ───────────────────────────────────────────────
@extend_schema(tags=['Admin — Plans'])
class AdminPlanListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminInvestmentPlanSerializer
    queryset = InvestmentPlan.objects.all()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)
        self.perform_create(serializer)
        return api_response(data=serializer.data, message='Investment plan created.', http_status=201)


@extend_schema(tags=['Admin — Plans'])
class AdminPlanDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAdminUser]
    serializer_class = AdminInvestmentPlanSerializer
    queryset = InvestmentPlan.objects.all()


# ─── Broadcast Email ─────────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Broadcast'])
class AdminBroadcastView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminBroadcastSerializer, summary='Broadcast email to users')
    def post(self, request):
        serializer = AdminBroadcastSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        target = serializer.validated_data['target']
        subject = serializer.validated_data['subject']
        message = serializer.validated_data['message']

        if target == 'all':
            users = User.objects.filter(is_active=True)
        elif target == 'verified':
            users = User.objects.filter(is_active=True, is_verified=True)
        elif target == 'active_investors':
            user_ids = Investment.objects.filter(status='active').values_list('user_id', flat=True)
            users = User.objects.filter(id__in=user_ids, is_active=True)
        else:
            users = User.objects.none()

        user_list = list(users.values('id', 'email', 'first_name', 'last_name'))

        sent = failed = 0
        for user_data in user_list:
            full_name = f"{user_data['first_name']} {user_data['last_name']}".strip() or 'Valued Member'
            ok = send_html_email(
                subject=subject,
                template_name='broadcast',
                context={'user_name': full_name, 'message': message},
                recipient_list=[user_data['email']],
            )
            if ok:
                sent += 1
            else:
                failed += 1

        notifications = [
            Notification(user_id=u['id'], title=subject, message=message, type='info')
            for u in user_list
        ]
        Notification.objects.bulk_create(notifications, batch_size=200)

        summary = f'Broadcast emailed to {sent} of {len(user_list)} users.'
        if failed:
            summary += f' {failed} failed - see Email Logs (status = failed) for the reason; you can retry them there.'
        return api_response(
            data={'total': len(user_list), 'sent': sent, 'failed': failed},
            message=summary,
        )


# ─── Email health / logs ─────────────────────────────────────────────────────
@extend_schema(tags=['Admin — Email'])
class AdminEmailLogListView(APIView):
    """Delivery log + configuration check, so admins can see whether emails really go out."""
    permission_classes = [IsAdminUser]

    @extend_schema(summary='List recent emails with delivery status, plus the email configuration report')
    def get(self, request):
        logs = EmailLog.objects.select_related('user').all()
        status_value = request.query_params.get('status')
        if status_value in ('sent', 'failed'):
            logs = logs.filter(status=status_value)
        search = request.query_params.get('search')
        if search:
            logs = logs.filter(Q(recipient__icontains=search) | Q(subject__icontains=search))

        try:
            limit = max(1, min(int(request.query_params.get('limit', 100)), 500))
        except ValueError:
            limit = 100

        since = timezone.now() - timedelta(hours=24)
        recent = EmailLog.objects.filter(created_at__gte=since)
        last_failure = EmailLog.objects.filter(status='failed').first()

        return api_response(data={
            'summary': {
                'sent_24h': recent.filter(status='sent').count(),
                'failed_24h': recent.filter(status='failed').count(),
                'failed_total': EmailLog.objects.filter(status='failed').count(),
                'last_failure_at': last_failure.created_at if last_failure else None,
                'last_failure_error': last_failure.error if last_failure else None,
                'last_failure_hint': explain_smtp_error(last_failure.error) if last_failure else '',
            },
            'config': email_config_report(),
            'logs': AdminEmailLogSerializer(logs[:limit], many=True).data,
        })


@extend_schema(tags=['Admin — Email'])
class AdminEmailLogRetryView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(summary='Re-send a failed email')
    def post(self, request, pk):
        log = EmailLog.objects.select_related('user').filter(id=pk).first()
        if not log:
            return api_response(message='Email log not found.', status_str='error', http_status=404)
        if not log.retryable:
            return api_response(
                message='This email cannot be re-sent (it was already delivered, or it contained a one-time code - ask the user to request a new code).',
                status_str='error', http_status=400,
            )
        ok = retry_email(log)
        log.refresh_from_db()
        if ok:
            return api_response(data=AdminEmailLogSerializer(log).data, message='Email re-sent successfully.')
        return api_response(
            data=AdminEmailLogSerializer(log).data,
            message=f'Still failing: {log.error}',
            status_str='error', http_status=502,
        )


@extend_schema(tags=['Admin — Email'])
class AdminEmailTestView(APIView):
    """Send a real test email and report the exact SMTP error if it fails."""
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminEmailTestSerializer, summary='Send a test email')
    def post(self, request):
        serializer = AdminEmailTestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        to = serializer.validated_data.get('to') or request.user.email
        try:
            send_html_email(
                subject='PipsTrust email test',
                template_name='broadcast',
                context={
                    'user': request.user,
                    'user_name': request.user.full_name,
                    'message': 'This is a test email from the PipsTrust admin panel. If you can read it, email delivery works.',
                },
                recipient_list=[to],
                raise_on_error=True,
            )
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            return api_response(
                data={'error': error, 'hint': explain_smtp_error(error), 'config': email_config_report()},
                message=f'Test email to {to} FAILED: {error}',
                status_str='error', http_status=502,
            )
        return api_response(message=f'Test email sent to {to}. Check the inbox (and the spam folder).')
