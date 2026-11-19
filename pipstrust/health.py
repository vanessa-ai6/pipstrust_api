from django.http import HttpResponse


class HealthCheckMiddleware:
    """Answers GET /healthz/ before anything else runs.

    Hosting platforms ping a URL to see that the app is alive. Those pings arrive over plain
    HTTP with their own Host header, so they would otherwise be rejected by the HTTPS redirect
    or by ALLOWED_HOSTS and the platform would think the app is down.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get('PATH_INFO') == '/healthz/':
            return HttpResponse('ok', content_type='text/plain')
        return self.get_response(request)
