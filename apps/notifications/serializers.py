from rest_framework import serializers
from .models import EmailLog, Notification


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'type', 'is_read', 'created_at']


class UserEmailLogSerializer(serializers.ModelSerializer):
    """What a user may see about their own emails: no technical error details."""

    class Meta:
        model = EmailLog
        fields = ['id', 'subject', 'status', 'created_at']
        read_only_fields = fields
