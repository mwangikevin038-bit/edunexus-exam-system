"""
Helper functions for the students views module.

Provides utilities for authentication, access control, student ordering,
subject-aware queries, and performance-level calculations used by the
various view layers.
"""

import random
import secrets
import string

from django.db.models import Count, Q, IntegerField
from django.db.models.functions import Cast

from .constants import (
    ASSESSMENT_SLUG_MAP,
    GRADE_CHOICES,
    RELIGION_SUBJECTS,
    RELIGION_TAG,
)
from ..models import Mark, MarkSubmission, Student, SubjectAssignment, Teacher
from ..school_scope import get_current_school, get_current_school_section
from ..security import user_has_main_school_admin_override


def generate_default_password():
    """Generate a random 12-character alphanumeric password for new teachers."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(12))


def get_published_subject_codes(class_name, stream, year, term, exam_name):
    """
    Return subject codes that have been formally published by the school admin.
    Official analysis and report cards should only use these finalized sheets.
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
    if section == 'LOWER_PRIMARY':
        filters['school_section'] = 'PRIMARY'
        filters['sub_section'] = 'LOWER'
    elif section == 'PRIMARY':
        filters['school_section'] = 'PRIMARY'
        filters['sub_section'] = 'UPPER'
    elif section == 'JSS':
        filters['school_section'] = 'JSS'
    return set(
        MarkSubmission.objects.filter(**filters).values_list("subject__code", flat=True)
    )


def get_published_contexts_for_user(user, require_class_teacher=False):
    """
    Return all published assessment contexts visible to any logged-in user.
    Results lists are read-only and visible to every authenticated teacher.
    Report cards call this with require_class_teacher=True for private scoping.
    """
    school = get_current_school()
    section = get_current_school_section()
    qs = MarkSubmission.objects.filter(status="published")
    if school:
        qs = qs.filter(school=school)
    if section == 'LOWER_PRIMARY':
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
        SubjectAssignment.objects.filter(**assignment_filters)
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

        submission = MarkSubmission.objects.filter(
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
        student_qs = Student.all_objects.all()
        if school:
            student_qs = student_qs.filter(school=school)
        if section == 'JSS':
            student_qs = student_qs.filter(school_section='JSS')
        elif section == 'PRIMARY':
            student_qs = student_qs.filter(school_section='PRIMARY')
        elif section == 'LOWER_PRIMARY':
            student_qs = student_qs.filter(school_section='PRIMARY', sub_section='LOWER')
    else:
        student_qs = Student.objects.all()

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


def get_teacher_for_user(user):
    """Return the Teacher instance linked to the given user, or None."""
    if not user.is_authenticated:
        return None
    return Teacher.objects.filter(user=user).first()


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

    for grade in all_grades:
        for stream in all_streams:
            if grade in task and stream in task:
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


def get_performance_level(score, sub_section=None):
    """
    Return (performance_level, points) for a converted 100% score.
    Uses the school's GradingConfig from the DB. NO hardcoded fallback.

    Looks up by (school_section, sub_section) so LOWER and UPPER primary
    can have different scales. Pass sub_section explicitly when calling
    from a context that knows it (e.g. iterating marks for a class).

    If config is missing, logs an error and returns ('NO CONFIG', 0).
    """
    import logging
    from ..models import GradingConfig
    from ..school_scope import get_current_school, get_current_school_section

    score = max(0, min(100, round(score or 0)))

    school = get_current_school()
    section = get_current_school_section()

    if school and section:
        # Try the sub-section-specific config first
        if sub_section:
            config = GradingConfig.all_objects.filter(
                school=school, school_section=section, sub_section=sub_section
            ).first()
            if config and config.subject_scale:
                return config.get_subject_level(score)
        # Fallback to the section-wide config
        config = GradingConfig.all_objects.filter(
            school=school, school_section=section
        ).first()
        if config and config.subject_scale:
            return config.get_subject_level(score)
        # For LOWER_PRIMARY, also try PRIMARY/LOWER as fallback
        if section == 'LOWER_PRIMARY':
            config = GradingConfig.all_objects.filter(
                school=school, school_section='PRIMARY', sub_section='LOWER'
            ).first()
            if config and config.subject_scale:
                return config.get_subject_level(score)
        # For PRIMARY (upper), also try PRIMARY/UPPER as fallback
        if section == 'PRIMARY':
            config = GradingConfig.all_objects.filter(
                school=school, school_section='PRIMARY', sub_section='UPPER'
            ).first()
            if config and config.subject_scale:
                return config.get_subject_level(score)

    logging.getLogger("students.helpers").error(
        "GradingConfig missing for school_id=%s section=%s sub_section=%s. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section, sub_section,
    )
    return 'NO CONFIG', 0


def calculate_report_plv(total_points, total_marks, sub_section=None):
    """
    2-tier JSS Performance Level used for report card comment matching.
    Uses the school's GradingConfig.total_scale from the DB.
    NO hardcoded fallback — if config is missing, logs error and returns '-'.
    """
    import logging
    from ..models import GradingConfig
    from ..school_scope import get_current_school, get_current_school_section

    pts = total_points or 0
    mks = total_marks  or 0

    school = get_current_school()
    section = get_current_school_section()

    if school and section:
        if sub_section:
            config = GradingConfig.all_objects.filter(
                school=school, school_section=section, sub_section=sub_section
            ).first()
            if config and config.total_scale:
                return config.get_total_level(mks)[0] if mks else '-'
        config = GradingConfig.all_objects.filter(school=school, school_section=section).first()
        if config and config.total_scale:
            return config.get_total_level(mks)[0] if mks else '-'

    logging.getLogger("students.helpers").error(
        "GradingConfig.total_scale missing for school_id=%s section=%s sub_section=%s. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section, sub_section,
    )
    return '-'


def calculate_broadsheet_plv(total_marks, total_points, sub_section=None):
    """
    Overall broadsheet level based on the learner's total performance points
    and raw total mark, keeping it consistent with report card PLV thresholds.
    """
    if not total_points and not total_marks:
        return '-'
    return calculate_report_plv(total_points, total_marks, sub_section)


def calculate_primary_plv(total_marks, assessed_subjects, sub_section=None):
    """
    Primary broadsheet PLV based on the school's GradingConfig.

    For primary, PLV is computed from the **mean percentage** across
    assessed subjects (via subject_scale). This works correctly for any
    number of subjects (4 in Grade 1-3, 5 in Grade 5, 7-9 in Grade 6).

    The total_scale (absolute marks) is intentionally NOT used for primary
    because the configured range (e.g. 0-400) only fits a 4-subject class.
    It is used for JSS where the total scale (0-800) matches the 8 subjects.

    Lookup order:
      1. Sub-section-specific config (PRIMARY/LOWER or PRIMARY/UPPER)
      2. Falls back to section-wide PRIMARY config
    NO hardcoded fallback.
    """
    import logging
    from ..models import GradingConfig
    from ..school_scope import get_current_school, get_current_school_section

    if not assessed_subjects or not total_marks:
        return '-'

    school = get_current_school()
    section = get_current_school_section()

    if school and section:
        # Try sub-section-specific config first
        config = None
        if sub_section:
            config = GradingConfig.all_objects.filter(
                school=school, school_section=section, sub_section=sub_section
            ).first()
        if not config:
            config = GradingConfig.all_objects.filter(
                school=school, school_section=section
            ).first()
        if config and config.subject_scale:
            mean = total_marks / assessed_subjects
            level, _ = config.get_subject_level(mean)
            if level and level != '-':
                return level

    logging.getLogger("students.helpers").error(
        "GradingConfig missing or unusable for school_id=%s section=%s sub_section=%s. "
        "Primary PLV cannot be resolved. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section, sub_section,
    )
    return '-'


def get_next_admission_no():
    """
    Compute the next sequential admission number as a zero-padded string.
    Skips non-numeric admission numbers safely.
    """
    last = (
        Student.objects.all()
        .filter(admission_no__regex=r'^[0-9]+$')
        .annotate(adm_int=Cast('admission_no', IntegerField()))
        .order_by('adm_int')
        .last()
    )
    if last and last.admission_no:
        try:
            return f"{int(last.admission_no) + 1:03}"
        except ValueError:
            pass
    return '001'


def get_students_ordered(grade, stream):
    """
    Return students filtered by grade and stream, ordered by admission number.
    Non-numeric admission numbers are sorted to the end.
    """
    from django.db.models import Value, CharField
    numeric = (
        Student.objects
        .filter(class_name=grade, stream=stream)
        .filter(admission_no__regex=r'^[0-9]+$')
        .annotate(adm_int=Cast('admission_no', IntegerField()))
        .order_by('adm_int')
    )
    non_numeric = (
        Student.objects
        .filter(class_name=grade, stream=stream)
        .exclude(admission_no__regex=r'^[0-9]+$')
        .order_by('admission_no')
    )
    return list(numeric) + list(non_numeric)


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
    marks = Mark.objects.filter(
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
        if Student.objects.filter(**religion_filter).exists():
            marks = marks.filter(student__religion=religion_tag)
    return marks


def get_religion_aware_student_count(class_name, stream, subject):
    """Return the count of students eligible for the given subject."""
    subject_code = subject.code if hasattr(subject, 'code') else subject
    return len(get_subject_students(class_name, stream, subject_code))