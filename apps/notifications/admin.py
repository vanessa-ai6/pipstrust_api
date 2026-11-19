from django.contrib import admin, messages

from .email_utils import retry_email
from .models import EmailLog, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
   list_display = ['title', 'user', 'type', 'is_read', 'created_at']
   list_filter = ['type', 'is_read']
   search_fields = ['user__email', 'title']


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
   """Delivery log. Filter by Status = Failed to see every email that did not go out."""
   list_display = ['created_at', 'status', 'recipient', 'subject', 'template', 'attempts', 'short_error']
   list_filter = ['status', 'template']
   search_fields = ['recipient', 'subject', 'error']
   date_hierarchy = 'created_at'
   readonly_fields = [
      'user', 'recipient', 'subject', 'template', 'status', 'error',
      'context', 'attempts', 'created_at', 'updated_at',
   ]
   actions = ['retry_failed']

   def has_add_permission(self, request):
      return False

   @admin.display(description='Error')
   def short_error(self, obj):
      return (obj.error[:80] + '...') if len(obj.error) > 80 else obj.error

   @admin.action(description='Retry selected failed emails')
   def retry_failed(self, request, queryset):
      ok = failed = skipped = 0
      for log in queryset.select_related('user'):
         if not log.retryable:
            skipped += 1
         elif retry_email(log):
            ok += 1
         else:
            failed += 1
      self.message_user(
         request,
         f'{ok} sent, {failed} still failing, {skipped} skipped (already sent or one-time-code emails).',
         level=messages.SUCCESS if not failed else messages.WARNING,
      )
