"""
Production rate limiting for authentication and sensitive endpoints.
Uses Django cache — swap to Redis in production via CACHES setting.
"""
import functools
import hashlib
import logging
import time

from django.core.cache import cache
from django.http import HttpResponse
from django.conf import settings

logger = logging.getLogger("students.security.ratelimit")

DEFAULT_MESSAGE = "Too many requests. Please wait and try again."


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _rate_limit_key(group, identifier):
    digest = hashlib.sha256(f"{group}:{identifier}".encode("utf-8")).hexdigest()
    return f"ratelimit:{group}:{digest}"


def is_rate_limited(request, group, max_requests, window_seconds):
    if getattr(settings, "RATELIMIT_DISABLE", False) and settings.DEBUG:
        return False

    identifier = f"{_client_ip(request)}:{getattr(request.user, 'pk', 'anon')}"
    key = _rate_limit_key(group, identifier)
    bucket = cache.get(key)

    now = time.time()
    if not bucket:
        cache.set(key, {"count": 1, "start": now}, window_seconds)
        return False

    if now - bucket["start"] > window_seconds:
        cache.set(key, {"count": 1, "start": now}, window_seconds)
        return False

    if bucket["count"] >= max_requests:
        logger.warning(
            "Rate limit exceeded: group=%s ip=%s path=%s",
            group,
            _client_ip(request),
            request.path,
        )
        return True

    bucket["count"] += 1
    cache.set(key, bucket, window_seconds)
    return False


def rate_limit(group, max_requests=10, window_seconds=60, methods=None):
    methods = methods or ["POST"]

    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method in methods and is_rate_limited(request, group, max_requests, window_seconds):
                return HttpResponse(DEFAULT_MESSAGE, status=429, content_type="text/plain")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
