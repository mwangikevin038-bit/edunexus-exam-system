"""
Premium CSV student onboarding views.

Handles CSV file upload via a wizard UI, dispatches processing to
a Celery background worker (or runs synchronously), and provides
a polling fallback endpoint for upload progress tracking.
"""

import uuid as _uuid
import json
import threading
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..security import get_request_school, get_request_school_section, school_admin_required

logger = logging.getLogger("students.csv_upload")

# In-memory store for sync processing results (fallback when Celery/WS unavailable)
_upload_results = {}
_upload_lock = threading.Lock()


def _json_safe_view(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"status": "error", "error": "Login required.", "login_url": "/login/"}, status=401)
        from ..security.roles import user_has_main_school_admin_override
        if not user_has_main_school_admin_override(request.user):
            return JsonResponse({"status": "error", "error": "School admin access required."}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


# ==============================================================================
# premium_csv_upload_page
# ==============================================================================

@login_required(login_url='login')
@school_admin_required
def premium_csv_upload_page(request):
    return render(request, 'students/premium_csv_upload.html')


# ==============================================================================
# csv_upload_api
# ==============================================================================

@csrf_exempt
@_json_safe_view
@require_POST
def csv_upload_api(request):
    school = get_request_school(request)
    if not school:
        return JsonResponse({"status": "error", "error": "School context required."}, status=403)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "error": "Invalid JSON."}, status=400)

    rows = payload.get("rows")
    if not rows or not isinstance(rows, list):
        return JsonResponse({"status": "error", "error": "Missing 'rows' array."}, status=400)

    if len(rows) > 10000:
        return JsonResponse({"status": "error", "error": "Maximum 10,000 rows per upload."}, status=400)

    # ── SECTION GUARD: pre-dispatch strict validation ─────────────────────
    # Reject the upload BEFORE spinning up the worker if any row has a
    # class_name that doesn't belong to the active workspace. The Celery
    # task does the same check, but catching it here means a faster
    # failure with no background work queued.
    from ..views.constants import classes_for_section, validate_rows_for_section
    section = get_request_school_section(request) or 'JSS'
    allowed = classes_for_section(section)
    if allowed is None:
        return JsonResponse({
            "status": "error",
            "error": f"Unknown workspace section {section!r}. Pick a valid workspace before uploading.",
        }, status=400)
    ok, section_errors, offending = validate_rows_for_section(rows, section)
    if not ok:
        return JsonResponse({
            "status": "error",
            "error": (
                f"Upload rejected: {len(offending)} class(es) outside the {section} "
                f"workspace ({sorted(offending)}). Switch workspaces or fix the CSV."
            ),
            "details": section_errors[:20],
        }, status=400)

    upload_id = _uuid.uuid4().hex

    from ..tasks import process_csv_upload as celery_task
    from ..csv_tasks import run_csv_upload_sync
    try:
        celery_task.delay(upload_id, school.pk, rows, section)
    except Exception as celery_err:
        logger.warning("Celery unavailable, processing synchronously: %s", celery_err)

        def _run_sync():
            result = run_csv_upload_sync(upload_id, school.pk, rows, section)
            with _upload_lock:
                _upload_results[upload_id] = result

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

    with _upload_lock:
        _upload_results[upload_id] = {
            "status": "processing",
            "processed": 0, "total": len(rows),
            "created": 0, "updated": 0, "skipped": 0,
            "errors": [],
            "message": "Processing in background thread...",
        }

    return JsonResponse({
        "status": "ok",
        "upload_id": upload_id,
        "total": len(rows),
        "message": f"Dispatched {len(rows)} records to background worker.",
    })


# ==============================================================================
# csv_upload_progress
# ==============================================================================

@csrf_exempt
@_json_safe_view
def csv_upload_progress(request):
    upload_id = request.GET.get("upload_id", "")
    if not upload_id:
        return JsonResponse({"status": "error", "error": "Missing upload_id"}, status=400)

    # Check the csv_upload cache (written by tasks.py)
    try:
        from django.core.cache import caches
        csv_cache = caches["csv_upload"]
        result = csv_cache.get(f"csv_result_{upload_id}")
        if result:
            return JsonResponse(result)
        progress = csv_cache.get(f"csv_progress_{upload_id}")
        if progress:
            return JsonResponse(progress)
    except Exception:
        pass

    # Fallback to in-memory results (sync thread)
    with _upload_lock:
        result = _upload_results.get(upload_id)

    if result:
        return JsonResponse(result)

    return JsonResponse({
        "status": "processing",
        "processed": 0, "total": 0,
        "created": 0, "updated": 0, "skipped": 0,
        "errors": [],
        "message": "Waiting for progress data...",
    })
