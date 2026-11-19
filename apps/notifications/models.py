from django.db import models
from django.conf import settings
import uuid

class Notification(models.Model):
   TYPE = (
      ('info', 'Info'),
      ('success', 'Success'),
      ('warning', 'Warning'),
      ('error', 'Error')
   )

   id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
   user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications')
   title = models.CharField(max_length=200)
   message = models.TextField()
   type = models.CharField(max_length=10, choices=TYPE, default='info')
   is_read = models.BooleanField(default=False)
   created_at = models.DateTimeField(auto_now_add=True)

   def __str__(self):
      return f'{self.title} - {self.user.email}'
   
   class Meta:
      db_table = 'notifications'
      ordering = ['-created_at']


class EmailLog(models.Model):
   """One row per email we tried to send, so failures are never silent.

   Admins can review this in the Django admin (/admin/notifications/emaillog/) or via
   GET /api/admin/email-logs/. Users can see their own via GET /api/notifications/email-log/.
   """
   STATUS = (
      ('sent', 'Sent'),
      ('failed', 'Failed'),
   )
   # Emails whose body must never be stored or re-sent (they carry one-time codes).
   NON_RETRYABLE_TEMPLATES = ('otp',)

   id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
   user = models.ForeignKey(
      settings.AUTH_USER_MODEL, null=True, blank=True,
      on_delete=models.SET_NULL, related_name='email_logs',
   )
   recipient = models.EmailField(db_index=True)
   subject = models.CharField(max_length=255)
   template = models.CharField(max_length=100, blank=True)
   status = models.CharField(max_length=10, choices=STATUS, db_index=True)
   error = models.TextField(blank=True)
   context = models.JSONField(default=dict, blank=True)
   attempts = models.PositiveSmallIntegerField(default=1)
   created_at = models.DateTimeField(auto_now_add=True)
   updated_at = models.DateTimeField(auto_now=True)

   def __str__(self):
      return f'[{self.status}] {self.subject} -> {self.recipient}'

   @property
   def retryable(self):
      return self.status == 'failed' and self.template not in self.NON_RETRYABLE_TEMPLATES

   class Meta:
      db_table = 'email_logs'
      ordering = ['-created_at']
