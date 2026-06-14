"""
Premium CSV student onboarding views.

Handles CSV file upload via a wizard UI, dispatches processing to
a Celery background worker, and provides a polling fallback endpoint
for upload progress tracking.
"""

import uuid as _uuid

import json
from channels.layers import get_channel_layer
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from ..security import get_request_school, get_request_school_section, school_admin_required


# ==============================================================================
# premium_csv_upload_page
# ==============================================================================

@login_required(login_url='login')
@school_admin_required
def premium_csv_upload_page(request):
    """Renders the premium CSV onboarding wizard."""
    return render(request, 'students/premium_csv_upload.html')


# ==============================================================================
# csv_upload_api
# ==============================================================================

@require_POST
@login_required(login_url='login')
@school_admin_required
def csv_upload_api(request):
    """
    Accepts the mapped CSV payload from the frontend, dispatches it to
    the Celery background worker, and returns an upload_id for tracking.
    """
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

    upload_id = _uuid.uuid4().hex
    section = get_request_school_section(request) or 'JSS'

    from ..csv_tasks import process_csv_upload
    process_csv_upload.delay(upload_id, school.pk, rows, section)

    return JsonResponse({
        "status": "ok",
        "upload_id": upload_id,
        "total": len(rows),
        "message": f"Dispatched {len(rows)} records to background worker.",
    })


# ==============================================================================
# csv_upload_progress
# ==============================================================================

@login_required(login_url='login')
@school_admin_required
def csv_upload_progress(request):
    """
    Fallback polling endpoint for progress when WebSocket is unavailable.
    Reads from the InMemoryChannelLayer (or Redis) to get current status.
    """
    upload_id = request.GET.get("upload_id", "")
    if not upload_id:
        return JsonResponse({"status": "error", "error": "Missing upload_id"}, status=400)

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return JsonResponse({
            "status": "processing",
            "processed": 0, "total": 0,
            "created": 0, "updated": 0, "skipped": 0,
            "errors": [],
            "message": "Channel layer not available.",
        })

    return JsonResponse({
        "status": "processing",
        "processed": 0, "total": 0,
        "created": 0, "updated": 0, "skipped": 0,
        "errors": [],
        "message": "Waiting for progress data...",
    })
