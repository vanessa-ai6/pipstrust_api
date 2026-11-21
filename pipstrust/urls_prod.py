"""URLs for production: the normal urls.py, plus serving uploaded files, minus the public API docs."""
from decouple import config
from django.conf import settings
from django.urls import re_path
from django.views.static import serve

from .urls import urlpatterns as _base_urlpatterns

# /api/docs/, /api/redoc/ and /api/schema/ describe every endpoint to anyone. Off unless ENABLE_API_DOCS=True.
_hidden = set() if config('ENABLE_API_DOCS', default=False, cast=bool) else {'schema', 'swagger-ui', 'redoc'}

urlpatterns = [p for p in _base_urlpatterns if getattr(p, 'name', None) not in _hidden]
urlpatterns += [
    # Uploaded files (deposit proofs, avatars). Fine for a small site; move to object storage (S3/R2) if it grows.
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]
