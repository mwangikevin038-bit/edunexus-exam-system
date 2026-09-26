"""
Helper functions for the students views module.

Provides utilities for authentication, access control, student ordering,
subject-aware queries, and performance-level calculations used by the
various view layers.
"""

import bisect
import random
import re
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
from ..models import Mark, MarkSubmission, Student, SubjectAssignment, Teacher, TermDate
from ..school_scope import get_current_school, get_current_school_section
from ..security import user_has_main_school_admin_override


def safe_pdf_filename(*parts):
    """Build a professional, header-safe PDF filename (Type-First, case-preserving).

    Joins non-empty parts with underscores, replaces spaces with underscores,
    strips characters that are unsafe in Content-Disposition headers, and
    appends ``.pdf`` when missing.

    Example: ``safe_pdf_filename('Merit_List', 'Grade 7', 'Blue', 2026)``
    → ``Merit_List_Grade_7_Blue_2026.pdf``
    """
    raw = '_'.join(str(p).strip() for p in parts if p is not None and str(p).strip())
    raw = raw.replace(' ', '_')
    raw = re.sub(r'[^A-Za-z0-9_\-.]+', '_', raw)
    raw = re.sub(r'_+', '_', raw).strip('._')
    if not raw:
        raw = 'document'
    if not raw.lower().endswith('.pdf'):
        raw += '.pdf'
    return raw


# ── Term-date fallback for report cards ──────────────────────────────────────

def resolve_term_dates(school, year, term):
    """
    Query TermDate for the given school/year/term and return (end_date, start_date)
    suitable for use as closing_date / opening_date fallback on report cards.
    Returns (None, None) if no TermDate record exists.
    """
    td = TermDate.objects.filter(
        school=school, academic_year=year, term=term
    ).first()
    if td:
        return td.end_date, td.start_date
    return None, None


# ── Cache TTL and key helpers ────────────────────────────────────────────────
_CACHE_TTL = 3600  # 1 hour

def _leaderboard_cache_key(school_id, class_name, stream, year, term, assessment):
    _g = str(class_name).replace(" ", "_") if class_name else ""
    _s = str(stream).replace(" ", "_") if stream else ""
    _t = str(term).replace(" ", "_") if term else ""
    _a = str(assessment).replace(" ", "_") if assessment else ""
    return f"lb_{school_id}_{_g}_{_s}_{year}_{_t}_{_a}"

def _class_avg_cache_key(school_id, class_name, stream, year, term, assessment):
    _g = str(class_name).replace(" ", "_") if class_name else ""
    _s = str(stream).replace(" ", "_") if stream else ""
    _t = str(term).replace(" ", "_") if term else ""
    _a = str(assessment).replace(" ", "_") if assessment else ""
    return f"avg_{school_id}_{_g}_{_s}_{year}_{_t}_{_a}"

def _delete_cache_key_pattern(logical_pattern):
    """
    Delete cache keys whose logical key matches logical_pattern.

    logical_pattern uses '*' as a wildcard (e.g. 'merit_list:1:Grade_3:*').
    Works with native django RedisCache (SCAN) and best-effort LocMem.
    Returns number of keys deleted; never raises.
    """
    if not logical_pattern:
        return 0

    try:
        if '*' not in logical_pattern:
            cache.delete(logical_pattern)
            return 1

        prefix = logical_pattern.split('*', 1)[0]
        full_prefix = cache.make_key(prefix)
        match_pattern = full_prefix + '*'

        client = None
        backend_client = getattr(cache, '_cache', None)
        if backend_client is not None:
            if hasattr(backend_client, 'get_client'):
                try:
                    client = backend_client.get_client(None, write=True)
                except TypeError:
                    client = backend_client.get_client(None)
            elif hasattr(backend_client, 'scan'):
                client = backend_client

        if client is not None and hasattr(client, 'scan'):
            cursor = 0
            deleted = 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=match_pattern, count=200)
                if keys:
                    deleted += int(client.delete(*keys) or 0)
                if cursor == 0:
                    break
            return deleted

        if isinstance(backend_client, dict):
            kp = getattr(cache, 'key_prefix', '') or ''
            ver = getattr(cache, 'version', 1)
            head = f'{kp}:{ver}:'
            logical_keys = []
            for fk in list(backend_client.keys()):
                if fk.startswith(full_prefix):
                    if fk.startswith(head):
                        logical_keys.append(fk[len(head):])
                    else:
                        logical_keys.append(fk)
            for lk in logical_keys:
                cache.delete(lk)
            return len(logical_keys)

        if hasattr(cache, 'delete_pattern'):
            return int(cache.delete_pattern(logical_pattern) or 0)
    except Exception:
        pass
    return 0


def _safe_cache_get(key, default=None):
    """cache.get that returns default if the cache backend is down."""
    try:
        return cache.get(key, default)
    except Exception:
        return default


def invalidate_report_caches(school_id, class_name, stream, year, term, assessment):
    """Call this whenever marks are uploaded/changed for a class/stream/exam."""
    try:
        cache.delete(_leaderboard_cache_key(school_id, class_name, stream, year, term, assessment))
        cache.delete(_class_avg_cache_key(school_id, class_name, stream, year, term, assessment))
    except Exception:
        pass

    # Also invalidate score sheet caches (exams list, analysis data)
    from .students_mgmt import invalidate_score_sheet_caches
    invalidate_score_sheet_caches(school_id, class_name)

    # Invalidate merit list Redis keys for this grade (native Redis SCAN)
    _safe_cn = str(class_name).replace(' ', '_') if class_name else ''
    _delete_cache_key_pattern(f'merit_list:{school_id}:{_safe_cn}:*')

    # Report-forms memo for this school (pattern delete)
    _delete_cache_key_pattern(f'report_forms:{school_id}:*')

    # Also invalidate the exam result snapshot
    from ..models import ExamResultSnapshot, School
    try:
        school = School.objects.get(pk=school_id)
        ExamResultSnapshot.all_objects.filter(
            school=school,
            term=term,
            year=year,
            exam_name=assessment,
            class_name=class_name,
            stream=stream,
        ).delete()
    except School.DoesNotExist:
        pass


def get_cached_class_averages(school, class_name, stream, year, term, assessment, published_subjects_qs):
    """
    Return {subject_code: avg_score} for a class/stream, cached in Redis.
    Tries the snapshot first (zero DB); falls back to Mark aggregation.
    """
    key = _class_avg_cache_key(school.pk, class_name, stream, year, term, assessment)
    cached = _safe_cache_get(key)
    if cached is not None:
        return cached

    # ── Try snapshot first ──────────────────────────────────────────────
    from ..models import ExamResultSnapshot, Exam
    exam_obj = Exam.all_objects.filter(
        school=school, name=assessment, term=term, year=year,
    ).first()
    if exam_obj:
        snap = ExamResultSnapshot.get_latest(school, term, year, assessment, class_name, stream)
        if snap and snap.broadsheet_data:
            avg_map = {
                code: data.get('class_average', data.get('mean_score', 0))
                for code, data in snap.broadsheet_data.items()
                if data.get('student_count', 0) > 0
            }
            if avg_map:
                cache.set(key, avg_map, _CACHE_TTL)
                return avg_map

    # ── Fallback: live Mark aggregation ─────────────────────────────────
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
    Return subject codes that should appear on report cards.
    
    A subject appears if EITHER:
      1. Its MarkSubmission has been formally published, OR
      2. Marks exist for that subject (teacher saved them, even if not submitted)
    
    This ensures marks are NEVER invisible — if a teacher entered them, they show.
    The publish workflow still controls official publication status, but does NOT
    gate mark visibility.
    """
    school = get_current_school()
    section = get_current_school_section()

    # ── 1. Published submission subject codes ──────────────────────────
    pub_filters = dict(
        class_name=class_name,
        stream=stream,
        year=year,
        term=term,
        exam_name=exam_name,
        status="published",
    )
    if school:
        pub_filters['school'] = school
    if not is_admin:
        if sub_section == 'LOWER':
            pub_filters['school_section'] = 'PRIMARY'
            pub_filters['sub_section'] = 'LOWER'
        elif sub_section == 'UPPER':
            pub_filters['school_section'] = 'PRIMARY'
            pub_filters['sub_section'] = 'UPPER'
        elif section == 'LOWER_PRIMARY':
            pub_filters['school_section'] = 'PRIMARY'
            pub_filters['sub_section'] = 'LOWER'
        elif section == 'PRIMARY':
            pub_filters['school_section'] = 'PRIMARY'
            pub_filters['sub_section'] = 'UPPER'
        elif section == 'JSS':
            pub_filters['school_section'] = 'JSS'
    published_codes = set(
        MarkSubmission.all_objects.filter(**pub_filters).values_list("subject__code", flat=True)
    )

    # ── 2. Subjects that have marks entered (even if not published) ────
    mark_filters = dict(
        student__class_name=class_name,
        student__stream=stream,
        year=year,
        term=term,
        exam_type=exam_name,
    )
    if school:
        mark_filters['school'] = school
    if not is_admin:
        if sub_section == 'LOWER':
            mark_filters['school_section'] = 'PRIMARY'
            mark_filters['sub_section'] = 'LOWER'
        elif sub_section == 'UPPER':
            mark_filters['school_section'] = 'PRIMARY'
            mark_filters['sub_section'] = 'UPPER'
        elif section == 'LOWER_PRIMARY':
            mark_filters['school_section'] = 'PRIMARY'
            mark_filters['sub_section'] = 'LOWER'
        elif section == 'PRIMARY':
            mark_filters['school_section'] = 'PRIMARY'
            mark_filters['sub_section'] = 'UPPER'
        elif section == 'JSS':
            mark_filters['school_section'] = 'JSS'
    marks_codes = set(
        Mark.all_objects.filter(**mark_filters).values_list("subject__code", flat=True).distinct()
    )

    return published_codes | marks_codes


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
    Optimized: batch-fetches marks and submissions to minimize DB queries.
    """
    school = get_current_school()
    assignment_filters = dict(class_name=class_name, stream=stream)
    if school:
        assignment_filters['school'] = school
    # Scope to the exam's section (and sub-section for Primary) so the
    # readiness UI matches what quick_publish_stream will actually publish.
    assignment_filters['school_section'] = exam.school_section
    if exam.sub_section:
        assignment_filters['sub_section'] = exam.sub_section

    assignments = list(
        SubjectAssignment.all_objects.filter(is_active=True, **assignment_filters)
        .select_related("teacher_profile", "teacher_profile__user", "subject")
        .order_by("subject__code")
    )

    # Batch-fetch ALL marks for this class/stream/exam in ONE query
    all_marks = Mark.all_objects.filter(
        student__class_name=class_name,
        student__stream=stream,
        term=exam.term,
        exam_type=exam.name,
        year=exam.year,
        school_section=exam.school_section,
    )
    if exam.sub_section:
        all_marks = all_marks.filter(sub_section=exam.sub_section)
    if school:
        all_marks = all_marks.filter(school=school)
    marks_by_subject = {}
    for m in all_marks.select_related('student').values('subject_id', 'is_absent'):
        sid = m['subject_id']
        if sid not in marks_by_subject:
            marks_by_subject[sid] = {'count': 0, 'absent': 0}
        marks_by_subject[sid]['count'] += 1
        if m['is_absent']:
            marks_by_subject[sid]['absent'] += 1

    # Batch-fetch ALL submissions for this class/stream/exam in ONE query.
    # Key by (subject_id, teacher_id): unique_together allows one row per
    # teacher, and we must resolve the assigned teacher's sheet — not an
    # arbitrary other teacher's submission for the same subject.
    submission_filters = dict(
        class_name=class_name, stream=stream,
        exam_name=exam.name, term=exam.term, year=exam.year,
        school_section=exam.school_section,
    )
    if exam.sub_section:
        submission_filters['sub_section'] = exam.sub_section
    if school:
        submission_filters['school'] = school
    all_submissions = {}
    for s in MarkSubmission.all_objects.filter(**submission_filters).select_related('teacher'):
        all_submissions[(s.subject_id, s.teacher_id)] = s

    # Batch-fetch student counts per subject (for religion-aware counting)
    all_students = Student.all_objects.filter(
        class_name=class_name, stream=stream, is_active=True,
        school_section=exam.school_section,
    )
    if exam.sub_section:
        all_students = all_students.filter(sub_section=exam.sub_section)
    if school:
        all_students = all_students.filter(school=school)
    total_student_count = all_students.count()
    religion_students = {}
    for rel in all_students.values('religion').annotate(cnt=Count('id')):
        religion_students[rel['religion']] = rel['cnt']

    rows = []
    totals = {
        "subjects": len(assignments),
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
        subject = assignment.subject
        sid = subject.id
        subject_code = subject.code if subject else ''

        # Get expected count (religion-aware)
        if subject_code in RELIGION_SUBJECTS:
            religion_tag = RELIGION_TAG.get(subject_code, '')
            expected_count = religion_students.get(religion_tag, 0)
        else:
            expected_count = total_student_count

        # Get captured count from batch-fetched data
        marks_data = marks_by_subject.get(sid, {'count': 0, 'absent': 0})
        captured_count = marks_data['count']
        absent_count = marks_data['absent']
        missing_count = max(expected_count - captured_count, 0)

        # Prefer the assigned teacher's submission; fall back to any
        # submission for the subject (legacy sheets from a previous teacher).
        submission = all_submissions.get((sid, assignment.teacher_profile_id))
        if submission is None:
            submission = next(
                (s for (sub_id, _tid), s in all_submissions.items() if sub_id == sid),
                None,
            )

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
            "subject_name": subject.name if subject else '',
            "teacher_name": assignment.teacher_profile.get_full_title() if assignment.teacher_profile else "—",
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
        assignment_qs = SubjectAssignment.objects.filter(is_active=True)
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
        assignment_qs = SubjectAssignment.objects.filter(is_active=True)
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


def clear_teacher_cache(user_pk=None):
    """Invalidate the teacher cache. Call after updating a teacher profile."""
    if user_pk is not None:
        cache.delete(f"teacher_user:{user_pk}")
    else:
        # Can't clear all Redis keys easily, but the TTL handles cleanup
        pass


def get_teacher_for_user(user):
    """Return the Teacher instance linked to the given user, or None.
    Cached in Redis for 1 hour to avoid DB hits across all workers."""
    if not user.is_authenticated:
        return None
    pk = user.pk
    cache_key = f"teacher_user:{pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached != 'NONE' else None
    teacher = Teacher.objects.filter(user=user).first()
    cache.set(cache_key, teacher if teacher else 'NONE', 3600)
    return teacher


def get_class_teacher_scope(teacher):
    """
    Use the existing assigned_task field, e.g. "Class Teacher Grade 7 Yellow",
    to determine a class teacher's permitted class stream.
    Cached for 1 hour to avoid 2 DB queries per request.
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

    cache_key = f"ct_scope:{school.pk}:{teacher.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached != 'NONE' else None

    all_grades = list(Grade.all_objects.filter(school=school).values_list("name", flat=True))
    all_streams = list(Stream.all_objects.filter(school=school).values_list("name", flat=True))

    # Use exact matching — "Class Teacher" + space + grade + space + stream
    prefix = "Class Teacher "
    remainder = task[len(prefix):] if task.startswith(prefix) else task
    result = None
    for grade in all_grades:
        for stream in all_streams:
            if remainder == f"{grade} {stream}":
                result = (grade, stream)
                break
        if result:
            break

    cache.set(cache_key, result if result else 'NONE', 3600)
    return result


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
        is_active=True,
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
        cache_key = ('L', tuple(e.get('min_score', 0) for e in raw), tuple(e.get('max_score', 0) for e in raw),
                     tuple(e.get('level', '') for e in raw), tuple(e.get('points', 0) for e in raw))
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
        cache_key = ('L', tuple(e.get('min_marks', 0) for e in raw), tuple(e.get('max_marks', 0) for e in raw),
                     tuple(e.get('level', '') for e in raw), tuple(e.get('points', 0) for e in raw))
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


_DESCRIPTOR_MAP = {
    'Exceeding Expectations': 'EE',
    'Meeting Expectations': 'ME',
    'Approaching Expectations': 'AE',
    'Below Expectations': 'BE',
    'Outstanding': 'EE',
    'Excellent': 'EE',
    'Good': 'ME',
    'Average': 'AE',
    'Fair': 'AE',
    'Poor': 'BE',
    'Very Poor': 'BE',
}


def to_primary_descriptor(perf_level, school_section=None):
    """Map any performance level string to a safe 2-char primary_descriptor.

    - JSS subjects: always returns '' (primary_descriptor is a Primary-only field).
    - Primary subjects: maps full names to 2-char codes (EE/ME/AE/BE/AB).
    - Handles 2-char codes (EE, ME, AE, BE, AB) — passes through.
    - Handles JSS 3-char codes (EE1, BE2, etc.) — maps if possible, else ''.
    - Never returns a string longer than 2 chars.
    """
    level = (perf_level or '').strip()

    if not level:
        return ''

    if school_section and school_section != 'PRIMARY':
        return ''

    if level.upper() == 'AB':
        return 'AB'

    if level in _DESCRIPTOR_MAP:
        return _DESCRIPTOR_MAP[level]

    upper = level.upper()
    if upper in ('EE', 'ME', 'AE', 'BE', 'AB'):
        return upper

    if len(level) == 3:
        base = level[:2].upper()
        if base in ('EE', 'ME', 'AE', 'BE'):
            return base

    return ''


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
    from .grading_engine import resolve_scale_fast, _GRADED_CACHE_KEYS

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

        if not scale_data and school:
            _school_prefix = f"grading_scale:{school.pk}:"
            cache_has_school = any(k.startswith(_school_prefix) for k in _GRADED_CACHE_KEYS)
            if not cache_has_school:
                from .grading_engine import prefetch_school_grading
                prefetch_school_grading(school)
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


def get_students_ordered(grade, stream, school=None):
    """
    Return students filtered by grade and stream, ordered by admission number.
    Handles P/J suffixed admission numbers. Non-numeric parts sorted to the end.
    """
    from django.db.models import Value, CharField, Case, When, Q
    from django.db.models.functions import Substr, Length
    students = Student.all_objects.filter(
        class_name=grade, stream=stream, is_active=True
    )
    if school is None:
        school = get_current_school()
    if school:
        students = students.filter(school=school)
    students = students.filter(
        admission_no__regex=r'^[0-9]+[PJ]$'
    ).annotate(
        adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField())
    ).order_by('adm_int')
    return list(students)


def get_subject_students(grade, stream, subject, school=None):
    """
    Return the learner list expected for a subject.
    CRE/IRE become religion-aware after learners have been tagged once.
    Accepts either Subject instance or subject code string.
    """
    subject_code = subject.code if hasattr(subject, 'code') else subject
    students = get_students_ordered(grade, stream, school=school)
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
    subject_section = getattr(subject, 'school_section', None)
    if subject_section:
        marks = marks.filter(school_section=subject_section)
    if school:
        marks = marks.filter(school=school)
    if subject_code in RELIGION_SUBJECTS:
        religion_tag = RELIGION_TAG.get(subject_code, '')
        religion_filter = dict(class_name=class_name, stream=stream, religion=religion_tag)
        if school:
            religion_filter['school'] = school
        if subject_section:
            religion_filter['school_section'] = subject_section
        if Student.all_objects.filter(**religion_filter).exists():
            marks = marks.filter(student__religion=religion_tag)
    return marks


def get_religion_aware_student_count(class_name, stream, subject):
    """Return the count of students eligible for the given subject."""
    subject_code = subject.code if hasattr(subject, 'code') else subject
    subject_section = getattr(subject, 'school_section', None)
    students = Student.all_objects.filter(class_name=class_name, stream=stream, is_active=True)
    if subject_section:
        students = students.filter(school_section=subject_section)
    if subject_code in RELIGION_SUBJECTS:
        religion_tag = RELIGION_TAG.get(subject_code, '')
        school = get_current_school()
        religion_filter = dict(class_name=class_name, stream=stream, religion=religion_tag, is_active=True)
        if school:
            religion_filter['school'] = school
        if subject_section:
            religion_filter['school_section'] = subject_section
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


# ── Exam result snapshot builder ─────────────────────────────────────────────

def dedup_marks_latest_by_code(marks):
    """Keep one mark per subject code — the latest by (date_recorded, id).

    A student can end up with two Mark rows sharing one subject code (e.g. a
    stray row written under another grade's Subject object with the same code).
    Downstream entries/means/totals must count each subject once; the most
    recently recorded mark wins. Comparison is date-based so the result does
    not depend on queryset ordering.
    """
    from datetime import datetime as _dt

    latest = {}
    for m in marks:
        code = m.subject.code if getattr(m, 'subject', None) else None
        if code is None:
            continue
        cur = latest.get(code)
        if cur is None:
            latest[code] = m
            continue
        m_key = (m.date_recorded or _dt.min, m.pk or 0)
        c_key = (cur.date_recorded or _dt.min, cur.pk or 0)
        if m_key > c_key:
            latest[code] = m
    return list(latest.values())


def build_exam_result_snapshot(school, exam, class_name, stream):
    """
    Build and save a snapshot of exam results for one stream.

    This snapshot stores:
    - Per-student totals, ranks, positions, PLVs (from ExamSummary)
    - Per-student per-subject marks with levels (for merit list broadsheet)
    - Per-subject class averages, teacher names
    - Analysis data (gender counts, stream average)

    The snapshot is used by broadsheet views and the merit list. Report card
    views still use build_report_card_context which reads from ExamSummary directly.
    """
    from django.utils import timezone
    from ..models import ExamResultSnapshot, ExamSummary, Mark, Subject, SubjectAssignment
    from .exams import _get_primary_performance
    from .grading_engine import prefetch_school_grading

    # Ensure grading cache is populated before resolving levels
    prefetch_school_grading(school)

    # 1. Get students
    students = list(
        Student.all_objects.filter(
            school=school,
            class_name=class_name,
            stream=stream,
            is_active=True,
        ).order_by('admission_no')
    )

    if not students:
        return None

    sample = students[0]

    # 2. Get published subject codes
    published_subject_codes = get_published_subject_codes(
        class_name, stream, exam.year, exam.term, exam.name,
        sub_section=sample.sub_section if sample.school_section == 'PRIMARY' else None,
        is_admin=True,
    )

    # 3. Get Subject objects
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

    # 4. Get all marks for this stream in one query
    all_marks = Mark.all_objects.filter(
        school=school,
        student__class_name=class_name,
        student__stream=stream,
        student__is_active=True,
        term=exam.term,
        exam_type=exam.name,
        year=exam.year,
        subject__in=published_subjects_qs,
    ).select_related('subject').order_by('-date_recorded', '-id')

    # Group marks by student, then keep only the latest mark per subject code
    # so a stray duplicate can never inflate entries/means/totals.
    marks_by_student = {}
    for mark in all_marks:
        marks_by_student.setdefault(mark.student_id, []).append(mark)
    for _sid in marks_by_student:
        marks_by_student[_sid] = dedup_marks_latest_by_code(marks_by_student[_sid])
    # Deduped flat list — feeds class_averages / subject_data / marks_by_gender
    deduped_marks = [m for _lst in marks_by_student.values() for m in _lst]

    # 5. Get ExamSummary for all students (pre-computed rankings)
    summaries = {
        s.student_id: s
        for s in ExamSummary.all_objects.filter(
            school=school,
            term=exam.term,
            year=exam.year,
            exam_name=exam.name,
            student__class_name=class_name,
            student__stream=stream,
        )
    }

    # Resolve section and sub_section early (needed by teacher query + grading)
    section = sample.school_section or 'JSS'
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY'

    if is_lower_primary:
        sub_section = 'LOWER'
    elif is_primary:
        # Derive from the student roster, not the exam: a Primary exam row can
        # carry a JSS/blank sub_section (e.g. whole-school opener picked from
        # the JSS exam) while the grade's assignments live under sample's.
        sub_section = sample.sub_section or exam.sub_section or 'UPPER'
        if sub_section not in ('LOWER', 'UPPER'):
            sub_section = 'UPPER'
    else:
        sub_section = None

    # 6. Get subject assignments for teacher names (with section/sub_section filter)
    sa_qs = SubjectAssignment.all_objects.filter(
        school=school,
        class_name=class_name,
        stream=stream,
        is_active=True,
    ).select_related('teacher_profile', 'teacher_profile__user', 'subject')
    if section == 'LOWER_PRIMARY':
        sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section='LOWER')
    elif is_primary:
        sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section=sub_section)
    elif section == 'JSS':
        sa_qs = sa_qs.filter(school_section='JSS')
    assignments = {}
    for a in sa_qs.order_by('id'):
        if a.subject:
            assignments.setdefault(a.subject.code, []).append(a)

    # 7. Compute class averages per subject
    class_averages = {}
    for sub_code in published_subject_codes:
        sub_marks = [m for m in deduped_marks if m.subject.code == sub_code]
        valid_scores = [m.score for m in sub_marks if not m.is_absent and m.score is not None]
        if valid_scores:
            class_averages[sub_code] = round(sum(valid_scores) / len(valid_scores), 1)
        else:
            class_averages[sub_code] = 0

    # 8. Build per-student data (including per-subject marks for broadsheet)

    student_data = {}
    for student in students:
        summary = summaries.get(student.id)
        student_marks = marks_by_student.get(student.id, [])

        # Compute totals from marks if no summary
        total_marks = 0
        total_points = 0
        subject_count = 0
        for mark in student_marks:
            if not mark.is_absent and mark.score is not None:
                total_marks += mark.score
                total_points += mark.points or 0
                subject_count += 1

        # Build per-subject marks with levels for broadsheet display
        marks_dict = {}
        for mark in student_marks:
            if mark.subject and mark.subject.code not in marks_dict:
                if mark.is_absent:
                    marks_dict[mark.subject.code] = {'score': 'AB', 'level': 'AB', 'subject_id': mark.subject_id}
                elif mark.score is not None:
                    if is_primary:
                        lv, _ = _get_primary_performance(
                            mark.score, school=school, section=section,
                            sub_section=sub_section, subject_id=mark.subject_id,
                        )
                    else:
                        lv, _ = get_performance_level(
                            mark.score, sub_section=sub_section,
                            subject_id=mark.subject_id, school=school, section=section,
                        )
                    marks_dict[mark.subject.code] = {'score': mark.score, 'level': lv, 'subject_id': mark.subject_id}
                else:
                    marks_dict[mark.subject.code] = {'score': '-', 'level': '-', 'subject_id': mark.subject_id}

        # Prefer ExamSummary; fall back to marks so snapshot grade/gender
        # breakdowns are never all-zero when the Celery summary task lagged.
        if summary:
            overall_plv = summary.overall_plv or '-'
            mean_points = float(summary.mean_points) if summary else 0
        elif subject_count and total_marks:
            if is_primary:
                overall_plv = calculate_primary_plv(
                    total_marks, subject_count,
                    sub_section=sub_section, school=school, section=section,
                )
            else:
                overall_plv = calculate_report_plv(
                    total_points, total_marks,
                    school=school, section=section,
                )
            mean_points = float(total_points) / float(subject_count)
        else:
            overall_plv = '-'
            mean_points = 0

        student_data[student.id] = {
            'student_id': student.id,
            'name': student.name,
            'admission_no': student.admission_no,
            'gender': student.gender,
            'stream': student.stream or '',
            'total_marks': summary.total_marks if summary else total_marks,
            'total_points': summary.total_points if summary else total_points,
            'subject_count': summary.subject_count if summary else subject_count,
            'mean_points': mean_points,
            'overall_plv': overall_plv,
            'stream_rank': summary.stream_rank if summary else 0,
            'grade_rank': summary.grade_rank if summary else 0,
            'marks': marks_dict,
        }

    # 9. Build subject summary data (with distributions for summary tables)
    from .constants import ORDERED_LEVELS, PRIMARY_PERF_LEVELS
    active_levels = PRIMARY_PERF_LEVELS if is_primary else ORDERED_LEVELS

    subject_data = {}
    for sub_code in published_subject_codes:
        teacher_names = []
        for _a in assignments.get(sub_code) or []:
            _t = _a.teacher_profile.get_full_title() if _a.teacher_profile else ''
            if _t and _t not in teacher_names:
                teacher_names.append(_t)
        sub_marks = [m for m in deduped_marks if m.subject.code == sub_code]
        valid_scores = [m.score for m in sub_marks if not m.is_absent and m.score is not None]

        # Build distribution for this subject
        distribution = {lvl: 0 for lvl in active_levels}
        for mark in sub_marks:
            if mark.is_absent or mark.score is None:
                continue
            if is_primary:
                lv, _ = _get_primary_performance(
                    mark.score, school=school, section=section,
                    sub_section=sub_section, subject_id=mark.subject_id,
                )
            else:
                lv, _ = get_performance_level(
                    mark.score, sub_section=sub_section,
                    subject_id=mark.subject_id, school=school, section=section,
                )
            if lv in distribution:
                distribution[lv] += 1

        mean_score = round(sum(valid_scores) / len(valid_scores), 2) if valid_scores else 0
        mean_points = round(sum(
            get_performance_level(mark.score, sub_section=sub_section, subject_id=mark.subject_id, school=school, section=section)[1]
            for mark in sub_marks if not mark.is_absent and mark.score is not None
        ) / len(valid_scores), 4) if valid_scores else 0

        subject_data[sub_code] = {
            'subject_name': sub_marks[0].subject.name if sub_marks else '',
            'subject_code': sub_code,
            'teacher_name': ', '.join(teacher_names),
            'class_average': class_averages.get(sub_code, 0),
            'total_score': sum(valid_scores),
            'highest': max(valid_scores) if valid_scores else 0,
            'lowest': min(valid_scores) if valid_scores else 0,
            'student_count': len(valid_scores),
            'absent_count': sum(1 for m in sub_marks if m.is_absent),
            'distribution': distribution,
            'mean_score': mean_score,
            'mean_points': mean_points,
        }

    # 10. Build analysis data
    # Gender distributions for Gender Summary table
    marks_by_gender = {'Male': [], 'Female': []}
    for mark in deduped_marks:
        if mark.student_id:
            for sid, sdata in student_data.items():
                if sid == mark.student_id:
                    g = sdata.get('gender', '')
                    if g in marks_by_gender:
                        marks_by_gender[g].append(mark)
                    break

    gender_analysis = {}
    for gender_label, gender_key in [('Girls', 'Female'), ('Boys', 'Male')]:
        g_marks = marks_by_gender.get(gender_key, [])
        g_entries = len([m for m in g_marks if not m.is_absent and m.score is not None])
        g_total = sum(m.score for m in g_marks if not m.is_absent and m.score is not None)
        g_dist = {lvl: 0 for lvl in active_levels}
        g_pts_total = 0
        for m in g_marks:
            if m.is_absent or m.score is None:
                continue
            if is_primary:
                lv, pts = _get_primary_performance(m.score, school=school, section=section, sub_section=sub_section, subject_id=m.subject_id)
            else:
                lv, pts = get_performance_level(m.score, sub_section=sub_section, subject_id=m.subject_id, school=school, section=section)
            if lv in g_dist:
                g_dist[lv] += 1
            g_pts_total += pts
        g_mean = round(g_total / g_entries, 1) if g_entries else 0
        g_pts = round(g_pts_total / g_entries, 4) if g_entries else 0
        # Determine overall PLV from mean points
        g_plv = '—'
        if g_entries > 0:
            if is_primary:
                g_plv, _ = _get_primary_performance(g_mean, school=school, section=section, sub_section=sub_section)
            else:
                g_plv, _ = get_performance_level(g_mean, sub_section=sub_section, school=school, section=section)
        gender_analysis[gender_label] = {
            'dist': g_dist,
            'entries': g_entries,
            'mean_score': g_mean,
            'mean_points': g_pts,
            'performance_text': g_plv,
        }

    analysis = {
        'total_students': len(students),
        'boys_count': sum(1 for s in students if s.gender == 'Male'),
        'girls_count': sum(1 for s in students if s.gender == 'Female'),
        'stream_average': round(sum(class_averages.values()) / len(class_averages), 1) if class_averages else 0,
        'gender_analysis': gender_analysis,
    }

    # 11. Save snapshot (replace any existing row — unique on
    # school/term/year/exam/class/stream; a plain save() INSERTs and blows up
    # with IntegrityError if invalidate_report_caches didn't run first).
    from django.db import transaction
    with transaction.atomic():
        ExamResultSnapshot.all_objects.filter(
            school=school,
            term=exam.term,
            year=exam.year,
            exam_name=exam.name,
            class_name=class_name,
            stream=stream,
        ).delete()
        snapshot = ExamResultSnapshot(
            school=school,
            term=exam.term,
            year=exam.year,
            exam_name=exam.name,
            class_name=class_name,
            stream=stream,
            school_section=sample.school_section,
            sub_section=sample.sub_section,
            report_card_data=student_data,
            broadsheet_data=subject_data,
            analysis_data=analysis,
            student_count=len(students),
            published_by=None,
        )
        snapshot.save()

    return snapshot


def invalidate_exam_snapshots(school, exam, class_name=None, stream=None):
    """
    Delete snapshots when marks change or results are unpublished.
    If class_name/stream are provided, only delete that specific snapshot.
    Otherwise, delete all snapshots for the exam.
    """
    from ..models import ExamResultSnapshot

    qs = ExamResultSnapshot.all_objects.filter(
        school=school,
        term=exam.term,
        year=exam.year,
        exam_name=exam.name,
    )
    if class_name:
        qs = qs.filter(class_name=class_name)
    if stream:
        qs = qs.filter(stream=stream)

    count = qs.count()
    qs.delete()
    return count


def get_exam_snapshot(school, exam, class_name, stream):
    """
    Get the latest snapshot for a given combination.
    Returns None if no snapshot exists.
    """
    from ..models import ExamResultSnapshot

    return ExamResultSnapshot.get_latest(
        school=school,
        term=exam.term,
        year=exam.year,
        exam_name=exam.name,
        class_name=class_name,
        stream=stream,
    )


def build_analysis_from_snapshot(school, exam):
    """
    Build analysis page data from snapshots for ALL streams in the exam.
    Returns a dict with keys: streams, students_who_sat, student_ids, grade_name,
    subject_perf, stream_stats, grade_breakdown, overall_plv, subject_breakdowns,
    gender_streams, total_girls, total_boys.
    Returns None if no snapshots exist.
    """
    from ..models import ExamResultSnapshot, Student
    from collections import Counter

    # Find all snapshots for this exam
    snapshots = list(ExamResultSnapshot.all_objects.filter(
        school=school, term=exam.term, year=exam.year, exam_name=exam.name,
    ).order_by('class_name', 'stream'))

    if not snapshots:
        return None

    # Merge all student data across streams
    all_student_data = {}
    for snap in snapshots:
        for k, v in snap.report_card_data.items():
            all_student_data[int(k)] = v

    if not all_student_data:
        return None

    # Get distinct streams and class names
    streams = sorted(set(s.get('stream', '') for s in all_student_data.values() if s.get('stream')))
    class_names = sorted(set(snap.class_name for snap in snapshots))
    grade_name = class_names[0] if class_names else ''

    # Students who sat (have summary data)
    student_ids = list(all_student_data.keys())
    students_who_sat = len(student_ids)

    # ── Subject performance from broadsheet_data ────────────────────────
    # Merge across ALL snapshots: sum entries/scores/distribution, join
    # teachers. Taking the first snapshot wholesale showed only one
    # class/stream's numbers as the exam-wide total.
    merged_subjects = {}
    for snap in snapshots:
        for code, data in (snap.broadsheet_data or {}).items():
            m = merged_subjects.get(code)
            if m is None:
                m = {
                    **data,
                    'distribution': dict(data.get('distribution') or {}),
                    '_teachers': [],
                    '_pts_total': (data.get('mean_points') or 0) * (data.get('student_count') or 0),
                }
                merged_subjects[code] = m
            else:
                prev_n = m.get('student_count') or 0
                new_n = data.get('student_count') or 0
                m['student_count'] = prev_n + new_n
                m['absent_count'] = (m.get('absent_count') or 0) + (data.get('absent_count') or 0)
                m['total_score'] = (m.get('total_score') or 0) + (data.get('total_score') or 0)
                m['_pts_total'] += (data.get('mean_points') or 0) * new_n
                for lvl, cnt in (data.get('distribution') or {}).items():
                    m['distribution'][lvl] = m['distribution'].get(lvl, 0) + (cnt or 0)
                if new_n:
                    if prev_n:
                        m['highest'] = max(m.get('highest') or 0, data.get('highest') or 0)
                        m['lowest'] = min(m.get('lowest') or 100, data.get('lowest') or 100)
                    else:
                        m['highest'] = data.get('highest') or 0
                        m['lowest'] = data.get('lowest') or 0
            t_name = (data.get('teacher_name') or '').strip()
            if t_name and t_name not in m['_teachers']:
                m['_teachers'].append(t_name)
    for m in merged_subjects.values():
        _teachers = m.pop('_teachers', [])
        _pts_total = m.pop('_pts_total', 0)
        _n = m.get('student_count') or 0
        if _n > 0:
            m['mean_score'] = round((m.get('total_score') or 0) / _n, 2)
            m['class_average'] = m['mean_score']
            m['mean_points'] = round(_pts_total / _n, 4)
        if _teachers:
            m['teacher_name'] = ', '.join(_teachers)
        elif not m.get('teacher_name'):
            m['teacher_name'] = ''

    # Resolve subject codes to names
    from ..models import Subject
    subject_code_to_name = {}
    for subj in Subject.all_objects.filter(school=school):
        subject_code_to_name[str(subj.code)] = subj.name

    subject_perf = {}
    for code, data in merged_subjects.items():
        subj_name = subject_code_to_name.get(str(code), str(code))
        # Different codes can share one subject name (e.g. 902/KIS) —
        # accumulate instead of overwriting.
        sp = subject_perf.setdefault(
            subj_name,
            {'total_points': 0, 'count': 0, 'total_score': 0, 'plv_counts': {}, 'changes': []},
        )
        sp['total_points'] += data.get('mean_points', 0) * data.get('student_count', 0)
        sp['count'] += data.get('student_count', 0)
        sp['total_score'] += data.get('total_score', 0)
        for lvl, cnt in (data.get('distribution') or {}).items():
            sp['plv_counts'][lvl] = sp['plv_counts'].get(lvl, 0) + (cnt or 0)

    # ── Stream stats from per-stream student data ───────────────────────
    stream_stats = {}
    for s_name in streams:
        stream_students = {sid: d for sid, d in all_student_data.items() if d.get('stream') == s_name}
        entries = len(stream_students)
        if entries == 0:
            continue
        total_pts = sum(d.get('total_points', 0) for d in stream_students.values())
        total_marks = sum(d.get('total_marks', 0) for d in stream_students.values())
        # Count total subjects across all students
        total_subj = sum(d.get('subject_count', 0) for d in stream_students.values())
        mean_pts = round(total_pts / entries, 4) if entries else 0
        mean_marks = round(total_marks / total_subj, 1) if total_subj else 0
        stream_stats[s_name] = {
            'mean_points': mean_pts,
            'mean_marks': mean_marks,
            'entries': entries,
            '_total_pts': total_pts,
            '_total_marks': total_marks,
            '_count': entries,
        }

    # ── Grade breakdown (per-stream PLV distribution) ───────────────────
    breakdown_levels = ['EE', 'ME', 'AE', 'BE']
    grade_breakdown = []
    total_row = {'form': grade_name, 'entries': 0, 'X': 0, 'Y': 0}
    total_row.update({lvl: 0 for lvl in breakdown_levels})
    total_row.update({'mean_marks': 0, 'mm_dev': 0, 'mean_points': 0, 'mp_dev': 0, 'performance_level': '—'})

    for s_name in streams:
        stream_students = {sid: d for sid, d in all_student_data.items() if d.get('stream') == s_name}
        entries = len(stream_students)
        if entries == 0:
            continue
        dist = Counter()
        for d in stream_students.values():
            plv = (d.get('overall_plv') or '-').strip().upper()
            dist[plv] += 1
        total_marks = sum(d.get('total_marks', 0) for d in stream_students.values())
        total_subj = sum(d.get('subject_count', 0) for d in stream_students.values())
        total_pts = sum(d.get('total_points', 0) for d in stream_students.values())
        mean_m = round(total_marks / total_subj, 1) if total_subj else 0
        mean_p = round(total_pts / entries, 4) if entries else 0

        row = {
            'form': f'{grade_name} {s_name}',
            'entries': entries,
            'X': 0, 'Y': 0,
            'mean_marks': mean_m,
            'mean_points': mean_p,
        }
        for lvl in breakdown_levels:
            row[lvl] = dist.get(lvl, 0)
        grade_breakdown.append(row)

        total_row['entries'] += entries
        total_row['mean_marks'] += total_marks
        total_row['mean_points'] += total_pts
        for lvl in breakdown_levels:
            total_row[lvl] += row[lvl]

    # Overall row
    if total_row['entries'] > 0:
        total_row['mean_marks'] = round(total_row['mean_marks'] / sum(d.get('subject_count', 0) for d in all_student_data.values() or [1]), 1)
        total_row['mean_points'] = round(total_row['mean_points'] / total_row['entries'], 4)

    # Overall PLV from most common
    all_plvs = [(d.get('overall_plv') or '-').strip().upper() for d in all_student_data.values()]
    plv_counter = Counter(all_plvs)
    overall_plv = plv_counter.most_common(1)[0][0] if plv_counter else '—'

    # ── Subject breakdowns (per-subject, per-stream) ────────────────────
    subject_breakdowns = {}
    for code, data in merged_subjects.items():
        subj_name = subject_code_to_name.get(str(code), str(code))
        subject_breakdowns[subj_name] = {
            'rows': [],
            'total': {'entries': data.get('student_count', 0), 'mean_marks': data.get('mean_score', 0)},
        }

    # ── Gender streams ──────────────────────────────────────────────────
    gender_streams = {}
    total_girls = 0
    total_boys = 0
    for s_name in streams:
        stream_students = {sid: d for sid, d in all_student_data.items() if d.get('stream') == s_name}
        girls = sum(1 for d in stream_students.values() if d.get('gender') == 'Female')
        boys = sum(1 for d in stream_students.values() if d.get('gender') == 'Male')
        gender_streams[s_name] = {'girls': girls, 'boys': boys}
        total_girls += girls
        total_boys += boys

    return {
        'streams': streams,
        'students_who_sat': students_who_sat,
        'student_ids': student_ids,
        'grade_name': grade_name,
        'subject_perf': subject_perf,
        'stream_stats': stream_stats,
        'grade_breakdown': grade_breakdown,
        'total_row': total_row,
        'overall_plv': overall_plv,
        'subject_breakdowns': subject_breakdowns,
        'gender_streams': gender_streams,
        'total_girls': total_girls,
        'total_boys': total_boys,
    }


# ── Snapshot-based report-card context builder ───────────────────────────────

def build_report_card_context_from_snapshot(
    school,
    grade,
    stream,
    exam_id,
    *,
    student_ids=None,
    include_chart_svg=True,
    is_admin=False,
):
    """
    Build report card context.

    Report cards require Mark objects with decorated attributes that can't be
    stored in a snapshot. This function always delegates to the live
    build_report_card_context which reads from ExamSummary (already cached
    by Celery on publish).

    The snapshot is used only by broadsheet/results views where the data
    format is simpler.
    """
    return build_report_card_context(
        school, grade, stream, exam_id,
        student_ids=student_ids,
        include_chart_svg=include_chart_svg,
        is_admin=is_admin,
    )


# ── Snapshot-based broadsheet data builder ───────────────────────────────────

def get_broadsheet_from_snapshot(school, grade, stream, year, term, exam_name):
    """
    Get broadsheet data from a pre-computed snapshot.

    Returns (student_data, subject_data, analysis_data) or None if no snapshot.
    """
    from ..models import ExamResultSnapshot

    snapshot = ExamResultSnapshot.get_latest(
        school=school,
        term=term,
        year=year,
        exam_name=exam_name,
        class_name=grade,
        stream=stream,
    )

    if not snapshot:
        return None

    return (
        snapshot.report_card_data or {},
        snapshot.broadsheet_data or {},
        snapshot.analysis_data or {},
    )


# ── Unified report-card context builder ──────────────────────────────────────

def build_report_card_context(
    school,
    grade,
    stream,
    exam_id,
    *,
    student_ids=None,
    include_chart_svg=True,
    is_admin=False,
):
    # `exam_id` may be an int PK, a numeric string, or a DB exam name like
    # "Opener Assessment" — the latter is what the bulk-PDF view receives.
    # Resolve to the canonical Exam row before doing anything else.
    from ..models import Exam as _Exam
    try:
        exam_pk = int(exam_id)
        exam = _Exam.all_objects.get(id=exam_pk, school=school, is_deleted=False)
    except (TypeError, ValueError, _Exam.DoesNotExist):
        exam = (
            _Exam.all_objects
            .filter(school=school, name=str(exam_id), is_deleted=False)
            .order_by('-year', 'term')
            .first()
        )
        if not exam:
            raise _Exam.DoesNotExist(
                f"No Exam matches id/name={exam_id!r} for school={school.pk}"
            )
    """
    Single source of truth for everything the report_card.html template needs.

    Used by:
      - report_forms_display       (students_mgmt.py) — web preview for the whole class
      - download_bulk_report_pdf   (pdf_exports.py)   — server-side PDF for a batch

    Returns a dict with the bulk "report context" structure:
        {
            'student_marks_list': [<per-student dict the template iterates>],
            'selected_year':      str,
            'selected_term':      str,
            'selected_assessment': str,           # display label ("End Term")
            'selected_assessment_raw': str,       # DB exam.name
            'selected_grade':      str,
            'selected_stream':     str,
            'class_count':         int,           # denominator for "X / Y" position
            'closing_date':        date|None,
            'opening_date':        date|None,
            'section_accent':      str (hex),
            'grade_descriptors':   list,          # for the descriptors table
            'max_points_per_subj': int,
            'sample_school_section': str,
            'sample_sub_section':     str|None,
        }

    The helper resolves the Exam once, prefetches the grading scale, fetches
    every student's marks + ExamSummary in flat queries (no N+1), and computes
    position / totals / PLV / chart data with a single uniform path so the
    on-screen preview and the printed PDF can never disagree.

    Falls back gracefully (no ValueError) when ExamSummary rows are missing —
    the original per-student mark list is aggregated in Python instead, which
    keeps historical reports renderable even if the Celery snapshot is stale.
    """
    import base64
    import datetime as _dt
    import json as _json

    from ..models import (
        ClassTeacherMasterComment,
        Exam,
        ExamSummary,
        Mark,
        SchoolHeadteacherComment,
        Student,
        Subject,
        SubjectAssignment,
        Teacher,
    )
    from ..school_scope import get_current_school_section
    from django.db.models import Q, Sum
    from .constants import (
        LOWER_PRIMARY_SUBJECT_NAMES,
        PRIMARY_SUBJECT_NAMES,
        SUBJECT_DISPLAY_ORDER,
        SUBJECT_SHORT_MAP,
        PRIMARY_SUBJECT_SHORT_MAP,
    )
    from .exams import _get_primary_performance

    # ── 1. Exam already resolved above (int pk / string name accepted) ─────────
    year          = exam.year
    term          = exam.term
    db_assessment = exam.name

    # Term-date fallback for closing / opening dates on report cards
    _term_closing, _term_opening = resolve_term_dates(school, year, term)

    # ── 2. Determine section from a sample student ────────────────────────────
    students_qs = Student.all_objects.filter(
        school=school, class_name=grade, stream=stream, is_active=True,
    ).order_by('name')
    if student_ids:
        students_qs = students_qs.filter(id__in=student_ids)
    selected_students = list(students_qs)
    if not selected_students:
        return {
            'student_marks_list': [],
            'selected_year': year, 'selected_term': term,
            'selected_assessment': db_assessment, 'selected_assessment_raw': db_assessment,
            'selected_grade': grade, 'selected_stream': stream,
            'class_count': 0, 'closing_date': None, 'opening_date': None,
            'section_accent': '#305CDE', 'grade_descriptors': [],
            'max_points_per_subj': 8, 'sample_school_section': 'JSS',
            'sample_sub_section': None,
        }

    sample = selected_students[0]
    is_primary       = sample.school_section == 'PRIMARY'
    is_lower_primary = is_primary and sample.sub_section == 'LOWER'

    # ── 3. Prefetch grading & resolve descriptors once ─────────────────────────
    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)
    grade_descriptors   = resolve_scale_fast(school.pk, sample.school_section, sample.sub_section)
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))

    # ── 4. Published subjects (single query) ──────────────────────────────────
    published_subject_codes = get_published_subject_codes(
        grade, stream, year, term, db_assessment,
        sub_section=sample.sub_section if is_primary else None,
        is_admin=is_admin,
    )
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}

    # ── 5. All marks for the class in a single index-optimized query ─────────
    all_marks_qs = Mark.all_objects.filter(
        school=school, year=year, term=term, exam_type=db_assessment,
        subject__in=published_subjects_qs, school_section=sample.school_section,
        student__class_name=grade, student__stream=stream,
    ).select_related('subject').order_by('subject', '-date_recorded', '-id')

    marks_by_student = {}
    for mark in all_marks_qs:
        marks_by_student.setdefault(mark.student_id, []).append(mark)
    # One cell per subject code — a stray/duplicate row must not print twice
    for _sid in marks_by_student:
        marks_by_student[_sid] = dedup_marks_latest_by_code(marks_by_student[_sid])

    # ── 6. Grade-wide ExamSummary for rank + counts ──────────────────────────
    summaries_qs = ExamSummary.all_objects.filter(
        school=school,
        student__class_name=grade, year=year, term=term, exam_name=db_assessment,
        school_section=sample.school_section, sub_section=sample.sub_section,
    ).select_related('student')
    grade_sorted    = sorted(summaries_qs, key=lambda s: (-s.total_marks, -s.total_points))
    grade_rank_map  = {s.student_id: rank for rank, s in enumerate(grade_sorted, start=1)}
    total_class_count = len(grade_sorted)
    summaries_by_id = {s.student_id: s for s in summaries_qs}

    # ── 7. Bulk totals fallback (one query, all selected students) ────────────
    totals_map = {
        row['student_id']: row for row in
        Mark.all_objects.filter(
            school=school, year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs, school_section=sample.school_section,
            student__in=selected_students,
        ).values('student_id').annotate(total_score=Sum('score'), total_pts=Sum('points'))
    }

    # ── 8. Class averages (Redis cached) ──────────────────────────────────────
    class_avg_map = get_cached_class_averages(
        school, grade, stream, year, term, db_assessment, published_subjects_qs,
    )

    # ── 9. Subject teacher map ────────────────────────────────────────────────
    teacher_map = {
        a.subject.code: (a.teacher_profile.get_full_title() if a.teacher_profile else '—')
        for a in SubjectAssignment.all_objects.filter(
            school=school, class_name=grade, stream=stream, is_active=True,
        ).select_related('teacher_profile__user', 'subject')
        if a.subject
    }

    # ── 10. Class teacher name (string match on assigned_task) ────────────────
    class_teacher_name = ""
    ct_q = Teacher.all_objects.filter(
        school=school, assigned_task__icontains=grade,
    ).filter(Q(assigned_task__icontains=stream)).select_related('user').first()
    if ct_q:
        class_teacher_name = ct_q.get_full_title()
    class_teacher_signature = ct_q.signature.url if (ct_q and ct_q.signature) else ""

    # ── 11. Master comments (class teacher + headteacher) ─────────────────────
    master_comment = ClassTeacherMasterComment.objects.filter(
        school=school, year=year, term=term, grade=grade,
        stream=stream, exam_type=db_assessment,
    ).first()
    school_ht_comment = SchoolHeadteacherComment.objects.filter(
        school=school, year=year, term=term, exam_type=db_assessment,
        school_section=sample.school_section,
    ).first()

    freeze_threshold = _dt.timedelta(days=30)
    now              = _dt.datetime.now(_dt.timezone.utc)

    # ── 12. Build per-student context dicts ───────────────────────────────────
    _short_map = PRIMARY_SUBJECT_SHORT_MAP if is_primary else SUBJECT_SHORT_MAP

    student_marks_list = []
    for student in selected_students:
        marks = sorted(
            marks_by_student.get(student.id, []),
            key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99),
        )

        # Totals — try ExamSummary first, fall back to live aggregation
        summary = summaries_by_id.get(student.id)
        if summary:
            total_marks, total_points, assessed_subjects = (
                summary.total_marks, summary.total_points, summary.subject_count,
            )
        else:
            totals_row    = totals_map.get(student.id, {})
            total_marks   = totals_row.get('total_score') or 0
            total_points  = totals_row.get('total_pts') or 0
            valid_scores  = [m.score for m in marks if m.score is not None and not m.is_absent]
            assessed_subjects = len(valid_scores) if valid_scores else 0

        # Per-mark decoration (subject_name, teacher, class_avg, deviation)
        for mark in marks:
            mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
            mark.teacher_name = teacher_map.get(mark.subject.code, '—')
            if is_primary and not mark.is_absent:
                pct = mark.score or 0
                mark.performance_level, mark.points = _get_primary_performance(
                    pct, school=school, section=student.school_section,
                    sub_section=student.sub_section,
                )
            class_avg = class_avg_map.get(mark.subject.code)
            mark.class_average = class_avg
            if class_avg is not None and mark.score is not None and not mark.is_absent:
                mark.deviation = round(mark.score - class_avg, 1)
            else:
                mark.deviation = None

        # Aggregates for the stat row
        if summary and summary.mean_points is not None:
            mean_points = float(summary.mean_points)
        else:
            mean_points = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
        max_total_marks  = assessed_subjects * 100
        max_total_points = assessed_subjects * max_points_per_subj

        # Chart payload — identical for web and PDF (short_labels included)
        chart_labels       = [m.subject_name for m in marks if not m.is_absent]
        chart_short_labels = [_short_map.get(m.subject.code, m.subject_code if hasattr(m, 'subject_code') else m.subject.code) for m in marks if not m.is_absent]
        chart_student      = [m.score for m in marks if not m.is_absent]
        chart_class_avg    = [class_avg_map.get(m.subject.code, 0) for m in marks if not m.is_absent]

        chart_data_json = _json.dumps({
            'labels':       chart_labels,
            'short_labels': chart_short_labels,
            'student':      chart_student,
            'class_avg':    chart_class_avg,
            'student_name': student.name.split()[0] if student.name else 'Student',
            'class_name':   f"{student.class_name} {student.stream}".strip(),
        })
        chart_data_json_b64 = base64.b64encode(chart_data_json.encode('utf-8')).decode('ascii')

        # Server-side vector chart (used by the PDF path).
        # matplotlib with a long-lived Figure/Axes pair - see pdf_exports.py
        # for the rationale. Cached per (student, exam) in Redis.
        chart_svg = ''
        if include_chart_svg and chart_labels:
            from school.cache_keys import sanitize_cache_part
            chart_cache_key = (
                f"student_chart_{student.id}_{year}_"
                f"{sanitize_cache_part(term)}_{sanitize_cache_part(db_assessment)}"
            )
            from django.core.cache import cache as _cache
            chart_svg = _cache.get(chart_cache_key)
            if not chart_svg:
                try:
                    from .pdf_exports import generate_premium_vector_chart_svg
                    chart_svg = generate_premium_vector_chart_svg(
                        chart_labels, chart_student, chart_class_avg,
                    )
                    if chart_svg:
                        _cache.set(chart_cache_key, chart_svg, timeout=86400)
                except Exception:
                    chart_svg = ''

        # Position — prefer grade-wide sort; 0 means "no rank yet"
        position = grade_rank_map.get(student.id, 0)

        # Overall PLV (single source of truth — read from cache, else compute)
        if summary and summary.overall_plv:
            overall_plv = summary.overall_plv
        else:
            overall_plv = (
                '-' if assessed_subjects == 0
                else calculate_primary_plv(
                    total_marks, assessed_subjects,
                    sub_section=student.sub_section, school=school,
                    section=student.school_section,
                ) if is_primary
                else calculate_report_plv(
                    total_points, total_marks, school=school,
                    section=student.school_section,
                )
            )

        # Class-teacher + headteacher comment selection (live -> frozen fallback)
        class_teacher_remark = ''
        headteacher_comment  = ''
        closing_date         = None
        opening_date         = None

        if master_comment and overall_plv not in ('', '-'):
            ct_field = f"comment_{overall_plv.lower()}"
            live_ct  = getattr(master_comment, ct_field, '') or ''
            if live_ct.strip():
                class_teacher_remark = live_ct
            elif marks and marks[0].frozen_class_teacher_comment:
                class_teacher_remark = marks[0].frozen_class_teacher_comment

        if school_ht_comment and overall_plv not in ('', '-'):
            ht_field = f"ht_comment_{overall_plv.lower()}"
            live_ht  = getattr(school_ht_comment, ht_field, '') or ''
            if live_ht.strip():
                headteacher_comment = live_ht
            elif marks and marks[0].frozen_headteacher_comment:
                headteacher_comment = marks[0].frozen_headteacher_comment

        if master_comment:
            closing_date = master_comment.closing_date
            opening_date = master_comment.opening_date
        if not closing_date and marks and marks[0].frozen_closing_date:
            closing_date = marks[0].frozen_closing_date
        if not opening_date and marks and marks[0].frozen_opening_date:
            opening_date = marks[0].frozen_opening_date
        if not closing_date and _term_closing:
            closing_date = _term_closing
        if not opening_date and _term_opening:
            opening_date = _term_opening

        student_marks_list.append({
            'student':              student,
            'marks':                list(marks),
            'total_marks':          total_marks,
            'total_points':         total_points,
            'overall_plv':          overall_plv,
            'mean_points':          mean_points,
            'mean_points_max':      max_points_per_subj,
            'max_total_marks':      max_total_marks,
            'max_total_points':     max_total_points,
            'grade_descriptors':    grade_descriptors,
            'chart_data_json':      chart_data_json,
            'chart_data_json_b64':  chart_data_json_b64,
            'chart_svg':            chart_svg or '',
            'class_teacher_remark': class_teacher_remark,
            'class_teacher_name':   class_teacher_name,
            'class_teacher_signature': class_teacher_signature,
            'headteacher_comment':  headteacher_comment,
            'closing_date':         closing_date,
            'opening_date':         opening_date,
            'position':             position,
            'class_count':          total_class_count,
        })

    student_marks_list.sort(key=lambda x: (x['position'] == 0, x['position']))

    # ── 13. Section accent colour ──────────────────────────────────────────────
    _section_colors = {'JSS': '#305CDE', 'PRIMARY': '#00674F', 'LOWER_PRIMARY': '#B45309'}
    if is_lower_primary:
        section_accent = _section_colors['LOWER_PRIMARY']
    elif is_primary:
        section_accent = _section_colors['PRIMARY']
    else:
        section_accent = _section_colors['JSS']

    # Display label for the assessment ("End Term", "Mid Term", "Opener")
    _lower = db_assessment.lower()
    if 'end'   in _lower: display_assessment = 'End Term'
    elif 'mid' in _lower: display_assessment = 'Mid Term'
    elif 'open' in _lower: display_assessment = 'Opener'
    else:                 display_assessment = db_assessment

    return {
        'student_marks_list':      student_marks_list,
        'selected_year':           year,
        'selected_term':           term,
        'selected_assessment':     display_assessment,
        'selected_assessment_raw': db_assessment,
        'selected_grade':          grade,
        'selected_stream':         stream,
        'class_count':             total_class_count,
        'closing_date':            master_comment.closing_date if master_comment else None,
        'opening_date':            master_comment.opening_date if master_comment else None,
        'section_accent':          section_accent,
        'grade_descriptors':       grade_descriptors,
        'max_points_per_subj':     max_points_per_subj,
        'sample_school_section':   sample.school_section,
        'sample_sub_section':      sample.sub_section,
    }


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
    from school.cache_keys import sanitize_cache_part
    return (
        f"report_forms:{school_id}:"
        f"{sanitize_cache_part(grade)}:{sanitize_cache_part(stream)}:{exam_id}"
    )


def invalidate_report_forms_cache(school_id, grade=None, stream=None, exam_id=None):
    """
    Evict cached report forms data when marks are modified.
    If grade/stream/exam_id are provided, evict only that specific key.
    Otherwise, evict all report_forms cache entries for the school.
    """
    if grade and stream and exam_id:
        key = get_report_forms_cache_key(school_id, grade, stream, exam_id)
        try:
            cache.delete(key)
        except Exception:
            pass
    else:
        # Evict all report_forms cache entries for this school
        _delete_cache_key_pattern(f"report_forms:{school_id}:*")


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