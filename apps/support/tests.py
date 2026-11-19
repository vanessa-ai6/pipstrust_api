from django.test import TestCase
from rest_framework.test import APIClient

from apps.users.models import User, UserProfile

from .models import SupportTicket


class SupportTicketTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='support@example.com', password='StrongPass123!',
            first_name='Support', last_name='User', is_verified=True,
        )
        UserProfile.objects.create(user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_user_can_create_and_reply_to_own_ticket(self):
        response = self.client.post('/api/support/tickets/', {
            'subject': 'Withdrawal help',
            'category': 'withdrawal',
            'priority': 'normal',
            'message': 'Please check my request.',
        }, format='json')
        self.assertEqual(response.status_code, 201)
        ticket = SupportTicket.objects.get(id=response.data['data']['id'])
        self.assertEqual(ticket.messages.count(), 1)

        reply = self.client.post(f'/api/support/tickets/{ticket.id}/reply/', {
            'message': 'Adding another detail.',
        }, format='json')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(ticket.messages.count(), 2)


class SupportTicketCreationTests(TestCase):
    """Regression: the web app posted to /tickets/create/, which did not exist (404)."""

    PAYLOAD = {
        'subject': 'Withdrawal is still pending', 'category': 'withdrawal',
        'priority': 'normal', 'message': 'Please check my request.',
    }

    def setUp(self):
        self.user = User.objects.create_user(
            email='vanessa@example.com', password='StrongPass123!',
            first_name='Vanessa', last_name='T', is_verified=True,
        )
        UserProfile.objects.create(user=self.user)
        self.admin = User.objects.create_user(
            email='admin@example.com', password='StrongPass123!',
            first_name='Ad', last_name='Min', is_verified=True, is_staff=True,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_both_urls_create_a_ticket(self):
        for url in ('/api/support/tickets/', '/api/support/tickets/create/'):
            res = self.api.post(url, self.PAYLOAD, format='json')
            self.assertEqual(res.status_code, 201, f'{url}: {res.data}')
        self.assertEqual(SupportTicket.objects.filter(user=self.user).count(), 2)

    def test_every_category_and_priority_the_form_offers_is_accepted(self):
        for category in ('account', 'deposit', 'withdrawal', 'investment', 'technical', 'other'):
            for priority in ('low', 'normal', 'high'):
                res = self.api.post('/api/support/tickets/', {**self.PAYLOAD, 'category': category, 'priority': priority}, format='json')
                self.assertEqual(res.status_code, 201, f'{category}/{priority}: {res.data}')

    def test_user_and_admin_are_emailed(self):
        from django.core import mail
        res = self.api.post('/api/support/tickets/create/', self.PAYLOAD, format='json')
        self.assertEqual(res.status_code, 201)
        recipients = {addr for m in mail.outbox for addr in m.to}
        self.assertIn('vanessa@example.com', recipients)   # "we received your ticket"
        self.assertIn('admin@example.com', recipients)     # alert to staff

    def test_ticket_is_still_created_when_email_is_down(self):
        from unittest.mock import patch
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=OSError('smtp down')):
            res = self.api.post('/api/support/tickets/create/', self.PAYLOAD, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(SupportTicket.objects.filter(user=self.user, subject=self.PAYLOAD['subject']).exists())
