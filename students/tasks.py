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


# ═══════════════════════════════════════════════════════════════════════
# ExamSummary — pre-calculated snapshots for fast report card rendering
# ═══════════════════════════════════════════════════════════════════════

@shared_task(bind=True, max_retries=1, default_retry_delay=15)
def populate_exam_summaries(
    self,
    school_id,
    grade,
    year,
    term,
    exam_name,
    school_section,
    sub_section=None,
):
    """
    Bulk-create/update ExamSummary rows for every student in a grade.

    Triggered by the admin when clicking "Publish" on a subject sheet.
    Computes totals, rankings, PLV, and frozen comments for ALL students
    in the grade — not just those with marks — so ranking is always complete.

    The task is idempotent: re-running it replaces all existing summaries
    for the (school, grade, year, term, exam) key set.

    Args:
        school_id:  int — School.pk
        grade:      str — e.g. "Grade 7"
        year:       int — e.g. 2026
        term:       str — e.g. "Term 2"
        exam_name:  str — e.g. "Opener Assessment"
        school_section: str — 'PRIMARY' or 'JSS'
        sub_section: str or None — 'LOWER' or 'UPPER' (PRIMARY only)
    """
    from collections import defaultdict
    from decimal import Decimal

    from django.db import transaction
    from django.db.models import Count, Q, Sum

    from .models import ExamSummary, GradingConfig, Mark, School, Student

    logger = logging.getLogger("students.exam_summaries")

    try:
        school = School.objects.get(pk=school_id)
    except School.DoesNotExist:
        logger.error("populate_exam_summaries: school_id=%s not found", school_id)
        return {"status": "error", "message": "School not found"}

    # ── 1. Aggregate marks per student (single DB query) ──────────────
    mark_agg = (
        Mark.all_objects.filter(
            school=school,
            student__class_name=grade,
            year=year,
            term=term,
            exam_type=exam_name,
            school_section=school_section,
            sub_section=sub_section,
        )
        .values('student_id')
        .annotate(
            total_marks=Sum('score'),
            total_points=Sum('points'),
            subject_count=Count('subject_id', distinct=True),
        )
    )

    student_marks = {
        row['student_id']: {
            'total_marks': row['total_marks'] or 0,
            'total_points': row['total_points'] or 0,
            'subject_count': row['subject_count'] or 0,
        }
        for row in mark_agg
    }

    # ── 2. Fetch ALL students in the grade ────────────────────────────
    student_filter = {
        'school': school,
        'class_name': grade,
    }
    if school_section == 'PRIMARY' and sub_section:
        student_filter['sub_section'] = sub_section
    elif school_section == 'JSS':
        student_filter['school_section'] = 'JSS'

    all_students = list(Student.all_objects.filter(**student_filter))
    student_ids = [s.id for s in all_students]

    # ── 3. Compute stream_rank and grade_rank in Python ────────────────
    #    Grade rank: across ALL streams in the grade
    #    Stream rank: within each stream

    # Grade-wide ranking (sorted by total_marks DESC, total_points DESC, then student_id for determinism)
    grade_sorted = sorted(
        student_ids,
        key=lambda sid: (
            -(student_marks.get(sid, {}).get('total_marks', 0)),
            -(student_marks.get(sid, {}).get('total_points', 0)),
            sid,
        ),
    )
    grade_ranks = {}
    for rank, sid in enumerate(grade_sorted, start=1):
        grade_ranks[sid] = rank

    # Stream ranking: group by stream, rank within each
    stream_groups = defaultdict(list)
    for s in all_students:
        stream_groups[s.stream].append(s.id)

    stream_ranks = {}
    for stream_name, sids in stream_groups.items():
        sids_sorted = sorted(
            sids,
            key=lambda sid: (
                -(student_marks.get(sid, {}).get('total_marks', 0)),
                -(student_marks.get(sid, {}).get('total_points', 0)),
                sid,
            ),
        )
        for rank, sid in enumerate(sids_sorted, start=1):
            stream_ranks[sid] = rank

    # ── 4. Resolve PLV and frozen comments per student ─────────────────
    # Fetch grading config once
    grading_config = None
    config_lookup = {'school': school, 'school_section': school_section}
    if sub_section:
        config_lookup['sub_section'] = sub_section
    else:
        config_lookup['sub_section__isnull'] = True
    grading_config = GradingConfig.all_objects.filter(**config_lookup).first()
    if not grading_config and sub_section:
        grading_config = GradingConfig.all_objects.filter(
            school=school, school_section=school_section,
        ).first()

    # Fetch the most recent mark per student for frozen comment snapshot
    latest_marks = (
        Mark.all_objects.filter(
            school=school,
            student_id__in=student_ids,
            year=year,
            term=term,
            exam_type=exam_name,
            school_section=school_section,
            sub_section=sub_section,
        )
        .order_by('student_id', '-date_recorded', '-id')
    )
    latest_mark_map = {}
    for m in latest_marks:
        if m.student_id not in latest_mark_map:
            latest_mark_map[m.student_id] = m

    # ── 5. Build ExamSummary objects ───────────────────────────────────
    summaries = []
    for student in all_students:
        sid = student.id
        m = student_marks.get(sid, {})
        total_marks = m.get('total_marks', 0)
        total_points = m.get('total_points', 0)
        subject_count = m.get('subject_count', 0)

        # PLV — must pass school/section to avoid thread-local lookup
        if school_section == 'PRIMARY':
            from .views.helpers import calculate_primary_plv
            overall_plv = calculate_primary_plv(
                total_marks, subject_count,
                sub_section=sub_section, school=school, section=school_section,
            )
        else:
            from .views.helpers import calculate_report_plv
            overall_plv = calculate_report_plv(total_points, total_marks, school=school, section=school_section)

        # Mean points
        mean_points = (
            Decimal(str(total_points)) / Decimal(str(subject_count))
            if subject_count else Decimal('0')
        )

        # Frozen comments from latest mark
        latest = latest_mark_map.get(sid)
        frozen_ct = latest.frozen_class_teacher_comment if latest else ""
        frozen_ht = latest.frozen_headteacher_comment if latest else ""
        frozen_close = latest.frozen_closing_date if latest else None
        frozen_open = latest.frozen_opening_date if latest else None

        summaries.append(ExamSummary(
            school=school,
            student_id=sid,
            term=term,
            year=year,
            exam_name=exam_name,
            school_section=school_section,
            sub_section=sub_section,
            total_marks=total_marks,
            total_points=total_points,
            mean_points=mean_points,
            subject_count=subject_count,
            overall_plv=overall_plv,
            stream_rank=stream_ranks.get(sid, 0),
            grade_rank=grade_ranks.get(sid, 0),
            frozen_class_teacher_comment=frozen_ct,
            frozen_headteacher_comment=frozen_ht,
            frozen_closing_date=frozen_close,
            frozen_opening_date=frozen_open,
        ))

    # ── 6. Atomic bulk upsert ──────────────────────────────────────────
    with transaction.atomic():
        # Delete old summaries for this exam key set (use all_objects — no request context in Celery)
        ExamSummary.all_objects.filter(
            school=school,
            student__class_name=grade,
            year=year,
            term=term,
            exam_name=exam_name,
            school_section=school_section,
            sub_section=sub_section,
        ).delete()

        # Bulk create all new summaries
        if summaries:
            ExamSummary.all_objects.bulk_create(summaries, batch_size=200)

    count = len(summaries)
    logger.info(
        "populate_exam_summaries: school=%s grade=%s %s T%s [%s] — %s summaries created",
        school_id, grade, year, term, exam_name, count,
    )
    return {
        "status": "completed",
        "school_id": school_id,
        "grade": grade,
        "year": year,
        "term": term,
        "exam_name": exam_name,
        "summaries_created": count,
    }