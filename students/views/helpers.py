"""
Helper functions for the students views module.

Provides utilities for authentication, access control, student ordering,
subject-aware queries, and performance-level calculations used by the
various view layers.
"""

import bisect
import random
import secrets
import string

from django.core.cache import cache
from django.db.models import Avg, Count, Q, Sum, IntegerField, Value, F, Window
from django.db.models.functions import Cast, Coalesce, DenseRank
from django.db.models.fields import FloatField

from .constants import (
    ASSESSMENT_SLUG_MAP,
    GRADE_CHOICES,
    RELIGION_SUBJECTS,
    RELIGION_TAG,
)
from ..models import Mark, MarkSubmission, Student, SubjectAssignment, Teacher
from ..school_scope import get_current_school, get_current_school_section
from ..security import user_has_main_school_admin_override


# ── Cache TTL and key helpers ────────────────────────────────────────────────
_CACHE_TTL = 3600  # 1 hour

def _leaderboard_cache_key(school_id, class_name, stream, year, term, assessment):
    return f"lb_{school_id}_{class_name}_{stream}_{year}_{term}_{assessment}"

def _class_avg_cache_key(school_id, class_name, stream, year, term, assessment):
    return f"avg_{school_id}_{class_name}_{stream}_{year}_{term}_{assessment}"

def invalidate_report_caches(school_id, class_name, stream, year, term, assessment):
    """Call this whenever marks are uploaded/changed for a class/stream/exam."""
    cache.delete(_leaderboard_cache_key(school_id, class_name, stream, year, term, assessment))
    cache.delete(_class_avg_cache_key(school_id, class_name, stream, year, term, assessment))


def get_cached_class_averages(school, class_name, stream, year, term, assessment, published_subjects_qs):
    """
    Return {subject_code: avg_score} for a class/stream, cached in Redis.
    Only hits the DB on cache miss.
    """
    key = _class_avg_cache_key(school.pk, class_name, stream, year, term, assessment)
    cached = cache.get(key)
    if cached is not None:
        return cached

    class_subject_avgs = (
        Mark.all_objects.filter(
            school=school,
            student__class_name=class_name,
            student__stream=stream,
            year=year, term=term, exam_type=assessment,
            subject__in=published_subjects_qs,
        )
        .exclude(is_absent=True)
        .values('subject__code')
        .annotate(avg_score=Avg('score'))
    )
    avg_map = {row['subject__code']: round(row['avg_score'], 1) for row in class_subject_avgs}
    cache.set(key, avg_map, _CACHE_TTL)
    return avg_map



def generate_default_password():
    """Generate a random 8-digit numeric password for new teachers."""
    return ''.join(secrets.choice(string.digits) for _ in range(8))


def get_published_subject_codes(class_name, stream, year, term, exam_name, sub_section=None, is_admin=False):
    """
    Return subject codes that have been formally published by the school admin.
    Official analysis and report cards should only use these finalized sheets.
    Admin users see all sections; teachers are scoped by their workspace section.
    """
    school = get_current_school()
    section = get_current_school_section()
    filters = dict(
        class_name=class_name,
        stream=stream,
        year=year,
        term=term,
        exam_name=exam_name,
        status="published",
    )
    if school:
        filters['school'] = school
    if not is_admin:
        if sub_section == 'LOWER':
            filters['school_section'] = 'PRIMARY'
            filters['sub_section'] = 'LOWER'
        elif sub_section == 'UPPER':
            filters['school_section'] = 'PRIMARY'
            filters['sub_section'] = 'UPPER'
        elif section == 'LOWER_PRIMARY':
            filters['school_section'] = 'PRIMARY'
            filters['sub_section'] = 'LOWER'
        elif section == 'PRIMARY':
            filters['school_section'] = 'PRIMARY'
            filters['sub_section'] = 'UPPER'
        elif section == 'JSS':
            filters['school_section'] = 'JSS'
    return set(
        MarkSubmission.all_objects.filter(**filters).values_list("subject__code", flat=True)
    )


def get_published_contexts_for_user(user, require_class_teacher=False, sub_section=None):
    """
    Return all published assessment contexts visible to any logged-in user.
    Results lists are read-only and visible to every authenticated teacher.
    Report cards call this with require_class_teacher=True for private scoping.
    """
    school = get_current_school()
    section = get_current_school_section()
    qs = MarkSubmission.all_objects.filter(status="published")
    if school:
        qs = qs.filter(school=school)

    if sub_section == 'LOWER':
        qs = qs.filter(school_section='PRIMARY', sub_section='LOWER')
    elif sub_section == 'UPPER':
        qs = qs.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'LOWER_PRIMARY':
        qs = qs.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        qs = qs.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        qs = qs.filter(school_section='JSS')
    teacher = get_teacher_for_user(user)
    class_scope = get_class_teacher_scope(teacher)

    if require_class_teacher and not user_has_main_school_admin_override(user):
        if not class_scope:
            return []
        qs = qs.filter(class_name=class_scope[0], stream=class_scope[1])

    contexts = list(
        qs.values("year", "term", "exam_name", "class_name", "stream")
        .annotate(subject_count=Count("subject", distinct=True))
        .order_by("-year", "term", "class_name", "stream", "exam_name")
    )
    for item in contexts:
        item["context_key"] = (
            f"{item['year']}|{item['term']}|{item['exam_name']}|"
            f"{item['class_name']}|{item['stream']}"
        )
        item["assessment_slug"] = item["exam_name"]
    return contexts


def get_stream_submission_summary(class_name, stream, exam):
    """
    Build a per-stream assessment summary used by admin review/publish screens.
    Religion-aware: CRE/IRE missing counts only check tagged students.
    Works for all grades (7, 8, 9) and all streams universally.
    """
    school = get_current_school()
    assignment_filters = dict(class_name=class_name, stream=stream)
    if school:
        assignment_filters['school'] = school

    assignments = (
        SubjectAssignment.all_objects.filter(**assignment_filters)
        .select_related("teacher_profile", "teacher_profile__user", "subject")
        .order_by("subject__code")
    )
    rows = []
    totals = {
        "subjects": assignments.count(),
        "submitted": 0,
        "approved": 0,
        "published": 0,
        "returned": 0,
        "missing_subjects": 0,
        "missing_scores": 0,
        "captured": 0,
        "expected": 0,
        "absent": 0,
    }

    for assignment in assignments:
        # submission_filters is built here, inside the loop, so
        # assignment.subject is always defined when accessed.
        submission_filters = dict(
            subject=assignment.subject,
            class_name=class_name,
            stream=stream,
            exam_name=exam.name,
            term=exam.term,
            year=exam.year,
            school_section=assignment.school_section,
            sub_section=assignment.sub_section,
        )
        if school:
            submission_filters['school'] = school

        expected_count = get_religion_aware_student_count(
            class_name,
            stream,
            assignment.subject,
        )
        marks_qs = get_subject_marks(
            class_name,
            stream,
            assignment.subject,
            exam.term,
            exam.name,
            exam.year,
        )
        captured_count = marks_qs.count()
        absent_count = marks_qs.filter(is_absent=True).count()
        missing_count = max(expected_count - captured_count, 0)

        submission = MarkSubmission.all_objects.filter(
            teacher=assignment.teacher_profile,
            **submission_filters,
        ).first()

        if submission:
            totals[submission.status] = totals.get(submission.status, 0) + 1
            status_key = submission.status
            status_label = "Returned" if submission.status == "returned" else submission.get_status_display()
        elif captured_count == 0:
            totals["missing_subjects"] += 1
            status_key = "not_started"
            status_label = "Not Started"
        elif missing_count == 0:
            status_key = "ready"
            status_label = "Ready"
        else:
            status_key = "in_progress"
            status_label = "In Progress"

        totals["captured"] += captured_count
        totals["expected"] += expected_count
        totals["absent"] += absent_count
        totals["missing_scores"] += missing_count

        rows.append({
            "assignment": assignment,
            "subject_name": assignment.subject.name,
            "teacher_name": assignment.teacher_profile.get_full_title(),
            "captured_count": captured_count,
            "total_students": expected_count,
            "absent_count": absent_count,
            "missing_count": missing_count,
            "submission": submission,
            "status_key": status_key,
            "status_label": status_label,
        })

    totals["completion_rate"] = round((totals["captured"] / totals["expected"]) * 100) if totals["expected"] else 0
    totals["all_submitted"] = totals["subjects"] > 0 and all(row["submission"] for row in rows)
    totals["all_scores_complete"] = totals["missing_scores"] == 0
    totals["can_approve"] = totals["all_submitted"] and totals["all_scores_complete"]
    totals["can_publish"] = totals["subjects"] > 0 and totals["approved"] == totals["subjects"]
    totals["stream_status"] = (
        "Published" if totals["published"] == totals["subjects"] and totals["subjects"]
        else "Approved" if totals["approved"] == totals["subjects"] and totals["subjects"]
        else "Submitted" if totals["submitted"] == totals["subjects"] and totals["subjects"]
        else "Returned" if totals["returned"]
        else "In Progress" if totals["captured"]
        else "Not Started"
    )
    return rows, totals


def get_selected_context(request, contexts):
    """Return the context dict matching the request's 'context' query parameter."""
    selected_key = request.GET.get("context")
    if selected_key:
        for item in contexts:
            if item["context_key"] == selected_key:
                return item
    return contexts[0] if contexts else None


def get_learner_contexts_for_user(user):
    """
    Return class streams a user may open in Learner Lists.
    """
    teacher = get_teacher_for_user(user)
    is_admin_view = user_has_main_school_admin_override(user)
    class_teacher_scope = get_class_teacher_scope(teacher)
    section = get_current_school_section()

    # Build base student queryset.
    # Admin view uses all_objects to see students across all sub-sections
    # (e.g. Grades 1-3 in LOWER alongside Grades 4-6 in UPPER).
    # Non-admin (teacher) views stay scoped via Student.objects (SchoolScopedManager).
    school = get_current_school()
    if is_admin_view:
        student_qs = Student.all_objects.filter(is_active=True)
        if school:
            student_qs = student_qs.filter(school=school)
        if section == 'JSS':
            student_qs = student_qs.filter(school_section='JSS')
        elif section == 'PRIMARY':
            student_qs = student_qs.filter(school_section='PRIMARY')
        elif section == 'LOWER_PRIMARY':
            student_qs = student_qs.filter(school_section='PRIMARY', sub_section='LOWER')
    else:
        student_qs = Student.objects.filter(is_active=True)

    if is_admin_view:
        qs = student_qs.values("class_name", "stream").annotate(learner_count=Count("id"))
    elif class_teacher_scope:
        # Section-scoped SubjectAssignment
        assignment_qs = SubjectAssignment.objects.all()
        if section == 'LOWER_PRIMARY':
            assignment_qs = assignment_qs.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            assignment_qs = assignment_qs.filter(school_section='PRIMARY', sub_section='UPPER')
        elif section == 'JSS':
            assignment_qs = assignment_qs.filter(school_section='JSS')
        assignment_pairs = list(
            assignment_qs.filter(teacher_profile=teacher)
            .values("class_name", "stream")
            .distinct()
        )
        filters = Q(class_name=class_teacher_scope[0], stream=class_teacher_scope[1])
        for item in assignment_pairs:
            filters |= Q(class_name=item["class_name"], stream=item["stream"])
        qs = student_qs.filter(filters).values("class_name", "stream").annotate(learner_count=Count("id"))
    else:
        assignment_qs = SubjectAssignment.objects.all()
        if section == 'LOWER_PRIMARY':
            assignment_qs = assignment_qs.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            assignment_qs = assignment_qs.filter(school_section='PRIMARY', sub_section='UPPER')
        elif section == 'JSS':
            assignment_qs = assignment_qs.filter(school_section='JSS')
        assignments = assignment_qs.filter(teacher_profile=teacher)
        allowed_pairs = assignments.values("class_name", "stream").distinct()
        filters = Q(pk__isnull=True)
        for item in allowed_pairs:
            filters |= Q(class_name=item["class_name"], stream=item["stream"])
        qs = student_qs.filter(filters).values("class_name", "stream").annotate(learner_count=Count("id"))

    contexts = list(qs.order_by("class_name", "stream"))
    for item in contexts:
        item["context_key"] = f"{item['class_name']}|{item['stream']}"
    return contexts


_teacher_user_cache = {}  # user_pk -> Teacher or None


def clear_teacher_cache(user_pk=None):
    """Invalidate the teacher cache. Call after updating a teacher profile."""
    if user_pk is not None:
        _teacher_user_cache.pop(user_pk, None)
    else:
        _teacher_user_cache.clear()


def get_teacher_for_user(user):
    """Return the Teacher instance linked to the given user, or None.
    Cached per user_pk to avoid repeated DB hits in the same request."""
    if not user.is_authenticated:
        return None
    pk = user.pk
    if pk not in _teacher_user_cache:
        _teacher_user_cache[pk] = Teacher.objects.filter(user=user).first()
    return _teacher_user_cache[pk]


def get_class_teacher_scope(teacher):
    """
    Use the existing assigned_task field, e.g. "Class Teacher Grade 7 Yellow",
    to determine a class teacher's permitted class stream.
    """
    if not teacher or not teacher.assigned_task:
        return None

    task = teacher.assigned_task
    if not task.startswith("Class Teacher"):
        return None

    from ..models import Grade, Stream
    school = get_current_school()
    if not school:
        return None

    all_grades = Grade.all_objects.filter(school=school).values_list("name", flat=True)
    all_streams = Stream.all_objects.filter(school=school).values_list("name", flat=True)

    # Use exact matching — "Class Teacher" + space + grade + space + stream
    prefix = "Class Teacher "
    remainder = task[len(prefix):] if task.startswith(prefix) else task
    for grade in all_grades:
        for stream in all_streams:
            if remainder == f"{grade} {stream}":
                return grade, stream
    return None


def user_can_access_class_stream(user, grade, stream, require_class_teacher=False):
    """Check whether a user is permitted to access a particular class stream."""
    if user_has_main_school_admin_override(user):
        return True

    teacher = get_teacher_for_user(user)
    class_scope = get_class_teacher_scope(teacher)
    if class_scope and class_scope == (grade, stream):
        return True

    if require_class_teacher:
        return False

    return SubjectAssignment.objects.filter(
        school=get_current_school(),
        teacher_profile=teacher,
        class_name=grade,
        stream=stream,
    ).exists()


def user_can_view_learner_profile(user, student):
    """Determine if a user may view a learner's profile."""
    from ..security.roles import Role, get_user_role

    role = get_user_role(user)
    if role == Role.STUDENT and hasattr(user, "student_profile"):
        return user.student_profile.pk == student.pk
    if role == Role.PARENT and hasattr(user, "guardian_profile"):
        return student.guardian_id == user.guardian_profile.pk
    return user_can_access_class_stream(user, student.class_name, student.stream, require_class_teacher=False)


def user_can_edit_learner_profile(user, student):
    """Determine if a user may edit a learner's profile."""
    from ..security.roles import Role, get_user_role

    if get_user_role(user) in {Role.STUDENT, Role.PARENT}:
        return False
    return user_has_main_school_admin_override(user) or user_can_access_class_stream(
        user,
        student.class_name,
        student.stream,
        require_class_teacher=True,
    )


_grading_config_cache = {}  # Kept for backward compat — delegates to grading_engine



# ═══════════════════════════════════════════════════════════════════════
# Fast Grading Lookup — bisect + module-level parsed cache
# ═══════════════════════════════════════════════════════════════════════

# Parsed lookup tables: scale_id → sorted tuple list
# Built once per GradingScale instance, never re-parsed.
_subject_lookup_cache = {}   # scale_id → [(min, max, level, pts), ...]
_total_lookup_cache = {}     # scale_id → [(min, max, level, pts), ...]


def _build_subject_lookup(scale):
    """Parse subject_scale into a sorted tuple list (once per scale).

    Accepts either a GradingScale model instance (with .pk, .subject_scale)
    or a raw list of scale entries.

    Returns two parallel lists for O(log n) bisect lookup:
      - mins:  [min_score, min_score, ...]  (sorted ascending)
      - entries: [(min, max, level, pts), ...]
    """
    if scale is None:
        return (), ()

    # Handle raw list input (from resolve_scale_fast)
    if isinstance(scale, list):
        if not scale:
            return (), ()
        raw = scale
        cache_key = id(scale)
    else:
        if not scale.pk:
            return (), ()
        cache_key = scale.pk
        raw = scale.subject_scale or []

    if cache_key not in _subject_lookup_cache:
        entries = tuple(
            sorted((e['min_score'], e['max_score'], e['level'], e['points']) for e in raw)
        )
        mins = tuple(e[0] for e in entries)
        _subject_lookup_cache[cache_key] = (mins, entries)
    return _subject_lookup_cache[cache_key]


def _build_total_lookup(scale):
    """Parse total_scale into a sorted tuple list (once per scale).

    Accepts either a GradingScale model instance (with .pk, .total_scale)
    or a raw list of scale entries.

    Returns two parallel lists for O(log n) bisect lookup:
      - mins:  [min_marks, min_marks, ...]  (sorted ascending)
      - entries: [(min, max, level, pts), ...]
    """
    if scale is None:
        return (), ()

    if isinstance(scale, list):
        if not scale:
            return (), ()
        raw = scale
        cache_key = id(scale)
    else:
        if not scale.pk:
            return (), ()
        cache_key = scale.pk
        raw = scale.total_scale or []

    if cache_key not in _total_lookup_cache:
        entries = tuple(
            sorted((e['min_marks'], e['max_marks'], e['level'], e['points']) for e in raw)
        )
        mins = tuple(e[0] for e in entries)
        _total_lookup_cache[cache_key] = (mins, entries)
    return _total_lookup_cache[cache_key]


def get_subject_level_fast(score, config):
    """O(log n) subject score lookup using bisect on pre-parsed tuples.

    Args:
        score: int — converted percentage (0-100)
        config: GradingConfig instance (already resolved, no DB query)

    Returns:
        (level, points) tuple

    Usage:
        config = _resolve_grading_config(school, section, sub_section)
        for mark in marks:
            level, pts = get_subject_level_fast(mark.score, config)
    """
    score = max(0, min(100, round(score or 0)))
    mins, entries = _build_subject_lookup(config)
    if not entries:
        return '-', 0
    # bisect_right on min_score boundaries → O(log n)
    idx = bisect.bisect_right(mins, score) - 1
    if 0 <= idx < len(entries):
        min_s, max_s, level, pts = entries[idx]
        if min_s <= score <= max_s:
            return level, pts
    return '-', 0


def get_total_level_fast(total_marks, config):
    """O(log n) total marks lookup using bisect on pre-parsed tuples.

    Args:
        total_marks: int — aggregated total marks
        config: GradingConfig instance (already resolved, no DB query)

    Returns:
        (level, points) tuple
    """
    total_marks = max(0, round(total_marks or 0))
    mins, entries = _build_total_lookup(config)
    if not entries:
        return '-', 0
    # bisect_right on min_marks boundaries → O(log n)
    idx = bisect.bisect_right(mins, total_marks) - 1
    if 0 <= idx < len(entries):
        min_m, max_m, level, pts = entries[idx]
        if min_m <= total_marks <= max_m:
            return level, pts
    return '-', 0


def get_performance_level(score, sub_section=None, subject_id=None, is_total_calculation=False, section=None, school=None):
    """
    Return (performance_level, points) using our optimized bisect lookups.
    Premium, dynamic subject and total calculation scale routing.

    Args:
        score: int — either a converted subject percentage (0-100) or an
                    aggregated total marks value (e.g. 0-800 for JSS).
        sub_section: str|None — 'LOWER' | 'UPPER' | None
        subject_id: int|None — pass the active Subject.pk so subject-specific
                     overrides win before the general fallback row kicks in.
        is_total_calculation: bool — when True, hits the broadsheet
                     `total_scale` JSON instead of `subject_scale`.
        section: str|None — explicit section override ('JSS', 'PRIMARY', 'LOWER_PRIMARY').
                    IMPORTANT: pass this from broadsheet/report callers because the
                    thread-local ContextVar is set to 'BOTH' for admin users,
                    which never matches any cached GradingAssignment key.
        school: School|None — explicit school override; otherwise reads thread-local.

    Returns:
        (level: str, points: int)
    """
    import logging
    from ..school_scope import get_current_school, get_current_school_section
    from .grading_engine import resolve_scale_fast

    # Round once, but do NOT clamp to 0-100 here — `is_total_calculation=True`
    # callers pass aggregated totals (e.g. 650 / 800) that must survive intact.
    # The clamp moves down into the subject branch where 0-100 actually applies.
    score_val = round(score or 0)

    if school is None:
        school = get_current_school()
    if section is None:
        section = get_current_school_section()

    if school and section:
        # Pass the real subject_id down to avoid skipping subject-specific overrides
        scale_data = resolve_scale_fast(
            school.pk,
            section,
            sub_section,
            subject_id=subject_id,
            is_total_calculation=is_total_calculation,
        )

        if scale_data:
            if is_total_calculation:
                return get_total_level_fast(score_val, scale_data)
            else:
                return get_subject_level_fast(max(0, min(100, score_val)), scale_data)

    logging.getLogger("students.helpers").error(
        "GradingScale missing for school_id=%s section=%s sub_section=%s subject=%s total=%s.",
        getattr(school, 'id', None), section, sub_section, subject_id, is_total_calculation,
    )
    return 'NO CONFIG', 0


def calculate_report_plv(total_points, total_marks, sub_section=None, school=None, section=None):
    """
    2-tier JSS Performance Level used for report card comment matching.
    Uses the school's GradingConfig.total_scale from the DB.
    NO hardcoded fallback — if scale is missing, logs error and returns '-'.

    Optional `school` and `section` parameters bypass the thread-local lookup,
    making this safe to call from Celery tasks or management commands.
    """
    import logging
    from ..school_scope import get_current_school, get_current_school_section

    pts = total_points or 0
    mks = total_marks  or 0

    if not school:
        school = get_current_school()
    if not section:
        section = get_current_school_section()

    if school and section:
        from .grading_engine import resolve_scale_fast
        scale_data = resolve_scale_fast(
            school.pk, section, sub_section,
            subject_id=None, is_total_calculation=True,
        )
        if scale_data:
            return get_total_level_fast(mks, scale_data)[0] if mks else '-'

    logging.getLogger("students.helpers").error(
        "GradingScale.total_scale missing for school_id=%s section=%s sub_section=%s. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section, sub_section,
    )
    return '-'


def calculate_broadsheet_plv(total_marks, total_points, sub_section=None, school=None, section=None):
    """
    Overall broadsheet level based on the learner's total performance points
    and raw total mark, keeping it consistent with report card PLV thresholds.
    """
    if not total_points and not total_marks:
        return '-'
    return calculate_report_plv(total_points, total_marks, sub_section, school=school, section=section)


def calculate_primary_plv(total_marks, assessed_subjects, sub_section=None, school=None, section=None):
    """
    Primary broadsheet PLV based on the school's GradingScale.total_scale.

    PLV is computed from the **total marks** against the configured total_scale
    ranges (e.g. 0-400 for 4-subject Lower Primary).
    """
    import logging
    from ..school_scope import get_current_school, get_current_school_section

    if not assessed_subjects or not total_marks:
        return '-'

    if not school:
        school = get_current_school()
    if not section:
        section = get_current_school_section()

    if school and section:
        from .grading_engine import resolve_scale_fast
        scale_data = resolve_scale_fast(
            school.pk, section, sub_section,
            subject_id=None, is_total_calculation=True,
        )
        if scale_data:
            level, _ = get_total_level_fast(total_marks, scale_data)
            if level and level != '-':
                return level

    logging.getLogger("students.helpers").error(
        "GradingConfig missing or unusable for school_id=%s section=%s sub_section=%s. "
        "Primary PLV cannot be resolved. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section, sub_section,
    )
    return '-'


def get_next_admission_no(school_section=None):
    """
    Compute the next sequential admission number as a zero-padded string
    with P/J suffix based on school_section.
    """
    from django.db.models.functions import Substr, Length
    suffix = 'P' if school_section == 'PRIMARY' else 'J'
    qs = Student.all_objects.all().filter(admission_no__regex=r'^[0-9]+[PJ]$')
    if school_section == 'PRIMARY':
        qs = qs.filter(school_section='PRIMARY')
    elif school_section == 'JSS':
        qs = qs.filter(school_section='JSS')
    last = (
        qs.annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
        .order_by('adm_int')
        .last()
    )
    if last and last.admission_no:
        try:
            return f"{int(last.admission_no[:-1]) + 1:03}{suffix}"
        except ValueError:
            pass
    return f'001{suffix}'


def get_students_ordered(grade, stream):
    """
    Return students filtered by grade and stream, ordered by admission number.
    Handles P/J suffixed admission numbers. Non-numeric parts sorted to the end.
    """
    from django.db.models import Value, CharField, Case, When, Q
    from django.db.models.functions import Substr, Length
    students = Student.all_objects.filter(
        class_name=grade, stream=stream, is_active=True
    ).filter(
        admission_no__regex=r'^[0-9]+[PJ]$'
    ).annotate(
        adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField())
    ).order_by('adm_int')
    return list(students)


def get_subject_students(grade, stream, subject):
    """
    Return the learner list expected for a subject.
    CRE/IRE become religion-aware after learners have been tagged once.
    Accepts either Subject instance or subject code string.
    """
    subject_code = subject.code if hasattr(subject, 'code') else subject
    students = get_students_ordered(grade, stream)
    if subject_code in RELIGION_SUBJECTS:
        religion_tag = RELIGION_TAG.get(subject_code, '')
        tagged_students = [s for s in students if s.religion == religion_tag]
        if tagged_students:
            return tagged_students
    return students


def get_subject_marks(class_name, stream, subject, term, exam_type, year):
    """
    Return marks for a subject using the same learner pool used for score entry.
    This prevents impossible counts such as 52/35 on CRE/IRE sheets.
    Accepts either Subject instance or subject code string.
    """
    subject_code = subject.code if hasattr(subject, 'code') else subject
    school = get_current_school()
    marks = Mark.all_objects.filter(
        student__class_name=class_name,
        student__stream=stream,
        subject=subject,
        term=term,
        exam_type=exam_type,
        year=year,
    )
    if school:
        marks = marks.filter(school=school)
    if subject_code in RELIGION_SUBJECTS:
        religion_tag = RELIGION_TAG.get(subject_code, '')
        religion_filter = dict(class_name=class_name, stream=stream, religion=religion_tag)
        if school:
            religion_filter['school'] = school
        if Student.all_objects.filter(**religion_filter).exists():
            marks = marks.filter(student__religion=religion_tag)
    return marks


def get_religion_aware_student_count(class_name, stream, subject):
    """Return the count of students eligible for the given subject."""
    subject_code = subject.code if hasattr(subject, 'code') else subject
    students = Student.all_objects.filter(class_name=class_name, stream=stream, is_active=True)
    if subject_code in RELIGION_SUBJECTS:
        religion_tag = RELIGION_TAG.get(subject_code, '')
        school = get_current_school()
        religion_filter = dict(class_name=class_name, stream=stream, religion=religion_tag, is_active=True)
        if school:
            religion_filter['school'] = school
        if Student.all_objects.filter(**religion_filter).exists():
            students = students.filter(religion=religion_tag)
    return students.count()


def get_class_leaderboard(school, class_name, stream, year, term, assessment, published_subjects_qs):
    """
    Return a ranked leaderboard for a class/stream using normalized Mean Score.

    Students taking fewer subjects are no longer penalized — ranking is by
    average score per subject, with total score as tie-breaker.

    Result is cached in Redis for 1 hour. Call invalidate_report_caches()
    when marks change to force a refresh.

    Returns:
        dict with keys:
            'sorted_ids':   list[int] — student IDs in rank order (best first)
            'class_count':  int       — total students ranked
            'scores_map':   dict      — {student_id: {'total': int, 'mean': float, 'count': int}}
    """
    key = _leaderboard_cache_key(school.pk, class_name, stream, year, term, assessment)
    cached = cache.get(key)
    if cached is not None:
        return cached

    class_scores = (
        Mark.all_objects.filter(
            school=school,
            student__class_name=class_name,
            student__stream=stream,
            year=year,
            term=term,
            exam_type=assessment,
            subject__in=published_subjects_qs,
        )
        .values('student_id')
        .annotate(
            total_score=Sum('score'),
            subject_count=Count('subject_id', distinct=True),
            mean_score=Avg('score'),
        )
        .order_by('-mean_score', '-total_score')
    )

    sorted_ids = [item['student_id'] for item in class_scores]
    result = {
        'sorted_ids': sorted_ids,
        'class_count': len(sorted_ids),
        'scores_map': {
            item['student_id']: {
                'total': item['total_score'],
                'mean': round(item['mean_score'], 2) if item['mean_score'] else 0,
                'count': item['subject_count'],
            }
            for item in class_scores
        },
    }
    cache.set(key, result, _CACHE_TTL)
    return result


def get_student_totals_with_rank(school, class_name, stream, year, term, assessment, published_subjects_qs):
    """
    Database-side aggregation: returns a queryset of dicts, one per student,
    with total_marks, total_points, subject_count, and dense_rank — all
    computed entirely in PostgreSQL.

    Ranking order: total_marks DESC, total_points DESC (tie-breaker).

    Returns:
        QuerySet[dict]: [
            {'student_id': int, 'total_marks': int, 'total_points': int,
             'subject_count': int, 'rank': int},
            ...
        ]
    """
    from django.db.models import IntegerField
    from django.db.models.functions import Coalesce

    base_filter = dict(
        school=school,
        student__class_name=class_name,
        year=year,
        term=term,
        exam_type=assessment,
        subject__in=published_subjects_qs,
    )
    if stream is not None:
        base_filter['student__stream'] = stream

    return (
        Mark.all_objects
        .filter(**base_filter)
        .exclude(is_absent=True)
        .values('student_id')
        .annotate(
            total_marks=Coalesce(Sum('score'), Value(0), output_field=IntegerField()),
            total_points=Coalesce(Sum('points'), Value(0), output_field=IntegerField()),
            subject_count=Count('subject_id', distinct=True),
            rank=Window(
                expression=DenseRank(),
                order_by=[F('total_marks').desc(), F('total_points').desc()],
            ),
        )
        .order_by('rank', '-total_points')
    )


# ── Atomic mark upsert (PostgreSQL SELECT FOR UPDATE + single write) ──────────

def upsert_mark(
    school_id, student_id, subject_id, school_section, sub_section,
    score, raw_score, maximum_marks, is_absent,
    primary_raw_score, primary_performance_point, primary_descriptor,
    performance_level, points,
    term, year, exam_type,
):
    """
    Atomic single-row upsert using SELECT ... FOR UPDATE within a
    serialized transaction.  Guarantees zero dead tuples, zero index
    bloat, and zero race conditions under high concurrency.

    Flow:
      1. Begin transaction (serializable isolation via atomic)
      2. SELECT ... FOR UPDATE — locks the existing row (if any)
      3. If exists → UPDATE in place (single UPDATE, no DELETE + INSERT)
      4. If not   → INSERT (single INSERT)
      5. Compute and set integrity_checksum
      6. Commit — lock released

    Returns the mark ID.
    """
    from django.db import transaction, connection
    from ..security.integrity import compute_mark_checksum

    class _MarkProxy:
        """Lightweight stand-in for a Mark instance so compute_mark_checksum works."""
        __slots__ = (
            'school_id', 'student_id', 'subject', 'score', 'raw_score',
            'maximum_marks', 'is_absent', 'term', 'year', 'exam_type',
            'performance_level', 'points',
        )

        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    def _compute_checksum(**kw):
        proxy = _MarkProxy(**kw)
        return compute_mark_checksum(proxy)

    # Filter kwargs shared by SELECT, INSERT, UPDATE
    _filter = dict(
        school_id=school_id,
        student_id=student_id,
        term=term,
        exam_type=exam_type,
        year=year,
        school_section=school_section,
    )
    if subject_id is not None:
        _filter['subject_id'] = subject_id
    else:
        _filter['subject_id__isnull'] = True
    if sub_section is not None:
        _filter['sub_section'] = sub_section
    else:
        _filter['sub_section__isnull'] = True

    with transaction.atomic():
        existing = (
            Mark.all_objects
            .select_for_update(nowait=False)
            .filter(**_filter)
            .first()
        )

        checksum = _compute_checksum(
            school_id=school_id, student_id=student_id, subject=subject_id,
            score=score, raw_score=raw_score, maximum_marks=maximum_marks,
            is_absent=is_absent, term=term, year=year, exam_type=exam_type,
            performance_level=performance_level, points=points,
        )

        if existing:
            Mark.all_objects.filter(pk=existing.pk).update(
                score=score,
                raw_score=raw_score,
                maximum_marks=maximum_marks,
                is_absent=is_absent,
                primary_raw_score=primary_raw_score,
                primary_performance_point=primary_performance_point,
                primary_descriptor=primary_descriptor,
                performance_level=performance_level,
                points=points,
                integrity_checksum=checksum,
            )
            return existing.pk
        else:
            mark = Mark.all_objects.create(
                school_id=school_id,
                student_id=student_id,
                subject_id=subject_id,
                school_section=school_section,
                sub_section=sub_section,
                score=score,
                raw_score=raw_score,
                maximum_marks=maximum_marks,
                is_absent=is_absent,
                primary_raw_score=primary_raw_score,
                primary_performance_point=primary_performance_point,
                primary_descriptor=primary_descriptor,
                performance_level=performance_level,
                points=points,
                term=term,
                year=year,
                exam_type=exam_type,
                integrity_checksum=checksum,
            )
            return mark.pk


# ── Report Forms Cache Helpers ──────────────────────────────────────────────

def get_report_forms_cache_key(school_id, grade, stream, exam_id):
    """Generate a cache key for report forms display."""
    return f"report_forms:{school_id}:{grade}:{stream}:{exam_id}"


def invalidate_report_forms_cache(school_id, grade=None, stream=None, exam_id=None):
    """
    Evict cached report forms data when marks are modified.
    If grade/stream/exam_id are provided, evict only that specific key.
    Otherwise, evict all report_forms cache entries for the school.
    """
    if grade and stream and exam_id:
        key = get_report_forms_cache_key(school_id, grade, stream, exam_id)
        cache.delete(key)
    else:
        # Evict all report_forms cache entries for this school
        pattern = f"report_forms:{school_id}:*"
        try:
            from django.core.cache import cache as _cache
            if hasattr(_cache, 'delete_pattern'):
                _cache.delete_pattern(pattern)
        except Exception:
            pass


def freeze_comments_for_student_marks(marks, class_teacher_remark, headteacher_comment,
                                       master_comment, school_ht_comment, freeze_threshold, now):
    """
    Freeze comments onto Mark records when the master comment has exceeded the
    freeze threshold. This is a WRITE operation that must be called from
    background tasks or explicit save actions — never from display views.

    Returns dict with frozen values to use for display.
    """
    from ..models import Mark
    import datetime

    result = {
        'class_teacher_remark': class_teacher_remark,
        'headteacher_comment': headteacher_comment,
        'closing_date': master_comment.closing_date if master_comment else None,
        'opening_date': master_comment.opening_date if master_comment else None,
    }

    if master_comment and class_teacher_remark:
        ct_field = f"comment_{class_teacher_remark.lower()}" if class_teacher_remark else None
        if ct_field:
            live_ct = getattr(master_comment, ct_field, "") or ""
            if live_ct.strip():
                age = now - (master_comment.last_modified.replace(tzinfo=datetime.timezone.utc)
                             if master_comment.last_modified.tzinfo is None
                             else master_comment.last_modified)
                if age >= freeze_threshold and marks:
                    Mark.all_objects.filter(id__in=[m.id for m in marks]).update(
                        frozen_class_teacher_comment=live_ct,
                        frozen_closing_date=master_comment.closing_date,
                        frozen_opening_date=master_comment.opening_date,
                    )
                    result['closing_date'] = master_comment.closing_date
                    result['opening_date'] = master_comment.opening_date

    if school_ht_comment and headteacher_comment:
        ht_field = f"ht_comment_{headteacher_comment.lower()}" if headteacher_comment else None
        if ht_field:
            live_ht = getattr(school_ht_comment, ht_field, "") or ""
            if live_ht.strip():
                age = now - (school_ht_comment.last_modified.replace(tzinfo=datetime.timezone.utc)
                             if school_ht_comment.last_modified.tzinfo is None
                             else school_ht_comment.last_modified)
                if age >= freeze_threshold and marks:
                    Mark.all_objects.filter(id__in=[m.id for m in marks]).update(
                        frozen_headteacher_comment=live_ht,
                    )

    return result