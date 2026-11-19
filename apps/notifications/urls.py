from django.urls import path
from .views import NotificationListView, MarkAllReadView, MarkReadView, UnreadCountView, UserEmailLogView

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification-list'),
    path('unread-count/', UnreadCountView.as_view(), name='unread-count'),
    path('email-log/', UserEmailLogView.as_view(), name='user-email-log'),
    path('mark-all-read/', MarkAllReadView.as_view(), name='mark-all-read'),
    path('<uuid:pk>/read/', MarkReadView.as_view(), name='mark-read'),
]
