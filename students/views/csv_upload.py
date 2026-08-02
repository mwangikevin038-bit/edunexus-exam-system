"""
Premium CSV student onboarding views.

Dispatches processing to a Celery worker via Redis.
Progress is tracked purely through Redis cache.
"""

import uuid as _uuid
import json
import logging
import time

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..security import get_request_school, get_request_school_section, school_admin_required

logger = logging.getLogger("students.csv_upload")


def _json_safe_view(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"status": "error", "error": "Login required.", "login_url": "/login/"}, status=401)
        from ..security.roles import user_has_main_school_admin_override
        if not user_has_main_school_admin_override(request.user):
            return JsonResponse({"status": "error", "error": "School admin access required."}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


@login_required(login_url='login')
@school_admin_required
def premium_csv_upload_page(request):
    return render(request, 'students/premium_csv_upload.html')


@csrf_exempt
@_json_safe_view
@require_POST
def csv_upload_api(request):
    school = get_request_school(request)
    if not school:
        return JsonResponse({"status": "error", "error": "School context required."}, status=403)

    # ── RATE LIMIT: 3 uploads per minute sliding window ────────────────────
    user_id = request.user.id if request.user.is_authenticated else request.META.get('REMOTE_ADDR')
    cache_key = f"rate_limit_csv_upload_{user_id}"
    request_history = cache.get(cache_key, [])
    now = time.time()
    # Filter out requests older than 60 seconds
    request_history = [t for t in request_history if now - t < 60.0]
    if len(request_history) >= 3:
        return JsonResponse({"status": "error", "error": "Too many upload requests. Please wait a minute."}, status=429)
    request_history.append(now)
    cache.set(cache_key, request_history, timeout=65)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON."}, status=400)

    rows = payload.get("rows")
    if not rows or not isinstance(rows, list):
        return JsonResponse({"status": "error", "error": "Missing 'rows' array."}, status=400)

    if len(rows) > 10000:
        return JsonResponse({"status": "error", "error": "Maximum 10,000 rows per upload."}, status=400)

    PRIMARY_ALL = {'Grade 1', 'Grade 2', 'Grade 3', 'Grade 4', 'Grade 5', 'Grade 6'}
    JSS_ALL = {'Grade 7', 'Grade 8', 'Grade 9'}
    LOWER_PRIMARY_ONLY = {'Grade 1', 'Grade 2', 'Grade 3'}

    section = get_request_school_section(request) or 'JSS'
    if section == 'PRIMARY':
        allowed = PRIMARY_ALL
    elif section == 'LOWER_PRIMARY':
        allowed = LOWER_PRIMARY_ONLY
    elif section == 'JSS':
        allowed = JSS_ALL
    else:
        allowed = None

    if allowed is None:
        return JsonResponse({
            "status": "error",
            "error": f"Unknown workspace section {section!r}. Pick a valid workspace before uploading.",
        }, status=400)

    allowed_lower = {a.lower() for a in allowed}
    offending = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        cls = (row.get('class_name') or '').strip()
        if cls and cls.lower() not in allowed_lower:
            offending.add(cls)

    if offending:
        sample_errors = [
            f"Row {i+2}: class '{r.get('class_name','')}' does not belong to workspace '{section}'. Allowed: {sorted(allowed)}"
            for i, r in enumerate(rows[:20])
            if (r.get('class_name') or '').strip().lower() not in allowed_lower
        ]
        return JsonResponse({
            "status": "error",
            "error": (
                f"Upload rejected: {len(offending)} class(es) outside the {section} "
                f"workspace ({sorted(offending)}). Switch workspaces or fix the CSV."
            ),
            "details": sample_errors,
        }, status=400)

    upload_id = _uuid.uuid4().hex

    DB_SECTION_MAP = {
        'LOWER_PRIMARY': 'PRIMARY',
        'PRIMARY': 'PRIMARY',
        'JSS': 'JSS',
    }
    db_section = DB_SECTION_MAP.get(section, section)

    # Write initial progress to Redis so the polling endpoint can read it
    from django.core.cache import caches
    csv_cache = caches["csv_upload"]
    csv_cache.set(f"csv_progress_{upload_id}", {
        "status": "processing",
        "processed": 0, "total": len(rows),
        "created": 0, "updated": 0, "skipped": 0,
        "errors": [],
        "message": "Queued for processing...",
    }, timeout=600)

    from ..tasks import process_csv_upload as celery_task
    try:
        celery_task.apply_async(
            args=[upload_id, school.pk, rows, db_section],
            queue='csv_upload',
        )
    except Exception as e:
        logger.exception("Celery dispatch failed: %s", e)
        csv_cache.set(f"csv_result_{upload_id}", {
            "status": "error",
            "processed": 0, "total": len(rows),
            "created": 0, "updated": 0, "skipped": len(rows),
            "errors": [f"Could not start background worker: {e}"],
            "message": f"Background worker unavailable: {e}",
        }, timeout=600)
        csv_cache.delete(f"csv_progress_{upload_id}")
        return JsonResponse({
            "status": "error",
            "error": f"Could not start background worker. Ensure Celery is running. Error: {e}",
        }, status=503)

    return JsonResponse({
        "status": "ok",
        "upload_id": upload_id,
        "total": len(rows),
        "message": f"Dispatched {len(rows)} records to background worker.",
    })


@csrf_exempt
@_json_safe_view
def csv_upload_progress(request):
    upload_id = request.GET.get("upload_id", "")
    if not upload_id:
        return JsonResponse({"status": "error", "error": "Missing upload_id"}, status=400)

    from django.core.cache import caches
    csv_cache = caches["csv_upload"]

    # Check for completed result first
    result = csv_cache.get(f"csv_result_{upload_id}")
    if result:
        return JsonResponse(result)

    # Check for in-progress updates
    progress = csv_cache.get(f"csv_progress_{upload_id}")
    if progress:
        return JsonResponse(progress)

    return JsonResponse({
        "status": "error",
        "processed": 0, "total": 0,
        "created": 0, "updated": 0, "skipped": 0,
        "errors": ["Upload ID not found. It may have expired."],
        "message": "No progress data found for this upload.",
    })
