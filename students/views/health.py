"""
Lightweight health/readiness check for monitoring.

GET /healthz -> 200 OK with a small JSON payload if the system is healthy.
Returns 503 if the DB or Redis is unreachable.

Designed to be hit every 10-30 seconds by an external monitor (UptimeRobot,
Pingdom, etc.) without any auth.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

logger = logging.getLogger("students.health")


@never_cache
@require_GET
def healthz(request):
    """Return 200 if DB + Redis are reachable, 503 otherwise."""
    checks = {}

    # 1. Database
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["db"] = "ok"
    except Exception as e:
        logger.exception("healthz: DB check failed")
        checks["db"] = f"error: {e.__class__.__name__}"

    # 2. Redis cache (the default cache)
    try:
        cache.set("__healthz__", "1", timeout=5)
        ok = cache.get("__healthz__") == "1"
        checks["redis"] = "ok" if ok else "error: cache.set/get mismatch"
    except Exception as e:
        logger.exception("healthz: Redis check failed")
        checks["redis"] = f"error: {e.__class__.__name__}"

    # 3. DEBUG flag (informational only, not part of the health gate)
    checks["debug"] = bool(getattr(settings, "DEBUG", False))

    # Only the string "ok" values count toward health. Skip the debug bool.
    status_values = {k: v for k, v in checks.items() if k != "debug"}
    all_ok = all(v == "ok" for v in status_values.values())
    return JsonResponse(
        {"ok": all_ok, "checks": checks},
        status=200 if all_ok else 503,
    )
