from django.core.management.base import BaseCommand
from django.core.mail import EmailMultiAlternatives
from django.conf import settings

from apps.notifications.email_utils import (
    email_config_report, explain_smtp_error, get_admin_emails,
)


class Command(BaseCommand):
    help = (
        'Check the email setup and send a real test email. '
        'Usage: python manage.py check_email --to you@example.com'
    )

    def add_arguments(self, parser):
        parser.add_argument('--to', help='Address to send the test email to (default: first admin recipient)')
        parser.add_argument('--config-only', action='store_true', help='Only print the configuration, do not send.')

    def handle(self, *args, **options):
        report = email_config_report()
        self.stdout.write(self.style.MIGRATE_HEADING('Email configuration'))
        for key in ('backend', 'host', 'port', 'use_tls', 'use_ssl', 'timeout_seconds',
                    'smtp_user', 'password_set', 'from_email', 'frontend_url'):
            self.stdout.write(f'  {key:16} {report[key]}')
        self.stdout.write(f"  {'admin_recipients':16} {', '.join(report['admin_recipients']) or '(none)'}")

        for warning in report['warnings']:
            self.stdout.write(self.style.WARNING(f'  ! {warning}'))

        if options['config_only']:
            return

        to = options['to'] or (get_admin_emails() or [None])[0]
        if not to:
            self.stderr.write(self.style.ERROR('No recipient: pass --to you@example.com'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f'\nSending test email to {to} ...'))
        try:
            msg = EmailMultiAlternatives(
                subject='PipsTrust email test',
                body='If you can read this, PipsTrust email delivery works.',
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[to],
            )
            msg.send(fail_silently=False)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            self.stderr.write(self.style.ERROR(f'FAILED: {error}'))
            hint = explain_smtp_error(error)
            if hint:
                self.stderr.write(self.style.WARNING(f'Hint: {hint}'))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS(
            'Sent. Check the inbox (and the spam folder) of the recipient.'
        ))
