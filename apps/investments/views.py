from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.utils import timezone
from django.conf import settings
from django.db import transaction as db_transaction
from django.db.models import Sum
from drf_spectacular.utils import extend_schema
from datetime import timedelta

from .models import InvestmentPlan, Deposit, Investment, Transaction
from .serializers import (
    InvestmentPlanSerializer, DepositCreateSerializer, DepositSerializer,
    InvestmentCreateSerializer, InvestmentSerializer,
    TransactionSerializer, DashboardSummarySerializer,
)
from apps.notifications.email_utils import notify_admins, send_html_email
from apps.notifications.models import Notification
from apps.users.views import api_response
from apps.users.models import UserProfile
from .services import settle_matured_investments


# ─── Investment Plans ─────────────────────────────────────────────────────────
@extend_schema(tags=['Plans'])
class InvestmentPlanListView(generics.ListAPIView):
    """List all active investment plans. Public endpoint."""
    serializer_class = InvestmentPlanSerializer
    permission_classes = [AllowAny]
    queryset = InvestmentPlan.objects.filter(is_active=True)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


# ─── Deposits ─────────────────────────────────────────────────────────────────
@extend_schema(tags=['Deposits'])
class DepositCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(request=DepositCreateSerializer, summary='Submit a deposit')
    def post(self, request):
        serializer = DepositCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        with db_transaction.atomic():
            deposit = serializer.save(user=request.user)

            Transaction.objects.create(
                user=request.user,
                type='deposit',
                amount=deposit.amount,
                status='pending',
                description=f'Deposit via {deposit.get_payment_method_display()}',
                reference=str(deposit.id),
            )

            Notification.objects.create(
                user=request.user,
                title='Deposit Received',
                message=f'We received your deposit of ${deposit.amount}. It will be added to your wallet balance once it is confirmed.',
                type='info',
            )

        # Confirmation to the user and an alert to the admins. Failures are recorded in
        # EmailLog (visible in the Django admin) and never block the deposit itself.
        send_html_email(
            subject='Deposit Received - PipsTrust',
            template_name='broadcast',
            context={
                'user': request.user,
                'user_name': request.user.full_name,
                'message': (
                    f'We have received your deposit of ${deposit.amount} via {deposit.get_payment_method_display()}.\n\n'
                    'It is now pending review. Once it is confirmed, the amount will be added to your '
                    'wallet balance and you will receive another email.'
                ),
            },
            recipient_list=[request.user.email],
        )
        notify_admins(
            subject='New Deposit Submitted - PipsTrust',
            message=(
                f'User {request.user.email} submitted a deposit of ${deposit.amount} '
                f'via {deposit.get_payment_method_display()}.\n'
                f'Transaction hash: {deposit.transaction_hash or "not provided"}\n\n'
                'Please review it in the admin panel and approve or reject it.'
            ),
        )

        return api_response(data=DepositSerializer(deposit).data, message='Deposit submitted successfully.', http_status=201)


@extend_schema(tags=['Deposits'])
class DepositListView(generics.ListAPIView):
    """List current user's deposits."""
    serializer_class = DepositSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['status', 'payment_method']
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        return Deposit.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Deposits'])
class DepositDetailView(generics.RetrieveAPIView):
    serializer_class = DepositSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Deposit.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


# ─── Investments ──────────────────────────────────────────────────────────────
@extend_schema(tags=['Investments'])
class InvestmentCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=InvestmentCreateSerializer, summary='Start an investment from a confirmed deposit')
    def post(self, request):
        serializer = InvestmentCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        plan = serializer.validated_data['plan']
        amount = serializer.validated_data['amount']

        duration_days = plan.duration
        if plan.duration_unit == 'weeks':
            duration_days = plan.duration * 7
        elif plan.duration_unit == 'months':
            duration_days = plan.duration * 30

        now = timezone.now()
        expected_return = amount * (plan.roi_percentage / 100) + amount

        with db_transaction.atomic():
            # Lock and re-check the wallet so concurrent requests cannot overspend.
            profile = UserProfile.objects.select_for_update().get(user=request.user)
            if profile.wallet_balance < amount:
                return api_response(
                    message=f'Insufficient wallet balance. Available: ${profile.wallet_balance}.',
                    status_str='error',
                    http_status=400,
                )
            profile.wallet_balance -= amount
            profile.save(update_fields=['wallet_balance', 'updated_at'])

            investment = Investment.objects.create(
                user=request.user,
                plan=plan,
                amount=amount,
                roi_percentage=plan.roi_percentage,
                expected_return=expected_return,
                starts_at=now,
                ends_at=now + timedelta(days=duration_days),
            )

            Transaction.objects.create(
                user=request.user,
                type='investment',
                amount=amount,
                status='success',
                description=f'Investment in {plan.name} plan',
                reference=str(investment.id),
            )

        send_html_email(
            subject='Investment Started - PipsTrust',
            template_name='investment_started',
            context={
                'user': request.user,
                'user_name': request.user.full_name,
                'plan_name': plan.name,
                'amount': amount,
                'roi_percentage': plan.roi_percentage,
                'expected_return': expected_return,
                'start_date': investment.starts_at.strftime('%B %d, %Y'),
                'end_date': investment.ends_at.strftime('%B %d, %Y'),
            },
            recipient_list=[request.user.email],
        )

        return api_response(data=InvestmentSerializer(investment).data, message='Investment started successfully.', http_status=201)


@extend_schema(tags=['Investments'])
class InvestmentListView(generics.ListAPIView):
    serializer_class = InvestmentSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['status']
    ordering_fields = ['created_at', 'amount', 'ends_at']

    def get_queryset(self):
        return Investment.objects.filter(user=self.request.user).select_related('plan')

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


@extend_schema(tags=['Investments'])
class InvestmentDetailView(generics.RetrieveAPIView):
    serializer_class = InvestmentSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Investment.objects.filter(user=self.request.user).select_related('plan')

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


# ─── Transactions ─────────────────────────────────────────────────────────────
@extend_schema(tags=['Transactions'])
class TransactionListView(generics.ListAPIView):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = ['type', 'status']
    search_fields = ['description', 'reference']
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        return Transaction.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return api_response(data=serializer.data)


# ─── Dashboard Summary ────────────────────────────────────────────────────────
@extend_schema(tags=['Dashboard'])
class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=DashboardSummarySerializer, summary='Get user dashboard summary')
    def get(self, request):
        user = request.user
        settle_matured_investments(user=user)
        profile = UserProfile.objects.get(user=user)

        total_deposited = user.deposits.filter(status='confirmed').aggregate(
            total=Sum('amount')
        )['total'] or 0
        total_withdrawn = user.withdrawal.filter(status='completed').aggregate(
            total=Sum('amount')
        )['total'] or 0
        total_invested = user.investments.aggregate(total=Sum('amount'))['total'] or 0
        pending_deposit_amount = user.deposits.filter(status='pending').aggregate(total=Sum('amount'))['total'] or 0

        data = {
            'wallet_balance': profile.wallet_balance,
            'total_deposited': total_deposited,
            'total_withdrawn': total_withdrawn,
            'total_earned': profile.total_earned,
            'total_invested': total_invested,
            'active_investments': user.investments.filter(status='active').count(),
            'completed_investments': user.investments.filter(status='completed').count(),
            'pending_deposits': user.deposits.filter(status='pending').count(),
            'pending_deposit_amount': pending_deposit_amount,
            'pending_withdrawals': user.withdrawal.filter(
                status__in=['pending', 'otp_verified', 'processing']
            ).count(),
            'referral_count': user.referrals.count(),
        }
        return api_response(data=DashboardSummarySerializer(data).data)