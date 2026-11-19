# Load the Celery app when Django starts so @shared_task / .delay() use our settings
# (broker URL, eager mode, ...). Without this import they fall back to a default
# AMQP broker and silently fail.
from .celery import app as celery_app

__all__ = ('celery_app',)