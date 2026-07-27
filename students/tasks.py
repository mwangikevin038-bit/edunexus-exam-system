"""
Celery tasks for the premium CSV Student Onboarding Engine.

Processes uploaded CSV files in background micro-batches of 100 records.
Supports upsert via composite unique key: (school_id, admission_no).
"""

import logging

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.core.cache import caches
from django.db import transaction

logger = logging.getLogger("students.csv_tasks")

csv_cache = caches["csv_upload"]

CHUNK_SIZE = 100


def _send_progress(upload_id, data):
    """Push progress to db-backed cache (cross-process safe) and WebSocket."""
    csv_cache.set(f"csv_progress_{upload_id}", data, timeout=600)
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"upload_{upload_id}",
                {"type": "upload_progress", "data": data},
            )
        except Exception:
            pass


def _send_complete(upload_id, data):
    """Push completion to db-backed cache (cross-process safe) and WebSocket."""
    csv_cache.set(f"csv_result_{upload_id}", data, timeout=600)
    csv_cache.delete(f"csv_progress_{upload_id}")
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        try:
            async_to_sync(channel_layer.group_send)(
                f"upload_{upload_id}",
                {"type": "upload_complete", "data": data},
            )
        except Exception:
            pass


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def process_csv_upload(self, upload_id, school_id, rows_json, section='JSS'):
    """
    Main background task. Receives the full mapped CSV payload as a JSON list.
    Processes in chunks of CHUNK_SIZE to keep memory low.
    """
    total = len(rows_json)

    try:
        from students.models import Grade, Guardian, School, Stream, Student
    except Exception as e:
        _send_complete(upload_id, {
            "status": "error", "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total,
            "errors": [f"Import error: {e}"],
            "message": f"Failed to start: {e}",
        })
        return {"status": "error", "errors": [str(e)]}

    try:
        _run_csv_upload(upload_id, school_id, rows_json, section, total)
    except Exception as e:
        logger.exception("process_csv_upload CRASHED: %s", e)
        _send_complete(upload_id, {
            "status": "error",
            "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total,
            "errors": [f"Unexpected worker error: {e}"],
            "message": f"Worker crashed: {e}",
        })
        return {"status": "error", "errors": [str(e)]}


def _run_csv_upload(upload_id, school_id, rows_json, section, total):
    from students.models import Grade, School, Stream, Student

    processed = 0
    created = 0
    updated = 0
    skipped = 0
    errors = []

    _send_progress(upload_id, {
        "status": "processing",
        "processed": 0,
        "total": total,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "message": f"Worker started — processing {total} records...",
    })

    try:
        school = School.objects.get(pk=school_id)
    except School.DoesNotExist:
        errors.append(f"School with id={school_id} does not exist.")
        _send_complete(upload_id, {
            "status": "error",
            "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total, "errors": errors,
            "message": "School not found. Upload aborted.",
        })
        return {"status": "error", "errors": errors}

    # ✅ FIX 4: Pre-fetch valid classes and streams ONCE before the loop
    #           Previously this ran 2 DB queries per row — fatal for large files
    #           Always union with ALL_VALID_CLASSES so classes not yet in the
    #           Grade table (e.g. Grade 3 when only JSS grades exist) are still accepted.
    from students.views.constants import ALL_VALID_CLASSES
    valid_classes = set(
        Grade.all_objects.filter(school=school).values_list("name", flat=True)
    ) | ALL_VALID_CLASSES

    valid_streams = set(
        Stream.all_objects.filter(school=school).values_list("name", flat=True)
    )
    if not valid_streams:
        valid_streams = set(Stream.all_objects.values_list("name", flat=True))

    valid_terms = set(dict(Student.TERM_CHOICES).keys())
    valid_genders = set(dict(Student.GENDER_CHOICES).keys())
    valid_religions = set(dict(Student.RELIGION_CHOICES).keys())

    # ── SECTION GUARD: strict pre-flight ──────────────────────────────────
    # Reject the WHOLE upload if any row has a class_name that does not
    # belong to the active workspace section. This prevents accidentally
    # importing a JSS student into the Primary section, or vice versa.
    from students.views.constants import (
        LOWER_PRIMARY_CLASSES, UPPER_PRIMARY_CLASSES,
        classes_for_section, validate_rows_for_section,
    )
    # PRIMARY workspace accepts BOTH LOWER (Grades 1-3) and UPPER (Grades 4-6)
    # because they're the same institution, just two sub-sections.
    if section == 'PRIMARY':
        allowed_classes = LOWER_PRIMARY_CLASSES | UPPER_PRIMARY_CLASSES
    else:
        allowed_classes = classes_for_section(section)
    if allowed_classes is None or not allowed_classes:
        # Unknown section token — refuse to proceed.
        msg = f"Unknown workspace section {section!r}. Upload aborted."
        errors.append(msg)
        _send_complete(upload_id, {
            "status": "error", "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total, "errors": errors,
            "message": msg,
        })
        return {"status": "error", "errors": errors}
    ok, section_errors, offending = validate_rows_for_section(rows_json, section)
    if not ok:
        msg = (
            f"Upload REJECTED: {len(offending)} class(es) outside the {section} "
            f"workspace ({sorted(offending)}). All rows must belong to {section}. "
            f"Switch workspaces or fix the CSV."
        )
        errors.append(msg)
        # Surface the first 20 offending rows so the user can fix their CSV.
        errors.extend(section_errors[:20])
        _send_complete(upload_id, {
            "status": "error", "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total, "errors": errors,
            "message": msg,
        })
        return {"status": "error", "errors": errors}

    for chunk_start in range(0, total, CHUNK_SIZE):
        chunk = rows_json[chunk_start: chunk_start + CHUNK_SIZE]
        chunk_created, chunk_updated, chunk_skipped, chunk_errors = _process_chunk(
            school, chunk, chunk_start, section,
            valid_classes, valid_streams, valid_terms, valid_genders, valid_religions,
        )
        created += chunk_created
        updated += chunk_updated
        skipped += chunk_skipped
        errors.extend(chunk_errors)
        processed += len(chunk)

        _send_progress(upload_id, {
            "status": "processing",
            "processed": processed,
            "total": total,
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[-10:],
            "message": f"Processed {processed}/{total}...",
        })

    status = "completed" if not errors else "completed_with_errors"
    summary = {
        "status": status,
        "processed": processed,
        "total": total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "message": f"Done: {created} created, {updated} updated, {skipped} skipped out of {total} records.",
    }

    _send_complete(upload_id, summary)
    return summary


def _process_chunk(school, chunk, offset, section,
                   valid_classes, valid_streams, valid_terms,
                   valid_genders, valid_religions):
    """
    Process a single chunk of up to CHUNK_SIZE rows.
    Validation sets are passed in (pre-fetched once) instead of queried per row.
    """
    from students.models import Guardian, Student

    created = 0
    updated = 0
    skipped = 0
    errors = []

    for i, row in enumerate(chunk):
        row_num = offset + i + 2
        s_name = (row.get("student_name") or "").strip()
        p_phone = (row.get("parent_phone") or "").strip()
        p_name  = (row.get("parent_name") or "").strip()
        cls     = (row.get("class_name") or "").strip()
        strm    = (row.get("stream") or "").strip()
        adm     = (row.get("admission_no") or "").strip()

        if p_phone and len(p_phone) == 9 and p_phone[0] in ('7', '1'):
            p_phone = '0' + p_phone

        if not s_name or not p_phone or not p_name or not cls or not strm:
            skipped += 1
            errors.append(f"Row {row_num}: Missing required fields (skipped)")
            continue

        lowercase_valid_classes = {str(c).lower().strip() for c in valid_classes}
        if cls.lower().strip() not in lowercase_valid_classes:
            skipped += 1
            errors.append(f"Row {row_num}: Invalid class '{cls}' (skipped)")
            continue

       
        if strm not in valid_streams:
            skipped += 1
            errors.append(f"Row {row_num}: Invalid stream '{strm}' (skipped)")
            continue

        term       = (row.get("term") or "Term 1").strip() or "Term 1"
        gender     = (row.get("gender") or "Not Specified").strip() or "Not Specified"
        religion   = (row.get("religion") or "None").strip() or "None"
        assessment_no = (row.get("assessment_no") or "").strip()

        if term not in valid_terms:
            term = "Term 1"
        if gender not in valid_genders:
            gender = "Not Specified"
        if religion not in valid_religions:
            religion = "None"

        try:
            with transaction.atomic():
                guardian_obj, _ = Guardian.all_objects.get_or_create(
                    school=school,
                    phone=p_phone,
                    defaults={"name": p_name, "school_section": section},
                )

                if adm:
                    existing = Student.all_objects.filter(
                        school=school, admission_no=adm
                    ).first()
                    if existing:
                        existing.name          = s_name
                        existing.class_name    = cls
                        existing.stream        = strm
                        existing.term          = term
                        existing.guardian      = guardian_obj
                        existing.assessment_no = assessment_no
                        existing.religion      = religion
                        existing.gender        = gender
                        # Auto-set sub_section based on class_name
                        try:
                            grade_num = int(cls.replace('Grade ', ''))
                            existing.sub_section = 'LOWER' if grade_num <= 3 else 'UPPER'
                        except (ValueError, AttributeError):
                            pass
                        existing.save()
                        updated += 1
                    else:
                        # Auto-set sub_section based on class_name
                        sub_section_val = None
                        try:
                            grade_num = int(cls.replace('Grade ', ''))
                            sub_section_val = 'LOWER' if grade_num <= 3 else 'UPPER'
                        except (ValueError, AttributeError):
                            pass
                        Student.all_objects.create(
                            school=school,
                            admission_no=adm,
                            assessment_no=assessment_no,
                            name=s_name,
                            class_name=cls,
                            stream=strm,
                            term=term,
                            guardian=guardian_obj,
                            religion=religion,
                            gender=gender,
                            school_section=section,
                            sub_section=sub_section_val or '',
                        )
                        created += 1
                else:
                    # Auto-set sub_section based on class_name
                    sub_section_val = None
                    try:
                        grade_num = int(cls.replace('Grade ', ''))
                        sub_section_val = 'LOWER' if grade_num <= 3 else 'UPPER'
                    except (ValueError, AttributeError):
                        pass
                    next_no = _next_admission_number(school, school_section=section)
                    Student.all_objects.create(
                        school=school,
                        admission_no=f"{next_no:03}",
                        assessment_no=assessment_no,
                        name=s_name,
                        class_name=cls,
                        stream=strm,
                        term=term,
                        guardian=guardian_obj,
                        religion=religion,
                        gender=gender,
                        school_section=section,
                        sub_section=sub_section_val or '',
                    )
                    created += 1

        except Exception as e:
            skipped += 1
            errors.append(f"Row {row_num}: DB error — {e}")

    return created, updated, skipped, errors


# ╔═══════════════════════════════════════════════════════════════════════════╗
# ║  ADMISSION NUMBER — SINGLE SOURCE OF TRUTH                              ║
# ║  DO NOT duplicate this function. All callers must import from here.      ║
# ║  Run tests/test_admission_numbers.py to verify after any change.         ║
# ╚═══════════════════════════════════════════════════════════════════════════╝
def _next_admission_number(school, school_section=None):
    """Get the next available admission number for a school.

    ADMISSION NUMBER RULES (DO NOT CHANGE):
    ─────────────────────────────────────────
    • PRIMARY section: both LOWER and UPPER sub-sections share ONE number series.
      Example: if highest is 344, next is 345 regardless of sub-section.
    • JSS section: has its OWN independent number series.
      Example: if highest JSS is 450, next JSS is 451.
    • Numbers are 3-digit zero-padded (e.g. "001", "345", "450").
    • The school_section parameter must be the DB value ('PRIMARY' or 'JSS'),
      NOT the workspace token ('LOWER_PRIMARY'). Callers must normalize first.
    """
    from students.models import Student

    # SAFETY: Normalize workspace token to DB value (defensive)
    if school_section == 'LOWER_PRIMARY':
        school_section = 'PRIMARY'

    # Base query: only numeric admission numbers
    qs = Student.all_objects.filter(school=school, admission_no__regex=r'^[0-9]+$')

    if school_section == 'PRIMARY':
        # PRIMARY = both sub-sections share one series
        qs = qs.filter(school_section='PRIMARY', sub_section__in=['LOWER', 'UPPER', None, ''])
    elif school_section == 'JSS':
        # JSS = independent series, no sub_section
        qs = qs.filter(school_section='JSS', sub_section__isnull=True)
    else:
        # Unknown section — return 1 as safe fallback
        return 1

    last = qs.order_by("-admission_no").values_list("admission_no", flat=True).first()
    if last and last.isdigit():
        return int(last) + 1
    return qs.count() + 1