from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.db import transaction
from drf_spectacular.utils import extend_schema

from apps.admin_api.permissions import IsAdminUser
from apps.notifications.email_utils import notify_admins, send_email_after_commit, send_html_email
from apps.notifications.models import Notification
from apps.users.views import api_response

from .models import SupportTicket, TicketMessage
from .serializers import (
    AdminTicketUpdateSerializer,
    SupportTicketCreateSerializer,
    SupportTicketSerializer,
    TicketReplySerializer,
)


def _excerpt(text, length=500):
    text = (text or '').strip()
    return text if len(text) <= length else text[:length] + '...'


def _email_user(ticket, subject, message):
    """Email the ticket owner (after the surrounding transaction commits)."""
    send_email_after_commit(
        subject=subject,
        template_name='broadcast',
        context={'user': ticket.user, 'user_name': ticket.user.full_name, 'message': message},
        recipient_list=[ticket.user.email],
    )


@extend_schema(tags=['Support'])
class SupportTicketListCreateView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tickets = SupportTicket.objects.filter(user=request.user).prefetch_related('messages')
        return api_response(data=SupportTicketSerializer(tickets, many=True).data)

    @extend_schema(request=SupportTicketCreateSerializer)
    def post(self, request):
        serializer = SupportTicketCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return api_response(
                message='Validation failed.', status_str='error',
                errors=serializer.errors, http_status=400,
            )
        ticket = serializer.save()
        first_message = ticket.messages.first()

        notify_admins(
            subject=f'New Support Ticket: {ticket.subject}',
            message=(
                f'{request.user.email} opened a {ticket.priority}-priority {ticket.category} ticket '
                f'(#{str(ticket.id)[:8]}).\n\nSubject: {ticket.subject}\n\n'
                f'{_excerpt(first_message.message if first_message else "")}'
            ),
        )
        send_html_email(
            subject=f'We received your support ticket - {ticket.subject}',
            template_name='broadcast',
            context={
                'user': request.user,
                'user_name': request.user.full_name,
                'message': (
                    f'Thanks for contacting PipsTrust support. Your ticket "{ticket.subject}" '
                    f'(#{str(ticket.id)[:8]}) has been received and our team will reply as soon as possible.\n\n'
                    'You can follow the conversation under Support Tickets in your account.'
                ),
            },
            recipient_list=[request.user.email],
        )
        return api_response(
            data=SupportTicketSerializer(ticket).data,
            message='Support ticket created successfully.', http_status=201,
        )


@extend_schema(tags=['Support'])
class SupportTicketDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ticket = SupportTicket.objects.filter(user=request.user, id=pk).prefetch_related('messages').first()
        if not ticket:
            return api_response(message='Ticket not found.', status_str='error', http_status=404)
        return api_response(data=SupportTicketSerializer(ticket).data)


@extend_schema(tags=['Support'])
class SupportTicketReplyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(request=TicketReplySerializer)
    def post(self, request, pk):
        serializer = TicketReplySerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        with transaction.atomic():
            ticket = SupportTicket.objects.select_for_update().filter(user=request.user, id=pk).first()
            if not ticket:
                return api_response(message='Ticket not found.', status_str='error', http_status=404)
            if ticket.status == 'closed':
                return api_response(message='This ticket is closed.', status_str='error', http_status=400)
            TicketMessage.objects.create(ticket=ticket, sender='user', message=serializer.validated_data['message'].strip())
            ticket.status = 'open'
            ticket.save(update_fields=['status', 'updated_at'])

        notify_admins(
            subject=f'New reply on support ticket: {ticket.subject}',
            message=(
                f'{request.user.email} replied on ticket #{str(ticket.id)[:8]} ({ticket.subject}):\n\n'
                f'{_excerpt(serializer.validated_data["message"])}'
            ),
        )
        return api_response(data=SupportTicketSerializer(ticket).data, message='Reply sent.')


@extend_schema(tags=['Admin — Support'])
class AdminSupportTicketListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        tickets = SupportTicket.objects.select_related('user').prefetch_related('messages').all()
        status_value = request.query_params.get('status')
        if status_value:
            tickets = tickets.filter(status=status_value)
        data = SupportTicketSerializer(tickets, many=True).data
        for row, ticket in zip(data, tickets):
            row['user_email'] = ticket.user.email
            row['user_name'] = ticket.user.full_name
        return api_response(data=data)


@extend_schema(tags=['Admin — Support'])
class AdminSupportTicketActionView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=AdminTicketUpdateSerializer)
    def post(self, request, pk):
        serializer = AdminTicketUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(message='Validation failed.', status_str='error', errors=serializer.errors, http_status=400)

        with transaction.atomic():
            ticket = SupportTicket.objects.select_for_update().select_related('user').filter(id=pk).first()
            if not ticket:
                return api_response(message='Ticket not found.', status_str='error', http_status=404)

            message = serializer.validated_data.get('message')
            if message:
                TicketMessage.objects.create(ticket=ticket, sender='admin', message=message.strip())
                ticket.status = 'answered'
                Notification.objects.create(
                    user=ticket.user,
                    title='Support replied',
                    message=f'You have a new reply on “{ticket.subject}”.',
                    type='info',
                )
                _email_user(
                    ticket,
                    f'Support replied to your ticket - {ticket.subject}',
                    f'Our support team replied to your ticket "{ticket.subject}":\n\n{_excerpt(message)}\n\n'
                    'Open Support Tickets in your account to continue the conversation.',
                )
            if 'status' in serializer.validated_data:
                new_status = serializer.validated_data['status']
                if new_status == 'closed' and ticket.status != 'closed':
                    Notification.objects.create(
                        user=ticket.user,
                        title='Support ticket closed',
                        message=f'Your ticket “{ticket.subject}” has been closed.',
                        type='info',
                    )
                    _email_user(
                        ticket,
                        f'Your support ticket was closed - {ticket.subject}',
                        f'Your ticket "{ticket.subject}" (#{str(ticket.id)[:8]}) has been closed. '
                        'If you still need help, open a new ticket from your account.',
                    )
                ticket.status = new_status
            ticket.save(update_fields=['status', 'updated_at'])

        return api_response(data=SupportTicketSerializer(ticket).data, message='Ticket updated.')
