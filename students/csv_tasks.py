"""
Synchronous fallback for CSV student onboarding.

Runs when Celery/Redis is unavailable. Uses all_objects (bypassing
multi-tenant scoping) since there is no request context in background threads.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.cache import caches
from django.db import transaction

logger = logging.getLogger("students.csv_tasks")

CHUNK_SIZE = 100
csv_cache = caches["csv_upload"]


def _derive_sub_section(class_name):
    """Derive sub_section from class_name."""
    if class_name in ('Grade 1', 'Grade 2', 'Grade 3'):
        return 'LOWER'
    if class_name in ('Grade 4', 'Grade 5', 'Grade 6'):
        return 'UPPER'
    return None


def _send_progress(upload_id, data):
    """Push a progress event to the cache and WebSocket group."""
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
    """Push the final completion event to the cache and WebSocket group."""
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


def run_csv_upload_sync(upload_id, school_id, rows_json, section='JSS'):
    """
    Synchronous fallback when Celery/Redis is unavailable.
    Runs the same processing logic directly in a background thread.
    """
    from students.models import Grade, Guardian, School, Stream, Student

    total = len(rows_json)
    processed = 0
    created = 0
    updated = 0
    skipped = 0
    errors = []

    _send_progress(upload_id, {
        "status": "processing", "processed": 0, "total": total,
        "created": 0, "updated": 0, "skipped": 0, "errors": [],
        "message": f"Processing {total} records (sync)...",
    })

    try:
        school = School.objects.get(pk=school_id)
    except School.DoesNotExist:
        errors.append(f"School with id={school_id} does not exist.")
        _send_complete(upload_id, {
            "status": "error", "processed": 0, "total": total,
            "created": 0, "updated": 0, "skipped": total, "errors": errors,
            "message": "School not found.",
        })
        return {"status": "error", "errors": errors}

    # Pre-fetch valid classes and streams ONCE
    valid_classes = set(
        Grade.all_objects.filter(school=school).values_list("name", flat=True)
    )
    if not valid_classes:
        valid_classes = set(dict(Student.CLASS_CHOICES).keys())

    valid_streams = set(
        Stream.all_objects.filter(school=school).values_list("name", flat=True)
    )
    if not valid_streams:
        valid_streams = set(Stream.all_objects.values_list("name", flat=True))

    valid_terms = set(dict(Student.TERM_CHOICES).keys())
    valid_genders = set(dict(Student.GENDER_CHOICES).keys())
    valid_religions = set(dict(Student.RELIGION_CHOICES).keys())

    # ── SECTION GUARD: strict pre-flight (mirrors tasks.py) ──────────────
    from students.views.constants import classes_for_section, validate_rows_for_section
    allowed_classes = classes_for_section(section)
    if allowed_classes is None:
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
            "status": "processing", "processed": processed, "total": total,
            "created": created, "updated": updated, "skipped": skipped,
            "errors": errors[-10:],
            "message": f"Processed {processed}/{total}...",
        })

    status = "completed" if not errors else "completed_with_errors"
    summary = {
        "status": status, "processed": processed, "total": total,
        "created": created, "updated": updated, "skipped": skipped,
        "errors": errors,
        "message": f"Done: {created} created, {updated} updated, {skipped} skipped out of {total} records.",
    }
    _send_complete(upload_id, summary)
    return summary


def _process_chunk(school, chunk, offset, section,
                   valid_classes, valid_streams, valid_terms,
                   valid_genders, valid_religions):
    """Process a single chunk as ONE big transaction (fast).

    Old code did 1 transaction per row (~1000 round-trips for 1000 rows).
    New code:
      1. Pre-validates every row in memory
      2. Pre-fetches existing guardians and students in 1 query each
      3. Inserts/updates everything in 1 transaction
    """
    from students.models import Guardian, Student

    created = 0
    updated = 0
    skipped = 0
    errors = []

    # Phase 1: parse + validate every row in memory
    parsed = []   # list of dicts ready for DB
    for i, row in enumerate(chunk):
        row_num = offset + i + 2
        s_name = (row.get("student_name") or "").strip()
        p_phone = (row.get("parent_phone") or "").strip()
        p_name = (row.get("parent_name") or "").strip()
        cls = (row.get("class_name") or "").strip()
        strm = (row.get("stream") or "").strip()
        adm = (row.get("admission_no") or "").strip()

        if p_phone and len(p_phone) == 9 and p_phone[0] in ('7', '1'):
            p_phone = '0' + p_phone

        if not s_name or not p_phone or not p_name or not cls or not strm:
            skipped += 1
            errors.append(f"Row {row_num}: Missing required fields (skipped)")
            continue
        # CASE-INSENSITIVE: Excel CSVs often have inconsistent capitalization
        # ("GRADE 3" vs "Grade 3"). Without this, all rows would be rejected.
        if cls.lower() not in {c.lower() for c in valid_classes}:
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
        if term not in valid_terms:    term = "Term 1"
        if gender not in valid_genders: gender = "Not Specified"
        if religion not in valid_religions: religion = "None"

        try:
            grade_num = int(cls.replace('Grade ', ''))
            sub_section_val = 'LOWER' if grade_num <= 3 else 'UPPER'
        except (ValueError, AttributeError):
            sub_section_val = ''

        parsed.append({
            "row_num": row_num,
            "name": s_name, "phone": p_phone, "parent_name": p_name,
            "class": cls, "stream": strm, "admission_no": adm,
            "term": term, "gender": gender, "religion": religion,
            "assessment_no": assessment_no,
            "sub_section": sub_section_val,
        })

    if not parsed:
        return created, updated, skipped, errors

    # Phase 2: bulk-fetch existing records (1 query each instead of N)
    unique_phones = {p["phone"] for p in parsed}
    existing_guardians = {
        g.phone: g for g in
        Guardian.all_objects.filter(school=school, phone__in=unique_phones)
    }
    admission_nos = [p["admission_no"] for p in parsed if p["admission_no"]]
    existing_students = {}
    if admission_nos:
        existing_students = {
            s.admission_no: s for s in
            Student.all_objects.filter(school=school, admission_no__in=admission_nos)
        }

    # Phase 3: do all DB writes in ONE transaction
    new_guardians = []
    new_students = []
    students_to_update = []

    for p in parsed:
        # Guardian: reuse existing or queue for insert
        g = existing_guardians.get(p["phone"])
        if g is None:
            g = Guardian(
                school=school, phone=p["phone"], name=p["parent_name"],
                school_section=section,
            )
            new_guardians.append(g)
            existing_guardians[p["phone"]] = g  # mark as known for in-chunk dedup

    # Bulk-insert new guardians
    if new_guardians:
        Guardian.all_objects.bulk_create(new_guardians, ignore_conflicts=True)
        # Re-fetch to get PKs
        for g in Guardian.all_objects.filter(
            school=school, phone__in=[x.phone for x in new_guardians]
        ):
            existing_guardians[g.phone] = g

    for p in parsed:
        guardian_obj = existing_guardians.get(p["phone"])
        if p["admission_no"] and p["admission_no"] in existing_students:
            # Update
            es = existing_students[p["admission_no"]]
            es.name = p["name"]
            es.class_name = p["class"]
            es.stream = p["stream"]
            es.term = p["term"]
            es.guardian = guardian_obj
            es.assessment_no = p["assessment_no"]
            es.religion = p["religion"]
            es.gender = p["gender"]
            es.sub_section = p["sub_section"]
            students_to_update.append(es)
        elif p["admission_no"]:
            # New with explicit admission_no
            new_students.append(Student(
                school=school, admission_no=p["admission_no"],
                assessment_no=p["assessment_no"], name=p["name"],
                class_name=p["class"], stream=p["stream"],
                term=p["term"], guardian=guardian_obj,
                religion=p["religion"], gender=p["gender"],
                school_section=section, sub_section=p["sub_section"],
            ))
        else:
            # No admission_no — generate one (scoped to section so PRIMARY and
            # JSS each have their own independent number series).
            next_no = _next_admission_number(school, school_section=section, sub_section=p["sub_section"])
            new_students.append(Student(
                school=school, admission_no=f"{next_no:03}",
                assessment_no=p["assessment_no"], name=p["name"],
                class_name=p["class"], stream=p["stream"],
                term=p["term"], guardian=guardian_obj,
                religion=p["religion"], gender=p["gender"],
                school_section=section, sub_section=p["sub_section"],
            ))

    try:
        with transaction.atomic():
            if students_to_update:
                Student.all_objects.bulk_update(
                    students_to_update,
                    ["name", "class_name", "stream", "term", "guardian",
                     "assessment_no", "religion", "gender", "sub_section"],
                    batch_size=100,
                )
                updated = len(students_to_update)
            if new_students:
                Student.all_objects.bulk_create(new_students, batch_size=100, ignore_conflicts=True)
                created = len(new_students)
    except Exception as e:
        # Fall back to row-by-row so we don't lose the whole chunk
        logger.exception("Bulk CSV chunk failed, falling back to row-by-row: %s", e)
        from django.db import IntegrityError
        for p in parsed:
            try:
                with transaction.atomic():
                    g = existing_guardians.get(p["phone"])
                    if not g:
                        g, _ = Guardian.all_objects.get_or_create(
                            school=school, phone=p["phone"],
                            defaults={"name": p["parent_name"], "school_section": section},
                        )
                    if p["admission_no"]:
                        es, created_flag = Student.all_objects.update_or_create(
                            school=school, admission_no=p["admission_no"],
                            defaults={
                                "name": p["name"], "class_name": p["class"],
                                "stream": p["stream"], "term": p["term"],
                                "guardian": g, "assessment_no": p["assessment_no"],
                                "religion": p["religion"], "gender": p["gender"],
                                "school_section": section,
                                "sub_section": p["sub_section"],
                            },
                        )
                    else:
                        next_no = _next_admission_number(school, school_section=section, sub_section=p.get("sub_section"))
                        Student.all_objects.create(
                            school=school, admission_no=f"{next_no:03}",
                            assessment_no=p["assessment_no"], name=p["name"],
                            class_name=p["class"], stream=p["stream"],
                            term=p["term"], guardian=g,
                            religion=p["religion"], gender=p["gender"],
                            school_section=section,
                            sub_section=p["sub_section"],
                        )
                    if created_flag:
                        created += 1
                    else:
                        updated += 1
            except IntegrityError as ie:
                skipped += 1
                errors.append(f"Row {p['row_num']}: {ie}")

    return created, updated, skipped, errors


def _next_admission_number(school, school_section=None, sub_section=None):
    """Get the next available admission number for a school.

    Scoped by (school_section, sub_section) so PRIMARY and JSS each have
    their own independent number series (the user's design rule).
    """
    from students.models import Student

    qs = Student.all_objects.filter(school=school, admission_no__regex=r'^[0-9]+$')
    if school_section is not None:
        qs = qs.filter(school_section=school_section)
    if sub_section is not None:
        qs = qs.filter(sub_section=sub_section)
    elif school_section is not None:
        # For JSS rows, sub_section is NULL. For PRIMARY we accept both
        # LOWER and UPPER (they share the Primary number series).
        if school_section == 'JSS':
            qs = qs.filter(sub_section__isnull=True)
        else:
            qs = qs.filter(sub_section__in=['LOWER', 'UPPER', None, ''])

    last = qs.order_by("-admission_no").values_list("admission_no", flat=True).first()
    if last and last.isdigit():
        return int(last) + 1
    return qs.count() + 1
