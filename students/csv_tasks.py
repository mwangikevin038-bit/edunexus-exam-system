"""
Celery tasks for the premium CSV Student Onboarding Engine.

Processes uploaded CSV files in background micro-batches of 100 records.
Supports upsert via composite unique key: (school_id, admission_no).
"""

import csv
import io
import json
import logging
import uuid

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger("students.csv_tasks")

CHUNK_SIZE = 100


def _send_progress(upload_id, data):
    """Push a progress event to the WebSocket group."""
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)(
            f"upload_{upload_id}",
            {"type": "upload_progress", "data": data},
        )


def _send_complete(upload_id, data):
    """Push the final completion event to the WebSocket group."""
    channel_layer = get_channel_layer()
    if channel_layer is not None:
        async_to_sync(channel_layer.group_send)(
            f"upload_{upload_id}",
            {"type": "upload_complete", "data": data},
        )


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def process_csv_upload(self, upload_id, school_id, rows_json, section='JSS'):
    """
    Main background task. Receives the full mapped CSV payload as a JSON list.
    Processes in chunks of CHUNK_SIZE to keep memory low.

    Each row in rows_json is a dict with the mapped column values:
        {
            "student_name": "...",
            "admission_no": "...",
            "assessment_no": "...",
            "class_name": "...",
            "stream": "...",
            "term": "...",
            "gender": "...",
            "religion": "...",
            "parent_name": "...",
            "parent_phone": "..."
        }
    """
    from students.models import Guardian, School, Student

    total = len(rows_json)
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
        "message": f"Processing {total} records...",
    })

    try:
        school = School.objects.get(pk=school_id)
    except School.DoesNotExist:
        errors.append(f"School with id={school_id} does not exist.")
        _send_complete(upload_id, {
            "status": "error",
            "processed": 0,
            "total": total,
            "created": 0,
            "updated": 0,
            "skipped": total,
            "errors": errors,
            "message": "School not found. Upload aborted.",
        })
        return {"status": "error", "errors": errors}

    for chunk_start in range(0, total, CHUNK_SIZE):
        chunk = rows_json[chunk_start: chunk_start + CHUNK_SIZE]
        chunk_created, chunk_updated, chunk_skipped, chunk_errors = _process_chunk(
            school, chunk, chunk_start, section
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


def _process_chunk(school, chunk, offset, section='JSS'):
    """Process a single chunk of up to CHUNK_SIZE rows. Returns counts."""
    from students.models import Guardian, Student

    created = 0
    updated = 0
    skipped = 0
    errors = []

    for i, row in enumerate(chunk):
        row_num = offset + i + 2
        s_name = (row.get("student_name") or "").strip()
        p_phone = (row.get("parent_phone") or "").strip()
        p_name = (row.get("parent_name") or "").strip()
        cls = (row.get("class_name") or "").strip()
        strm = (row.get("stream") or "").strip()
        adm = (row.get("admission_no") or "").strip()

        if not s_name or not p_phone or not p_name or not cls or not strm:
            skipped += 1
            errors.append(f"Row {row_num}: Missing required fields (skipped)")
            continue

        valid_classes = dict(Student.CLASS_CHOICES).keys()
        from students.models import Grade, Stream
        valid_streams = set(
            Stream.all_objects.filter(school=school).values_list("name", flat=True)
        )
        if not valid_streams:
            valid_streams = set(
                Stream.all_objects.values_list("name", flat=True)
            )
        if cls not in valid_classes:
            skipped += 1
            errors.append(f"Row {row_num}: Invalid class '{cls}' (skipped)")
            continue
        if strm not in valid_streams:
            skipped += 1
            errors.append(f"Row {row_num}: Invalid stream '{strm}' (skipped)")
            continue

        term = (row.get("term") or "Term 1").strip() or "Term 1"
        gender = (row.get("gender") or "Not Specified").strip() or "Not Specified"
        religion = (row.get("religion") or "None").strip() or "None"
        assessment_no = (row.get("assessment_no") or "").strip()

        valid_terms = dict(Student.TERM_CHOICES).keys()
        if term not in valid_terms:
            term = "Term 1"
        valid_genders = dict(Student.GENDER_CHOICES).keys()
        if gender not in valid_genders:
            gender = "Not Specified"
        valid_religions = dict(Student.RELIGION_CHOICES).keys()
        if religion not in valid_religions:
            religion = "None"

        try:
            with transaction.atomic():
                guardian_obj, _ = Guardian.objects.get_or_create(
                    school=school,
                    phone=p_phone,
                    defaults={"name": p_name, "school_section": section},
                )

                if adm:
                    existing = Student.objects.filter(
                        school=school, admission_no=adm
                    ).first()
                    if existing:
                        existing.name = s_name
                        existing.class_name = cls
                        existing.stream = strm
                        existing.term = term
                        existing.guardian = guardian_obj
                        existing.assessment_no = assessment_no
                        existing.religion = religion
                        existing.gender = gender
                        existing.save()
                        updated += 1
                    else:
                        Student.objects.create(
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
                        )
                        created += 1
                else:
                    next_no = _next_admission_number(school)
                    Student.objects.create(
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
                    )
                    created += 1

        except Exception as e:
            skipped += 1
            errors.append(f"Row {row_num}: DB error — {e}")

    return created, updated, skipped, errors


def _next_admission_number(school):
    """Get the next available admission number for a school."""
    from students.models import Student

    last = (
        Student.objects.filter(school=school)
        .order_by("-admission_no")
        .values_list("admission_no", flat=True)
        .first()
    )
    if last and last.isdigit():
        return int(last) + 1
    count = Student.objects.filter(school=school).count()
    return count + 1
