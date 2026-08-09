"""
Exam & Mark Entry Views
========================
Handles teacher mark entry, admin exam management, stream-level and
individual submission reviews, and assessment lock management.
"""

import datetime
import json
import re
import time

import bleach
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Avg, Count, IntegerField, Q
from django.db.models.functions import Cast
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .helpers import invalidate_report_caches

from .constants import (
    ASSESSMENT_MAP,
    GRADE_CHOICES,
    JSS_GRADE_CHOICES,
    OPPOSITE_RELIGION_SUBJECT,
    RELIGION_SUBJECTS,
    RELIGION_TAG,
    TERM_CHOICES,
)
from .helpers import (
    get_performance_level,
    get_religion_aware_student_count,
    get_stream_submission_summary,
    get_subject_level_fast,
    get_subject_marks,
    get_subject_students,
    upsert_mark,
)
from ..models import (
    AssessmentLock,
    Exam,
    Grade,
    GradingConfig,
    Mark,
    MarkSubmission,
    Student,
    Subject,
    SubjectAssignment,
    Teacher,
)
from ..security import (
    get_request_school,
    get_request_school_section,
    get_school_object_or_403,
    rate_limit,
    school_admin_required,
    tenant_read_only_required,
    user_has_main_school_admin_override,
)
from ..security.roles import user_can_mutate_marks
from ..school_scope import get_current_school, get_current_school_section


def _htmx_redirect(request, url):
    """Return HX-Redirect header for HTMX requests, standard redirect otherwise."""
    if request.headers.get('HX-Request'):
        resp = HttpResponse()
        resp['HX-Redirect'] = url
        return resp
    return redirect(url)


def _get_grading_scale_json():
    """Return the subject grading scale for the current section as a JSON string."""
    school = get_current_school()
    section = get_current_school_section()
    if school and section:
        from .grading_engine import prefetch_school_grading, resolve_scale_fast
        prefetch_school_grading(school)
        scale_data = resolve_scale_fast(school.pk, section, None, subject_id=None)
        if scale_data:
            return json.dumps(scale_data)
    from ..models import GradingConfig
    if section in ('LOWER_PRIMARY', 'PRIMARY'):
        return json.dumps(GradingConfig.get_default_subject_scale(section))
    return json.dumps(GradingConfig.get_default_subject_scale('JSS'))


def _resolve_opposite_religion_subject(school, assignment):
    """Resolve OPPOSITE_RELIGION_SUBJECT code string to a Subject FK instance."""
    opposite_code = OPPOSITE_RELIGION_SUBJECT.get(assignment.subject.code)
    if not opposite_code:
        return None
    return Subject.objects.filter(
        school=school, code=opposite_code,
        school_section=assignment.school_section,
        grade=assignment.class_name,
    ).first()


@login_required(login_url='login')
@tenant_read_only_required
@rate_limit("mark_entry", max_requests=30, window_seconds=60, methods=["POST"])
def select_exam(request):
    """
    Teacher mark entry screen. Loads the teacher's subject assignments and
    active exams, then processes score submissions for a selected combination.
    """
    if not user_can_mutate_marks(request.user):
        raise PermissionDenied("Only teachers and school admins may enter marks.")

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        messages.error(request, "No teacher profile is linked to this account.")
        return redirect('home_alt')

    assignment_id = request.GET.get('assignment_id') or request.POST.get('assignment_id')
    exam_id = request.GET.get('exam_id') or request.POST.get('exam_id')

    school = get_request_school(request)

    assignments = (
        SubjectAssignment.objects
        .filter(school=school, teacher_profile=teacher, school_section='JSS')
        .select_related('subject', 'teacher_profile__user', 'teacher_profile')
        .order_by('class_name', 'stream', 'subject__code')
    )

    active_exams = Exam.objects.filter(school=school, status='active', school_section='JSS', is_deleted=False).order_by('-year', 'term', 'name')

    selected_assignment = None
    selected_exam = None
    students = None
    submission = None
    is_locked = False
    is_submitted = False
    current_maximum_marks = 100

    if assignment_id and exam_id:
        selected_assignment = get_school_object_or_403(
            SubjectAssignment,
            request,
            id=assignment_id,
            teacher_profile=teacher,
        )

        selected_exam = get_school_object_or_403(
            Exam,
            request,
            id=exam_id,
            status='active',
        )

        is_locked = AssessmentLock.objects.filter(
            school=school,
            year=selected_exam.year,
            term=selected_exam.term,
            grade=selected_assignment.class_name,
            exam_type=selected_exam.name,
            is_locked=True,
        ).exists()

        submission = MarkSubmission.objects.filter(
            school=school,
            teacher=teacher,
            subject=selected_assignment.subject,
            class_name=selected_assignment.class_name,
            stream=selected_assignment.stream,
            exam_name=selected_exam.name,
            term=selected_exam.term,
            year=selected_exam.year,
            school_section=selected_assignment.school_section,
        ).first()

        is_submitted = submission is not None and submission.status in [
            "submitted",
            "approved",
            "published",
        ]

        existing_mark_for_max = Mark.objects.filter(
            school=school,
            subject=selected_assignment.subject,
            term=selected_exam.term,
            exam_type=selected_exam.name,
            year=selected_exam.year,
            student__class_name=selected_assignment.class_name,
            student__stream=selected_assignment.stream,
        ).first()

        if existing_mark_for_max:
            current_maximum_marks = existing_mark_for_max.maximum_marks or 100

        # ================================================================
        # CHANGE 1 — Smart student filtering for IRE/CRE/HRE
        # First time: show all students
        # Subsequent times: show only tagged students
        # ================================================================
        students = get_subject_students(
            selected_assignment.class_name,
            selected_assignment.stream,
            selected_assignment.subject,
        )

        # Batch-fetch all existing marks in ONE query instead of N queries
        student_ids = [s.id for s in students]
        existing_marks_qs = Mark.objects.filter(
            student_id__in=student_ids,
            subject=selected_assignment.subject,
            term=selected_exam.term,
            exam_type=selected_exam.name,
            year=selected_exam.year,
            school=school,
        )
        marks_by_student = {m.student_id: m for m in existing_marks_qs}

        for student in students:
            existing = marks_by_student.get(student.id)

            if existing:
                if existing.is_absent:
                    student.current_score = "AB"
                    student.current_points = 0
                    student.current_percentage = "AB"
                elif existing.raw_score is not None:
                    student.current_score = existing.raw_score
                    student.current_points = existing.points
                    student.current_percentage = existing.score
                else:
                    student.current_score = existing.score
                    student.current_points = existing.points
                    student.current_percentage = existing.score
            else:
                student.current_score = ""
                student.current_points = ""
                student.current_percentage = ""

        if request.method == 'POST':
            if is_locked:
                messages.error(request, "This assessment sheet is locked by admin.")
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            if is_submitted:
                messages.error(request, "This sheet has already been submitted and cannot be edited. Ask the admin to return it first.")
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            try:
                maximum_marks = int(request.POST.get('maximum_marks', current_maximum_marks))
            except (ValueError, TypeError):
                maximum_marks = current_maximum_marks

            # ============================================================
            # PHASE 1 — Collect all input data in a single pass (zero DB)
            # ============================================================
            missing_students = []
            is_religion = selected_assignment.subject.code in RELIGION_SUBJECTS
            religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '') if is_religion else ''
            opposite_religion = _resolve_opposite_religion_subject(school, selected_assignment) if is_religion else None
            religion_student_ids = []
            opposite_religion_student_ids = []

            raw_inputs = []
            for student in students:
                value = request.POST.get(f'score_{student.id}', '').strip()

                if not value:
                    missing_students.append(student.name)
                    raw_inputs.append((student, None))
                    continue

                if value.upper() == "AB":
                    raw_inputs.append((student, "AB"))
                    if is_religion:
                        religion_student_ids.append(student.id)
                    continue

                try:
                    raw_score = int(value)
                except ValueError:
                    messages.error(request, f"Invalid score for {student.name}. Use a number or AB.")
                    return _htmx_redirect(request,
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                if raw_score < 0 or raw_score > maximum_marks:
                    messages.error(request, f"{student.name}'s score exceeds the total marks.")
                    return _htmx_redirect(request,
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                raw_inputs.append((student, raw_score))

            # Validate before touching DB — religion subjects allow blanks
            if missing_students and not is_religion:
                messages.error(request, "Please enter a score or AB for every learner before submitting.")
                return _htmx_redirect(request,
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            # ============================================================
            # PHASE 2 — Atomic bulk write: transaction.atomic + bulk_update
            # ============================================================
            deleted_count = 0
            saved_count = 0

            # ── Pre-fetch grading scale + HMAC key (single queries) ────
            from .grading_engine import prefetch_school_grading, resolve_scale_fast
            prefetch_school_grading(school)
            subject_id = selected_assignment.subject.id if selected_assignment.subject else None
            grading_scale = resolve_scale_fast(
                school.pk, selected_assignment.school_section, selected_assignment.sub_section,
                subject_id=subject_id,
            )
            from ..security.integrity import compute_mark_checksum

            with transaction.atomic():
                existing_marks = {
                    m.student_id: m for m in Mark.all_objects.filter(
                        subject=selected_assignment.subject,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                        school=school,
                        school_section=selected_assignment.school_section,
                        sub_section=selected_assignment.sub_section,
                    ).select_related('student', 'subject')
                }

                marks_to_create = []
                marks_to_update = []
                ids_to_delete = []

                for student, value in raw_inputs:
                    existing = existing_marks.get(student.id)

                    if value is None:
                        if existing:
                            ids_to_delete.append(existing.id)
                            deleted_count += 1
                        continue

                    if value == "AB":
                        if existing:
                            existing.raw_score = None
                            existing.maximum_marks = maximum_marks
                            existing.score = 0
                            existing.is_absent = True
                            existing.performance_level = 'AB'
                            existing.points = 0
                            existing.integrity_checksum = compute_mark_checksum(existing)
                            marks_to_update.append(existing)
                        else:
                            new_mark = Mark(
                                school=school,
                                student=student,
                                subject=selected_assignment.subject,
                                term=selected_exam.term,
                                exam_type=selected_exam.name,
                                year=selected_exam.year,
                                school_section=selected_assignment.school_section,
                                sub_section=selected_assignment.sub_section,
                                raw_score=None,
                                maximum_marks=maximum_marks,
                                score=0,
                                is_absent=True,
                                performance_level='AB',
                                points=0,
                            )
                            new_mark.integrity_checksum = compute_mark_checksum(new_mark)
                            marks_to_create.append(new_mark)
                        saved_count += 1
                        continue

                    if is_religion and opposite_religion:
                        opposite_religion_student_ids.append(student.id)

                    score = round((value / maximum_marks) * 100)

                    # Compute grading from percentage
                    if grading_scale:
                        perf_level, perf_points = get_subject_level_fast(score, grading_scale)
                    else:
                        perf_level, perf_points = '-', 0

                    if existing:
                        existing.raw_score = value
                        existing.maximum_marks = maximum_marks
                        existing.score = score
                        existing.is_absent = False
                        existing.performance_level = perf_level
                        existing.points = perf_points
                        existing.integrity_checksum = compute_mark_checksum(existing)
                        marks_to_update.append(existing)
                    else:
                        new_mark = Mark(
                            school=school,
                            student=student,
                            subject=selected_assignment.subject,
                            term=selected_exam.term,
                            exam_type=selected_exam.name,
                            year=selected_exam.year,
                            school_section=selected_assignment.school_section,
                            sub_section=selected_assignment.sub_section,
                            raw_score=value,
                            maximum_marks=maximum_marks,
                            score=score,
                            is_absent=False,
                            performance_level=perf_level,
                            points=perf_points,
                        )
                        new_mark.integrity_checksum = compute_mark_checksum(new_mark)
                        marks_to_create.append(new_mark)
                    saved_count += 1

                if ids_to_delete:
                    Mark.all_objects.filter(id__in=ids_to_delete).delete()
                if marks_to_create:
                    Mark.all_objects.bulk_create(marks_to_create, batch_size=250)
                if marks_to_update:
                    Mark.all_objects.bulk_update(
                        marks_to_update,
                        ['raw_score', 'maximum_marks', 'score', 'is_absent',
                         'performance_level', 'points', 'integrity_checksum'],
                        batch_size=250,
                    )

            # ============================================================
            # PHASE 3 — Post-atomic side effects (non-critical, no lock)
            # ============================================================
            if religion_student_ids:
                Student.objects.filter(id__in=religion_student_ids).update(religion=religion_tag)

            if opposite_religion and opposite_religion_student_ids:
                Mark.all_objects.filter(
                    school=school,
                    student_id__in=opposite_religion_student_ids,
                    subject=opposite_religion,
                    term=selected_exam.term,
                    exam_type=selected_exam.name,
                    year=selected_exam.year,
                    school_section=selected_assignment.school_section,
                ).delete()

            MarkSubmission.objects.update_or_create(
                school=school,
                teacher=teacher,
                subject=selected_assignment.subject,
                class_name=selected_assignment.class_name,
                stream=selected_assignment.stream,
                exam_name=selected_exam.name,
                term=selected_exam.term,
                year=selected_exam.year,
                school_section=selected_assignment.school_section,
                defaults={
                    "status": "submitted",
                    "admin_note": "",
                    "reviewed_at": None,
                    "published_at": None,
                }
            )

            messages.success(request, f"{saved_count} learner records submitted successfully." + (f" {deleted_count} mark(s) cleared." if deleted_count else ""))
            invalidate_report_caches(
                school.pk, selected_assignment.class_name, selected_assignment.stream,
                selected_exam.year, selected_exam.term, selected_exam.name,
            )
            return _htmx_redirect(request, 'select_exam')

    exam_rows = []

    # ==================================================================
    # PRE-COMPUTE — Religion existence + student pool (1 query)
    # ==================================================================
    religion_exists = {}
    total_eligible = {}
    if active_exams:
        unique_streams = {(a.class_name, a.stream) for a in assignments}
        student_rels = Student.all_objects.filter(school=school).values_list(
            'class_name', 'stream', 'religion',
        )
        rel_set = set()
        pool = {}
        for cn, st, rel in student_rels:
            rel_set.add((cn, st, rel))
            pool.setdefault((cn, st), []).append(rel)

        for cn, st in unique_streams:
            for code in RELIGION_SUBJECTS:
                religion_exists[(cn, st, code)] = (cn, st, RELIGION_TAG.get(code, '')) in rel_set

        for a in assignments:
            key = (a.class_name, a.stream, a.subject.code)
            rels = pool.get((a.class_name, a.stream), [])
            if a.subject.code in RELIGION_SUBJECTS and religion_exists.get(key):
                tag = RELIGION_TAG.get(a.subject.code, '')
                total_eligible[key] = sum(1 for r in rels if r == tag)
            else:
                total_eligible[key] = len(rels)

    # ==================================================================
    # AGGREGATION — Marks counts in 1 query instead of N×M
    # ==================================================================
    marks_agg = {}
    if active_exams:
        agg_filters = Q(student__school=school)
        exam_q = Q()
        for exam in active_exams:
            exam_q |= Q(term=exam.term, exam_type=exam.name, year=exam.year)
        agg_filters &= exam_q

        agg_rows = (
            Mark.all_objects
            .filter(agg_filters)
            .values(
                'student__class_name', 'student__stream', 'subject_id',
                'school_section', 'sub_section',
            )
            .annotate(
                captured=Count('id'),
                absent=Count('id', filter=Q(is_absent=True)),
            )
        )
        for row in agg_rows:
            marks_agg[(
                row['student__class_name'], row['student__stream'],
                row['subject_id'], row['school_section'], row['sub_section'],
            )] = (row['captured'], row['absent'])

    # ==================================================================
    # SUBMISSIONS — Batch-fetch (1 query)
    # ==================================================================
    all_submissions_qs = MarkSubmission.objects.filter(
        teacher=teacher,
        school=school,
    )
    if active_exams:
        exam_q = Q()
        for exam in active_exams:
            exam_q |= Q(exam_name=exam.name, term=exam.term, year=exam.year)
        all_submissions_qs = all_submissions_qs.filter(exam_q)
    submission_map = {
        (sub.subject_id, sub.class_name, sub.stream, sub.exam_name, sub.term, sub.year): sub
        for sub in all_submissions_qs
    }

    # ==================================================================
    # BUILD ROWS — Pure Python, zero DB queries
    # ==================================================================
    for exam in active_exams:
        for assignment in assignments:
            akey = (
                assignment.class_name, assignment.stream,
                assignment.subject_id, assignment.school_section,
                assignment.sub_section,
            )
            captured_count, absent_count = marks_agg.get(akey, (0, 0))
            total_students = total_eligible.get(
                (assignment.class_name, assignment.stream, assignment.subject.code), 0
            )
            missing_count = max(total_students - captured_count, 0)

            sub_key = (assignment.subject_id, assignment.class_name, assignment.stream, exam.name, exam.term, exam.year)
            row_submission = submission_map.get(sub_key)

            status_label = "Not Started"
            status_key = "not_started"

            if row_submission and row_submission.status == "returned":
                status_label = "Returned"
                status_key = "returned"
            elif row_submission and row_submission.status == "approved":
                status_label = "Approved"
                status_key = "approved"
            elif row_submission and row_submission.status == "published":
                status_label = "Published"
                status_key = "published"
            elif row_submission and row_submission.status == "submitted":
                status_label = "Submitted"
                status_key = "submitted"
            elif captured_count == 0:
                status_label = "Not Started"
                status_key = "not_started"
            elif missing_count == 0:
                status_label = "Ready"
                status_key = "ready"
            else:
                status_label = "In Progress"
                status_key = "in_progress"

            exam_rows.append({
                "exam": exam,
                "assignment": assignment,
                "status": status_label,
                "status_label": status_label,
                "status_key": status_key,
                "submission": row_submission,
            })

    exam_rows.sort(key=lambda r: (r['assignment'].class_name, r['assignment'].stream, r['exam'].name))

    is_htmx = request.headers.get('HX-Request') == 'true'
    template_name = 'students/select_exam_details_partial.html' if is_htmx else 'students/select_exam_details.html'
    
    return render(request, template_name, {
        'teacher': teacher,
        'exam_rows': exam_rows,
        'selected_assignment': selected_assignment,
        'selected_exam': selected_exam,
        'students': students,
        'is_locked': is_locked,
        'is_submitted': is_submitted,
        'submission': submission,
        'current_maximum_marks': current_maximum_marks,
        'grading_mode': 'jss',
        'grading_scale_json': _get_grading_scale_json(),
        'back_url': 'select_exam',
    })

@login_required(login_url='login')
@school_admin_required
def manage_exams(request):
    """
    Admin workspace for creating exams and monitoring teacher submissions.
    Uses the existing Manage Exams sidebar item. No extra sidebar link needed.
    """
    if not user_has_main_school_admin_override(request.user):
        messages.error(request, "You are not allowed to manage exams.")
        return redirect('select_exam')

    current_year = datetime.date.today().year
    active_tab = request.GET.get('tab', 'manage')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    status_choices = getattr(
        Exam,
        "STATUS_CHOICES",
        [
            ("draft", "Draft"),
            ("active", "Active"),
            ("closed", "Closed"),
        ],
    )

    # -----------------------------
    # Create / update / toggle exams
    # -----------------------------
    if request.method == "POST":
        action_type = request.POST.get("action_type")

        if action_type == "create_exam":
            exam_name = request.POST.get("exam_name", "").strip()
            term = request.POST.get("term")
            year = int(request.POST.get("year") or current_year)
            status = request.POST.get("status", "active")
            selected_classes = request.POST.getlist("selected_classes")

            if not exam_name or not term or not year:
                messages.error(request, "Please provide assessment name, term, and year.")
                return redirect(reverse('manage_exams') + '?tab=create')

            if not selected_classes:
                messages.error(request, "Please select at least one class.")
                return redirect(reverse('manage_exams') + '?tab=create')

            LOWER_PRIMARY_GRADES = ['Grade 1', 'Grade 2', 'Grade 3']
            PRIMARY_GRADES = ['Grade 4', 'Grade 5', 'Grade 6']

            # Group selected classes by (school_section, sub_section)
            section_groups = {}
            for class_name in selected_classes:
                if class_name in LOWER_PRIMARY_GRADES:
                    key = ('PRIMARY', 'LOWER')
                elif class_name in PRIMARY_GRADES:
                    key = ('PRIMARY', 'UPPER')
                else:
                    key = ('JSS', None)
                section_groups.setdefault(key, []).append(class_name)

            created_count = 0
            skipped_count = 0
            created_sections = []

            for (exam_db_section, exam_sub_section), classes in section_groups.items():
                _, created = Exam.all_objects.update_or_create(
                    school=school,
                    name=exam_name,
                    term=term,
                    year=year,
                    school_section=exam_db_section,
                    sub_section=exam_sub_section,
                    defaults={"status": status},
                )
                if created:
                    created_count += 1
                    label = exam_db_section
                    if exam_sub_section:
                        label += f' ({exam_sub_section})'
                    created_sections.append(label)
                else:
                    skipped_count += 1

            if created_count:
                msg = f"Assessment created for: {', '.join(created_sections)}."
                if skipped_count:
                    msg += f" {skipped_count} section(s) already existed."
                messages.success(request, msg)
            else:
                messages.info(request, "All selected assessments already exist.")

            return redirect(reverse('manage_exams') + '?tab=create')

        if action_type == "save_grading_config":
            grading_name = request.POST.get("grading_name", "").strip()
            lows = request.POST.getlist("low[]")
            highs = request.POST.getlist("high[]")
            grades_list = request.POST.getlist("grade[]")
            points_list = request.POST.getlist("points[]")

            if not grading_name:
                messages.error(request, "Please provide a grading system name.")
                return redirect(reverse('manage_exams') + '?tab=grading')

            _scale = []
            for i in range(len(grades_list)):
                grade = grades_list[i].strip() if i < len(grades_list) else ''
                pts = int(points_list[i]) if i < len(points_list) and points_list[i].strip().isdigit() else 0
                low_val = int(lows[i]) if i < len(lows) and lows[i].strip().lstrip('-').isdigit() else 0
                high_val = int(highs[i]) if i < len(highs) and highs[i].strip().lstrip('-').isdigit() else 100
                if grade:
                    _scale.append({
                        "level": grade,
                        "min_score": low_val,
                        "max_score": high_val,
                        "points": pts,
                    })

            if not _scale:
                messages.error(request, "Please add at least one grading level.")
                return redirect(reverse('manage_exams') + '?tab=grading')

            from ..models import GradingScale, GradingAssignment
            subject_id = request.POST.get("subject")
            raw_section = request.POST.get("school_section", "JSS")

            # Map form values to DB values
            if raw_section == 'PRIMARY_UPPER':
                school_section = 'PRIMARY'
                sub_section = 'UPPER'
            elif raw_section == 'PRIMARY_LOWER':
                school_section = 'PRIMARY'
                sub_section = 'LOWER'
            else:
                school_section = raw_section
                sub_section = request.POST.get("sub_section") or None

            subject_obj = None
            if subject_id:
                subject_obj = Subject.all_objects.filter(school=school, id=subject_id).first()

            existing_scale = GradingScale.objects.filter(school=school, name=grading_name).first()
            if existing_scale:
                existing_scale.subject_scale = _scale
                existing_scale.save()
                messages.success(request, "Grading system updated successfully.")
            else:
                existing_scale = GradingScale.objects.create(
                    school=school,
                    name=grading_name,
                    subject_scale=_scale,
                )
                messages.success(request, "Grading system created successfully.")

            GradingAssignment.objects.update_or_create(
                school=school,
                school_section=school_section,
                sub_section=sub_section,
                subject=subject_obj,
                defaults={'grading_scale': existing_scale},
            )
            from .grading_engine import clear_grading_cache
            clear_grading_cache()
            return redirect(reverse('manage_exams') + '?tab=grading')

        if action_type == "delete_grading_config":
            config_id = request.POST.get("config_id")
            if config_id:
                from ..models import GradingAssignment, GradingScale
                assignment = GradingAssignment.objects.filter(school=school, id=config_id).first()
                if assignment:
                    scale = assignment.grading_scale
                    assignment.delete()
                    if not scale.assignments.exists():
                        scale.delete()
                messages.success(request, "Grading system deleted.")
            return redirect(reverse('manage_exams') + '?tab=grading')

        if action_type == "toggle_status":
            exam_id = request.POST.get("exam_id")
            exam = get_school_object_or_403(Exam, request, id=exam_id)

            exam.status = "closed" if exam.status == "active" else "active"
            exam.save()

            messages.success(request, "Assessment status has been updated.")
            post_sub = request.POST.get("sub", "").strip().upper()
            redirect_url = reverse('manage_exams')
            params = []
            if post_sub:
                params.append(f"sub={post_sub}")
            post_year = request.POST.get('year') or request.GET.get('year')
            if post_year:
                params.append(f"year={post_year}")
            if params:
                redirect_url += '?' + '&'.join(params)
            return redirect(redirect_url)

        if action_type == "delete_exam":
            exam_id = request.POST.get("exam_id")
            exam = get_school_object_or_403(Exam, request, id=exam_id)
            exam.is_deleted = True
            exam.deleted_at = timezone.now()
            exam.deleted_by = request.user
            exam.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])

            messages.success(request, "Assessment has been deleted.")
            post_sub = request.POST.get("sub", "").strip().upper()
            redirect_url = reverse('manage_exams')
            params = []
            if post_sub:
                params.append(f"sub={post_sub}")
            post_year = request.POST.get('year') or request.GET.get('year')
            if post_year:
                params.append(f"year={post_year}")
            if params:
                redirect_url += '?' + '&'.join(params)
            return redirect(redirect_url)

        if action_type == "recover_exam":
            exam_id = request.POST.get("exam_id")
            exam = get_school_object_or_403(Exam, request, using="all_objects", id=exam_id, is_deleted=True)
            exam.is_deleted = False
            exam.deleted_at = None
            exam.deleted_by = None
            exam.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
            messages.success(request, "Exam has been recovered.")
            return redirect(reverse('manage_exams') + '?tab=deleted')

    # -----------------------------
    # Exam registry
    # -----------------------------
    section = get_request_school_section(request)

    # In PRIMARY workspace, support sub-section toggle via ?sub=LOWER|UPPER
    active_sub = request.GET.get('sub', '').strip().upper()
    if section == 'PRIMARY' and active_sub not in ('LOWER', 'UPPER'):
        active_sub = request.session.get('active_sub', 'LOWER')
    if section == 'PRIMARY' and active_sub not in ('LOWER', 'UPPER'):
        active_sub = 'LOWER'  # default to Lower Primary
    if section == 'PRIMARY':
        request.session['active_sub'] = active_sub
        request.session.modified = True

    # -----------------------------
    # Year filter
    # -----------------------------
    all_exams = Exam.all_objects.filter(school=school, is_deleted=False)
    if section == 'LOWER_PRIMARY':
        all_exams = all_exams.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        all_exams = all_exams.filter(school_section='PRIMARY', sub_section=active_sub)
    elif section == 'JSS':
        all_exams = all_exams.filter(school_section='JSS')

    available_years = list(
        all_exams
        .values_list('year', flat=True)
        .distinct()
        .order_by('-year')
    )

    selected_year = request.GET.get('year')
    if selected_year and selected_year.isdigit():
        selected_year = int(selected_year)
    else:
        selected_year = current_year

    # If selected year has no exams, fall back to latest year with exams
    if selected_year not in available_years and available_years:
        selected_year = available_years[0]

    exams = all_exams.filter(year=selected_year).order_by("-term", "name")

    selected_exam_id = request.GET.get("exam_id")
    selected_exam = None

    if selected_exam_id:
        selected_exam = Exam.all_objects.filter(school=school, id=selected_exam_id, is_deleted=False).first()
        if selected_exam:
            if section == 'LOWER_PRIMARY' and selected_exam.sub_section != 'LOWER':
                selected_exam = None
            elif section == 'PRIMARY' and selected_exam.sub_section != active_sub:
                selected_exam = None
            elif section == 'JSS' and selected_exam.school_section != 'JSS':
                selected_exam = None

    if not selected_exam:
        selected_exam = Exam.all_objects.filter(school=school, status="active", is_deleted=False)
        if section == 'LOWER_PRIMARY':
            selected_exam = selected_exam.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            selected_exam = selected_exam.filter(school_section='PRIMARY', sub_section=active_sub)
        elif section == 'JSS':
            selected_exam = selected_exam.filter(school_section='JSS')
        selected_exam = selected_exam.order_by("-year", "term", "name").first()

    if not selected_exam:
        selected_exam = exams.first()

    # -----------------------------
    # Submission monitor
    # -----------------------------
    grouped_streams = {}
    monitor_summary = {
        "total_sheets": 0,
        "submitted": 0,
        "returned": 0,
        "approved": 0,
        "published": 0,
        "in_progress": 0,
        "not_started": 0,
        "ready": 0,
    }

    if selected_exam:
        assignments = (
            SubjectAssignment.all_objects
            .filter(school=school)
            .select_related("subject", "teacher_profile", "teacher_profile__user")
            .order_by("class_name", "stream", "subject__code")
        )
        if section == 'LOWER_PRIMARY':
            assignments = assignments.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            assignments = assignments.filter(school_section='PRIMARY', sub_section=active_sub)
        elif section == 'JSS':
            assignments = assignments.filter(school_section='JSS')

        # ==================================================================
        # PRE-COMPUTE — Religion existence + student pool (1 query)
        # ==================================================================
        religion_exists = {}
        total_eligible = {}
        unique_streams = {(a.class_name, a.stream) for a in assignments}
        student_rels = Student.all_objects.filter(school=school).values_list(
            'class_name', 'stream', 'religion',
        )
        rel_set = set()
        pool = {}
        for cn, st, rel in student_rels:
            rel_set.add((cn, st, rel))
            pool.setdefault((cn, st), []).append(rel)

        for cn, st in unique_streams:
            for code in RELIGION_SUBJECTS:
                religion_exists[(cn, st, code)] = (cn, st, RELIGION_TAG.get(code, '')) in rel_set

        for a in assignments:
            key = (a.class_name, a.stream, a.subject.code)
            rels = pool.get((a.class_name, a.stream), [])
            if a.subject.code in RELIGION_SUBJECTS and religion_exists.get(key):
                tag = RELIGION_TAG.get(a.subject.code, '')
                total_eligible[key] = sum(1 for r in rels if r == tag)
            else:
                total_eligible[key] = len(rels)

        # ==================================================================
        # AGGREGATION — Marks counts in 1 query instead of N
        # ==================================================================
        marks_agg = {}
        agg_rows = (
            Mark.all_objects
            .filter(
                student__school=school,
                term=selected_exam.term,
                exam_type=selected_exam.name,
                year=selected_exam.year,
            )
            .values(
                'student__class_name', 'student__stream', 'subject_id',
                'school_section', 'sub_section',
            )
            .annotate(
                captured=Count('id'),
                absent=Count('id', filter=Q(is_absent=True)),
            )
        )
        for row in agg_rows:
            marks_agg[(
                row['student__class_name'], row['student__stream'],
                row['subject_id'], row['school_section'], row['sub_section'],
            )] = (row['captured'], row['absent'])

        # ==================================================================
        # SUBMISSIONS — Batch-fetch (1 query)
        # ==================================================================
        all_submissions = MarkSubmission.all_objects.filter(
            school=school,
            exam_name=selected_exam.name,
            term=selected_exam.term,
            year=selected_exam.year,
        )
        submission_map = {
            (sub.teacher_id, sub.subject_id, sub.class_name, sub.stream): sub
            for sub in all_submissions
        }

        # ==================================================================
        # BUILD ROWS — Pure Python, zero DB queries
        # ==================================================================
        for assignment in assignments:
            akey = (
                assignment.class_name, assignment.stream,
                assignment.subject_id, assignment.school_section,
                assignment.sub_section,
            )
            captured_count, absent_count = marks_agg.get(akey, (0, 0))
            total_students = total_eligible.get(
                (assignment.class_name, assignment.stream, assignment.subject.code), 0
            )
            missing_count = max(total_students - captured_count, 0)

            sub_key = (assignment.teacher_profile_id, assignment.subject_id, assignment.class_name, assignment.stream)
            submission = submission_map.get(sub_key)

            if submission:
                status_label = "Returned" if submission.status == "returned" else submission.get_status_display()
                status_key = submission.status

                if submission.status == "submitted":
                    monitor_summary["submitted"] += 1
                elif submission.status == "returned":
                    monitor_summary["returned"] += 1
                elif submission.status == "approved":
                    monitor_summary["approved"] += 1
                elif submission.status == "published":
                    monitor_summary["published"] += 1
            elif captured_count == 0:
                status_label = "Not Started"
                status_key = "not_started"
                monitor_summary["not_started"] += 1
            elif missing_count == 0:
                status_label = "Ready"
                status_key = "ready"
                monitor_summary["ready"] += 1
            else:
                status_label = "In Progress"
                status_key = "in_progress"
                monitor_summary["in_progress"] += 1

            monitor_summary["total_sheets"] += 1

            group_key = f"{assignment.class_name} {assignment.stream}"

            if group_key not in grouped_streams:
                grouped_streams[group_key] = {
                    "group_title": group_key,
                    "class_name": assignment.class_name,
                    "stream": assignment.stream,
                    "total_students": total_students,
                    "exam_id": selected_exam.id,
                    "captured_cells": 0,
                    "expected_cells": 0,
                    "rows": [],
                }

            grouped_streams[group_key]["captured_cells"] += captured_count
            grouped_streams[group_key]["expected_cells"] += total_students

            grouped_streams[group_key]["rows"].append({
                "assignment_id": assignment.id,
                "exam_id": selected_exam.id,
                "subject_code": assignment.subject,
                "subject_name": assignment.subject.name,
                "teacher_name": assignment.teacher_profile.get_full_title(),
                "captured_count": captured_count,
                "total_students": total_students,
                "absent_count": absent_count,
                "missing_count": missing_count,
                "status_label": status_label,
                "status_key": status_key,
                "submitted_at": submission.submitted_at if submission else None,
            })

    for group in grouped_streams.values():
        expected_cells = group.get("expected_cells") or 0
        captured_cells = group.get("captured_cells") or 0
        group["completion_rate"] = round((captured_cells / expected_cells) * 100) if expected_cells else 0
        rows = group.get("rows", [])
        group["subject_count"] = len(rows)
        group["submitted_or_better"] = sum(
            1 for row in rows if row["status_key"] in ["submitted", "approved", "published"]
        )
        group["approved_count"] = sum(1 for row in rows if row["status_key"] == "approved")
        group["published_count"] = sum(1 for row in rows if row["status_key"] == "published")
        group["missing_scores"] = sum(row["missing_count"] for row in rows)
        group["stream_status"] = (
            "Published" if group["published_count"] == group["subject_count"] and group["subject_count"]
            else "Approved" if group["approved_count"] == group["subject_count"] and group["subject_count"]
            else "Ready for Review" if group["submitted_or_better"] == group["subject_count"] and group["missing_scores"] == 0 and group["subject_count"]
            else "In Progress" if captured_cells
            else "Not Started"
        )

    # -----------------------------
    # Exam list grouped by term
    # -----------------------------
    exam_list_by_term = {}
    all_year_exams = list(Exam.all_objects.filter(school=school, year=selected_year, is_deleted=False).order_by("-term", "name"))

    def _normalize_exam_name(name):
        lower = name.strip().lower()
        if 'opener' in lower or 'opening' in lower:
            return 'Opener Assessment'
        if 'mid' in lower:
            return 'Mid Term Assessment'
        if 'end' in lower or 'final' in lower:
            return 'End Term Assessment'
        return name.strip()

    seen_exam_keys = set()
    unique_year_exams = []
    for ex in all_year_exams:
        norm_name = _normalize_exam_name(ex.name)
        exam_key = (norm_name, ex.term, ex.year)
        if exam_key not in seen_exam_keys:
            seen_exam_keys.add(exam_key)
            ex._normalized_name = norm_name
            unique_year_exams.append(ex)
    all_year_exams = unique_year_exams

    if all_year_exams:
        all_submissions = list(MarkSubmission.all_objects.filter(
            school=school,
            year=selected_year,
        ).select_related('teacher', 'teacher__user', 'subject').order_by('-submitted_at'))

        for sub in all_submissions:
            sub._normalized_exam_name = _normalize_exam_name(sub.exam_name)

        sub_index = {}
        for sub in all_submissions:
            key = (sub._normalized_exam_name, sub.term, sub.class_name)
            candidate_date = sub.submitted_at
            if sub.published_at and sub.published_at > candidate_date:
                candidate_date = sub.published_at
            if sub.reviewed_at and sub.reviewed_at > candidate_date:
                candidate_date = sub.reviewed_at
            if key not in sub_index or candidate_date > sub_index[key][1]:
                sub_index[key] = (sub, candidate_date)

        sub_counts = {}
        for sub in all_submissions:
            key = (sub._normalized_exam_name, sub.term, sub.class_name)
            if key not in sub_counts:
                sub_counts[key] = {
                    'total': 0, 'submitted': 0, 'returned': 0,
                    'approved': 0, 'published': 0,
                }
            sub_counts[key]['total'] += 1
            if sub.status in sub_counts[key]:
                sub_counts[key][sub.status] += 1

        total_subjects_by_class = {}
        for row in SubjectAssignment.all_objects.filter(school=school).values('class_name').annotate(cnt=Count('id')):
            total_subjects_by_class[row['class_name']] = row['cnt']

        exam_class_subject_counts = {}
        for sub in all_submissions:
            key = (sub._normalized_exam_name, sub.term, sub.class_name)
            exam_class_subject_counts[key] = exam_class_subject_counts.get(key, 0) + 1

        def _compute_class_status(exam_name_norm, term, class_name):
            counts = sub_counts.get((exam_name_norm, term, class_name), None)
            if not counts or counts['total'] == 0:
                return 'Not Started'
            total = counts['total']
            published = counts.get('published', 0)
            approved = counts.get('approved', 0)
            submitted = counts.get('submitted', 0)
            returned = counts.get('returned', 0)
            if published == total:
                return 'Published'
            if approved + published == total:
                return 'Approved'
            if returned > 0:
                return 'Returned'
            if submitted + approved + published == total:
                return 'Pending'
            return 'In Progress'

        for exam in all_year_exams:
            term_key = exam.term
            if term_key not in exam_list_by_term:
                exam_list_by_term[term_key] = {
                    'term': term_key,
                    'exams': [],
                }

            classes_seen = set()
            class_rows = []
            for sub in all_submissions:
                if sub._normalized_exam_name != exam._normalized_name or sub.term != exam.term:
                    continue
                class_key = sub.class_name
                if class_key in classes_seen:
                    continue
                classes_seen.add(class_key)

                last_sub_entry = sub_index.get((exam._normalized_name, exam.term, sub.class_name))
                updated_by = ''
                updated_on = exam.created_at
                if last_sub_entry:
                    last_sub, updated_on = last_sub_entry
                    if last_sub.teacher and last_sub.teacher.user:
                        updated_by = last_sub.teacher.user.get_full_name() or last_sub.teacher.user.username

                status_display = _compute_class_status(exam._normalized_name, exam.term, sub.class_name)

                class_rows.append({
                    'class_name': sub.class_name,
                    'status': status_display,
                    'updated_by': updated_by,
                    'updated_on': updated_on,
                    'exam_id': exam.id,
                })

            if not class_rows:
                status_display = 'Not Started'
                class_rows.append({
                    'class_name': '-',
                    'status': status_display,
                    'updated_by': '',
                    'updated_on': exam.created_at,
                    'exam_id': exam.id,
                })

            class_rows.sort(key=lambda r: -int(re.search(r'\d+', r['class_name']).group()) if re.search(r'\d+', r['class_name']) else 0)

            exam_list_by_term[term_key]['exams'].append({
                'id': exam.id,
                'name': exam._normalized_name,
                'status': exam.status,
                'classes': class_rows,
                'rowspan': len(class_rows),
            })

    term_order = {'Term 3': 1, 'Term 2': 2, 'Term 1': 3}
    exam_list_by_term = dict(
        sorted(
            exam_list_by_term.items(),
            key=lambda x: term_order.get(x[0], 99)
        )
    )

    context = {
        "exams": exams,
        "selected_exam": selected_exam,
        "grouped_streams": grouped_streams.values(),
        "monitor_summary": monitor_summary,
        "current_year": current_year,
        "selected_year": selected_year,
        "available_years": available_years,
        "exam_list_by_term": exam_list_by_term,
        "terms": TERM_CHOICES,
        "status_choices": status_choices,
        "section": section,
        "active_sub": active_sub,
        "active_tab": active_tab,
        "year_range": range(current_year - 2, current_year + 3),
        "available_grades": Grade.all_objects.filter(school=school).order_by('order'),
    }

    # ── Grading configs for the Grading Systems tab ──
    if active_tab == 'grading':
        import json
        from ..models import GradingAssignment, GradingScale
        assignments = GradingAssignment.objects.filter(
            school=school,
        ).select_related('grading_scale', 'subject').order_by('grading_scale__name')
        grading_configs = []
        for assign in assignments:
            scale = assign.grading_scale
            subject_name = assign.subject.name if assign.subject else None
            section_label = assign.school_section
            if assign.school_section == 'PRIMARY' and assign.sub_section:
                section_label = f"Primary ({assign.sub_section.title()})"
            if subject_name:
                display = f"{scale.name} — {subject_name}"
            else:
                display = f"{scale.name} — General ({section_label})"
            scale_data = scale.subject_scale or []
            grading_configs.append({
                'id': assign.id,
                'display_name': display,
                'school_section': assign.school_section,
                'sub_section': assign.sub_section,
                'subject_name': subject_name,
                'subject_scale': scale_data,
                'subject_scale_reversed': list(reversed(scale_data)),
            })
        context['grading_configs'] = grading_configs
        context['grading_configs_json'] = json.dumps(grading_configs)

        # Distinct subjects grouped by code for the dropdown
        all_subjects = Subject.all_objects.filter(
            school=school, is_active=True,
        ).order_by('school_section', 'code', 'name').distinct('code', 'school_section')
        context['available_subjects'] = all_subjects

        # JSON for dynamic filtering by section in JS
        subjects_json = []
        seen_codes = set()
        for subj in Subject.all_objects.filter(school=school, is_active=True).order_by('school_section', 'code', 'name'):
            key = (subj.code, subj.school_section)
            if key in seen_codes:
                continue
            seen_codes.add(key)
            subjects_json.append({
                'id': subj.id,
                'code': subj.code,
                'name': subj.name,
                'section': subj.school_section,
                'sub_section': subj.sub_section or '',
            })
        context['subjects_json'] = json.dumps(subjects_json)

    # ── Deleted Exams tab context ──
    if active_tab == 'deleted':
        deleted_year = request.GET.get('year', current_year)
        deleted_exams = Exam.all_objects.filter(
            school=school, is_deleted=True, year=deleted_year,
        ).order_by('-term', 'name').select_related('deleted_by')

        deleted_exam_rows = []
        for exam in deleted_exams:
            deleted_exam_rows.append({
                'id': exam.id,
                'name': exam.name,
                'year': exam.year,
                'term': exam.term,
                'school_section': exam.school_section,
                'sub_section': exam.sub_section or '',
                'deleted_by': exam.deleted_by.get_full_name() if exam.deleted_by else 'System',
                'deleted_at': exam.deleted_at.strftime('%d/%m/%Y') if exam.deleted_at else '-',
            })

        context['deleted_exam_rows'] = deleted_exam_rows
        context['deleted_years'] = list(range(current_year - 5, current_year + 1))
        context['deleted_selected_year'] = int(deleted_year)

    # Add sub-section counts when in PRIMARY workspace
    if section == 'PRIMARY':
        lower_count = Exam.all_objects.filter(school=school, school_section='PRIMARY', sub_section='LOWER', is_deleted=False).count()
        upper_count = Exam.all_objects.filter(school=school, school_section='PRIMARY', sub_section='UPPER', is_deleted=False).count()
        context["lower_exam_count"] = lower_count
        context["upper_exam_count"] = upper_count

    return render(request, "students/manage_exams.html", context)


@login_required(login_url='login')
@school_admin_required
def edit_exam(request):
    """
    Edit exam name page. Shows a form to rename the exam and handles the update.
    """
    if not user_has_main_school_admin_override(request.user):
        messages.error(request, "You are not allowed to edit exams.")
        return redirect('select_exam')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    exam_id = request.GET.get('exam_id') or request.POST.get('exam_id')
    if not exam_id:
        messages.error(request, "No exam specified.")
        return redirect('manage_exams')

    # Admin can access any exam in their school — no section filtering
    exam = Exam.all_objects.filter(school=school, id=exam_id, is_deleted=False).first()
    if not exam:
        messages.error(request, "Exam not found.")
        return redirect('manage_exams')

    # Get classes taking this exam (from submissions)
    classes_taking = (
        MarkSubmission.all_objects
        .filter(
            school=school,
            exam_name=exam.name,
            term=exam.term,
            year=exam.year,
        )
        .values_list('class_name', flat=True)
        .distinct()
        .order_by('class_name')
    )
    classes_list = ', '.join(classes_taking) if classes_taking else 'No classes assigned'

    if request.method == 'POST':
        new_name = request.POST.get('exam_name', '').strip()
        if not new_name:
            messages.error(request, "Exam name cannot be empty.")
            return redirect(f'{reverse("edit_exam")}?exam_id={exam_id}')

        # Check if new name would violate unique_together constraint
        existing = Exam.all_objects.filter(
            school=school,
            name=new_name,
            term=exam.term,
            year=exam.year,
            school_section=exam.school_section,
            sub_section=exam.sub_section,
            is_deleted=False,
        ).exclude(id=exam.id).exists()

        if existing:
            messages.error(request, "An exam with this name already exists for this term and year.")
            return redirect(f'{reverse("edit_exam")}?exam_id={exam_id}')

        old_name = exam.name
        exam.name = new_name
        exam.save()

        # Update all submissions with the old exam name to use the new name
        MarkSubmission.all_objects.filter(
            school=school,
            exam_name=old_name,
            term=exam.term,
            year=exam.year,
        ).update(exam_name=new_name)

        # Update all marks with the old exam name
        Mark.all_objects.filter(
            student__school=school,
            exam_type=old_name,
            term=exam.term,
            year=exam.year,
        ).update(exam_type=new_name)

        # Update all exam summaries with the old exam name
        from ..models import ExamSummary
        ExamSummary.all_objects.filter(
            school=school,
            exam_name=old_name,
            term=exam.term,
            year=exam.year,
        ).update(exam_name=new_name)

        messages.success(request, "✓ Exam name updated")
        return redirect(f'{reverse("edit_exam")}?exam_id={exam_id}')

    context = {
        'exam': exam,
        'classes_list': classes_list,
    }
    return render(request, 'students/edit_exam.html', context)


@login_required(login_url='login')
@school_admin_required
def analyse_exam(request):
    """
    Exam analysis page showing subject performance, stream comparison,
    grade breakdown, and performance over time.
    """
    if not user_has_main_school_admin_override(request.user):
        messages.error(request, "You are not allowed to analyse exams.")
        return redirect('select_exam')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    exam_id = request.GET.get('exam_id')
    class_name_filter = request.GET.get('class_name', '').strip()
    if not exam_id:
        latest_exam = Exam.all_objects.filter(school=school, is_deleted=False).order_by('-year', 'term', 'name').first()
        if latest_exam:
            exam_id = latest_exam.id
        else:
            messages.error(request, "No exams found.")
            return redirect('manage_exams')

    exam = Exam.all_objects.filter(school=school, id=exam_id, is_deleted=False).first()
    if not exam:
        messages.error(request, "Exam not found.")
        return redirect('manage_exams')

    from ..models import ExamSummary, Subject, GradingConfig
    from .constants import ORDERED_LEVELS

    section = exam.school_section

    PRIMARY_ORDERED_LEVELS = ['EE', 'ME', 'AE', 'BE']
    breakdown_levels = PRIMARY_ORDERED_LEVELS if section == 'PRIMARY' else ORDERED_LEVELS
    sub_section = exam.sub_section

    # If no class_name passed, try to auto-detect from the exam's summaries
    if not class_name_filter:
        auto_grades = (
            ExamSummary.all_objects.filter(
                school=school, exam_name=exam.name, term=exam.term, year=exam.year,
            )
            .values_list('student__class_name', flat=True)
            .distinct()
        )
        auto_grades_list = list(auto_grades)
        if len(auto_grades_list) >= 1:
            class_name_filter = auto_grades_list[0]

    all_students = Student.all_objects.filter(
        school=school,
        is_active=True, status='Active',
    )
    if class_name_filter:
        all_students = all_students.filter(class_name=class_name_filter)

    summaries = ExamSummary.all_objects.filter(
        school=school, exam_name=exam.name, term=exam.term, year=exam.year,
    )
    if class_name_filter:
        summaries = summaries.filter(student__class_name=class_name_filter)

    if class_name_filter:
        detected_section = summaries.values_list('school_section', flat=True).distinct()
        detected_sub = summaries.values_list('sub_section', flat=True).distinct()
        if detected_section:
            section = detected_section[0]
        if detected_sub:
            sub_section = [s for s in detected_sub if s]
            sub_section = sub_section[0] if sub_section else sub_section

    breakdown_levels = PRIMARY_ORDERED_LEVELS if section == 'PRIMARY' else ORDERED_LEVELS
    from .grading_engine import resolve_scale_fast, get_grading_scale
    grade_descriptors = resolve_scale_fast(
        school.pk, section, sub_section,
        subject_id=None, is_total_calculation=False,
    )
    grading_scale_obj = get_grading_scale(school.pk, section, sub_section, subject_id=None)

    total_students = all_students.count()
    students_who_sat = summaries.values('student_id').distinct().count()
    student_ids = summaries.values_list('student_id', flat=True).distinct()

    streams = list(
        summaries.values_list('student__stream', flat=True).distinct().order_by('student__stream')
    )
    grade_name = summaries.values_list('student__class_name', flat=True).first() or exam.name

    subjects = Subject.all_objects.filter(school=school)

    all_marks = Mark.all_objects.filter(
        student__school=school,
        student_id__in=student_ids,
        term=exam.term,
        year=exam.year,
        exam_type=exam.name,
    ).select_related('subject', 'student')

    subject_perf = {}
    for mark in all_marks:
        if mark.subject_id and mark.subject:
            subj_name = mark.subject.name
            if subj_name not in subject_perf:
                subject_perf[subj_name] = {'total_points': 0, 'count': 0, 'changes': []}
            subject_perf[subj_name]['total_points'] += mark.points
            subject_perf[subj_name]['count'] += 1

    PLV_LABELS = {
        'EE1': 'Exceeding Expectations', 'EE2': 'Exceeding Expectations',
        'ME1': 'Meeting Expectations', 'ME2': 'Meeting Expectations',
        'AE1': 'Approaching Expectations', 'AE2': 'Approaching Expectations',
        'BE1': 'Below Expectations', 'BE2': 'Below Expectations',
        'EE': 'Exceeding Expectations', 'ME': 'Meeting Expectations',
        'AE': 'Approaching Expectations', 'BE': 'Below Expectations',
    }

    subject_rows = []
    for subj_name, data in sorted(subject_perf.items()):
        if data['count'] > 0:
            mean_pts = data['total_points'] / data['count']
            plv = '-'
            if grade_descriptors:
                for level_def in grade_descriptors:
                    if level_def.get('points') == round(mean_pts):
                        plv = level_def.get('level', '-')
                        break
            subject_rows.append({
                'name': subj_name,
                'points': round(mean_pts, 4),
                'change': 0.0,
                'performance_level': plv,
                'performance_level_label': PLV_LABELS.get(plv, plv),
            })

    subject_rows.sort(key=lambda x: x['points'], reverse=True)

    stream_stats = {}
    for s in streams:
        s_ids = summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
        s_marks = all_marks.filter(student_id__in=s_ids)
        total_pts = sum(m.points for m in s_marks)
        count = s_marks.count() if s_marks.count() > 0 else 1
        mean_pts = total_pts / count if count else 0
        total_marks_sum = sum(m.score for m in s_marks)
        mean_marks = total_marks_sum / count if count else 0
        stream_stats[s] = {
            'mean_points': round(mean_pts, 4),
            'mean_marks': round(mean_marks, 1),
            'entries': len(s_ids),
        }

    all_stream_pts = [v['mean_points'] for v in stream_stats.values()]
    overall_mean_points = round(sum(all_stream_pts) / len(all_stream_pts), 4) if all_stream_pts else 0
    all_stream_marks = [v['mean_marks'] for v in stream_stats.values()]
    overall_mean_marks = round(sum(all_stream_marks) / len(all_stream_marks), 1) if all_stream_marks else 0

    other_exams = Exam.all_objects.filter(
        school=school, year=exam.year, is_deleted=False,
    ).exclude(id=exam.id).order_by('term', 'name')

    # Compute comparison exam stats for change calculation
    prev_exam = other_exams.first() if other_exams else None
    prev_mean_marks = 0
    prev_mean_points = 0
    if prev_exam:
        prev_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=prev_exam.name, term=prev_exam.term, year=prev_exam.year,
        )
        if class_name_filter:
            prev_summaries = prev_summaries.filter(student__class_name=class_name_filter)

        prev_student_ids = prev_summaries.values_list('student_id', flat=True).distinct()
        prev_all_marks = Mark.all_objects.filter(
            student__school=school,
            student_id__in=prev_student_ids,
            term=prev_exam.term,
            year=prev_exam.year,
            exam_type=prev_exam.name,
        )
        prev_stream_pts = []
        prev_stream_marks = []
        for s in streams:
            s_ids = prev_summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
            s_marks = prev_all_marks.filter(student_id__in=s_ids)
            count = s_marks.count() if s_marks.count() > 0 else 1
            prev_stream_pts.append(sum(m.points for m in s_marks) / count)
            prev_stream_marks.append(sum(m.score for m in s_marks) / count)
        if prev_stream_marks:
            prev_mean_marks = round(sum(prev_stream_marks) / len(prev_stream_marks), 1)
        if prev_stream_pts:
            prev_mean_points = round(sum(prev_stream_pts) / len(prev_stream_pts), 4)

    mm_change = round(overall_mean_marks - prev_mean_marks, 2) if prev_mean_marks else 0
    mp_change = round(overall_mean_points - prev_mean_points, 4) if prev_mean_points else 0

    # Compute subject-level change against previous exam
    prev_subject_perf = {}
    if prev_exam:
        for mark in prev_all_marks:
            if mark.subject_id and mark.subject:
                subj_name = mark.subject.name
                if subj_name not in prev_subject_perf:
                    prev_subject_perf[subj_name] = {'total_points': 0, 'count': 0}
                prev_subject_perf[subj_name]['total_points'] += mark.points
                prev_subject_perf[subj_name]['count'] += 1

    for row in subject_rows:
        prev_data = prev_subject_perf.get(row['name'])
        if prev_data and prev_data['count'] > 0:
            prev_mean = prev_data['total_points'] / prev_data['count']
            row['change'] = round(row['points'] - prev_mean, 4)
        else:
            row['change'] = 0.0

    overall_plv = '-'
    if grading_scale_obj and grading_scale_obj.total_scale:
        total_marks_800 = overall_mean_marks * 8
        for level_def in grading_scale_obj.total_scale:
            if level_def.get('min_marks', 0) <= total_marks_800 <= level_def.get('max_marks', 0):
                overall_plv = level_def.get('level', '-')
                break
    grade_breakdown = []
    for s in streams:
        s_ids = summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
        s_summaries = summaries.filter(student__stream=s)
        row = {'form': f"{grade_name} {s}", 'X': 0, 'Y': 0, 'entries': len(s_ids)}
        for lvl in breakdown_levels:
            row[lvl] = 0
        row.update({
            'mean_marks': stream_stats.get(s, {}).get('mean_marks', 0),
            'mm_dev': 0,
            'mean_points': stream_stats.get(s, {}).get('mean_points', 0),
            'mp_dev': 0,
            'performance_level': '-',
        })
        for summ in s_summaries:
            raw_plv = summ.overall_plv
            if raw_plv in row:
                row[raw_plv] += 1

        row['mean_marks'] = stream_stats.get(s, {}).get('mean_marks', 0)
        row['mm_dev'] = round(row['mean_marks'] - overall_mean_marks, 4)
        row['mean_points'] = stream_stats.get(s, {}).get('mean_points', 0)
        row['mp_dev'] = round(row['mean_points'] - overall_mean_points, 4)

        if grading_scale_obj and grading_scale_obj.total_scale:
            total_m = row['mean_marks'] * 8
            for level_def in grading_scale_obj.total_scale:
                if level_def.get('min_marks', 0) <= total_m <= level_def.get('max_marks', 0):
                    row['performance_level'] = level_def.get('level', '-')
                    break
        else:
            best_lvl = '-'
            best_count = 0
            for lvl in breakdown_levels:
                if row.get(lvl, 0) > best_count:
                    best_count = row[lvl]
                    best_lvl = lvl
            if best_count > 0:
                row['performance_level'] = best_lvl

        grade_breakdown.append(row)

    if overall_plv == '-':
        all_plvs = [r.get('performance_level', '-') for r in grade_breakdown if r.get('performance_level', '-') != '-']
        if all_plvs:
            from collections import Counter
            overall_plv = Counter(all_plvs).most_common(1)[0][0]

    total_row = {
        'form': grade_name,
        'X': 0, 'Y': 0,
        'entries': sum(r['entries'] for r in grade_breakdown),
        'mean_marks': overall_mean_marks,
        'mm_dev': 0,
        'mean_points': overall_mean_points,
        'mp_dev': 0,
        'performance_level': overall_plv,
    }
    for lvl in breakdown_levels:
        total_row[lvl] = sum(r.get(lvl, 0) for r in grade_breakdown)

    subject_breakdowns = {}
    for subj_name, data in sorted(subject_perf.items()):
        if data['count'] == 0:
            continue
        subj_rows = []
        for s in streams:
            s_ids_list = summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
            subj_marks = all_marks.filter(student_id__in=s_ids_list, subject__name=subj_name)
            row = {'form': f"{grade_name} {s}", 'X': 0, 'Y': 0, 'entries': len(s_ids_list)}
            for lvl in breakdown_levels:
                row[lvl] = 0
            total_pts = 0
            count = 0
            for m in subj_marks:
                total_pts += m.points
                count += 1
                if section == 'PRIMARY':
                    plv_key = m.primary_descriptor
                else:
                    plv_key = '-'
                    if grade_descriptors:
                        for level_def in grade_descriptors:
                            converted = (m.score / m.maximum_marks * 100) if m.maximum_marks else 0
                            if level_def.get('min_marks', 0) <= converted <= level_def.get('max_marks', 0):
                                plv_key = level_def.get('level', '-')
                                break
                if plv_key in row:
                    row[plv_key] += 1
            mean_pts = total_pts / count if count else 0
            row['mean_marks'] = round(sum(m.score for m in subj_marks) / count, 1) if count else 0
            row['mm_dev'] = round(row['mean_marks'] - overall_mean_marks, 4)
            row['mean_points'] = round(mean_pts, 4)
            row['mp_dev'] = round(mean_pts - overall_mean_points, 4)
            row['performance_level'] = '-'
            if grade_descriptors:
                for level_def in grade_descriptors:
                    if level_def.get('points') == round(mean_pts):
                        row['performance_level'] = level_def.get('level', '-')
                        break
            row['entries'] = count
            subj_rows.append(row)
        subj_total = {
            'form': grade_name, 'X': 0, 'Y': 0,
            'entries': sum(r['entries'] for r in subj_rows),
            'mean_marks': round(sum(r['mean_marks'] for r in subj_rows) / len(subj_rows), 1) if subj_rows else 0,
            'mm_dev': 0,
            'mean_points': round(sum(r['mean_points'] for r in subj_rows) / len(subj_rows), 4) if subj_rows else 0,
            'mp_dev': 0,
            'performance_level': '-',
        }
        for lvl in breakdown_levels:
            subj_total[lvl] = sum(r.get(lvl, 0) for r in subj_rows)
        subject_breakdowns[subj_name] = {'rows': subj_rows, 'total': subj_total}

    subject_names = sorted(subject_perf.keys())

    available_grades = list(
        Student.all_objects.filter(school=school, is_active=True, status='Active')
        .values_list('class_name', flat=True).distinct().order_by('class_name')
    )

    all_exams_for_school = Exam.all_objects.filter(
        school=school, year=exam.year, is_deleted=False,
    ).order_by('-year', 'term', 'name')

    # Group exams by grade + term for the custom dropdown
    exam_groups_dict = {}
    seen_exam_keys = set()
    for ex in all_exams_for_school:
        key = (ex.name, ex.term, ex.year)
        if key in seen_exam_keys:
            continue
        grade_label = ex.school_section or 'JSS'
        first_summary = ExamSummary.all_objects.filter(
            school=school, exam_name=ex.name, term=ex.term, year=ex.year,
        ).select_related('student').first()
        if first_summary and first_summary.student:
            grade_label = first_summary.student.class_name
        term_label = f"{grade_label} - {ex.term} ({ex.year})"
        if term_label not in exam_groups_dict:
            exam_groups_dict[term_label] = []
        exam_groups_dict[term_label].append({
            'id': ex.id,
            'name': ex.name,
        })
        seen_exam_keys.add(key)

    # Build ordered list of groups
    exam_groups = []
    for group_label, exams_list in exam_groups_dict.items():
        exam_groups.append({
            'label': group_label,
            'exams': exams_list,
        })

    # Performance over time: collect all exams for same grade across years
    pot_exams = Exam.all_objects.filter(
        school=school, is_deleted=False,
    ).order_by('year', 'term', 'name')

    # Deduplicate P-O-T by exam name to avoid same-named exams in different sections
    seen_pot_names = set()
    pot_unique_exams = []
    for pe in pot_exams:
        key = (pe.name, pe.term, pe.year)
        if key in seen_pot_names:
            continue
        # Only include exams that have summaries for the current class
        pe_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=pe.name, term=pe.term, year=pe.year,
        )
        if class_name_filter:
            pe_summaries = pe_summaries.filter(student__class_name=class_name_filter)
        if pe_summaries.exists():
            seen_pot_names.add(key)
            pot_unique_exams.append(pe)

    # Build time-series data: labels = exam names, per-stream mean points
    pot_labels = []
    pot_streams_data = {s: [] for s in streams}
    for pot_exam in pot_unique_exams:
        pot_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=pot_exam.name, term=pot_exam.term, year=pot_exam.year,
        )
        if class_name_filter:
            pot_summaries = pot_summaries.filter(student__class_name=class_name_filter)

        if not pot_summaries.exists():
            continue

        # Get class name for label
        pot_labels.append(f"{grade_name} {pot_exam.term}, {pot_exam.name}, {pot_exam.year}")

        pot_all_marks = Mark.all_objects.filter(
            student__school=school,
            student_id__in=pot_summaries.values_list('student_id', flat=True).distinct(),
            term=pot_exam.term,
            year=pot_exam.year,
            exam_type=pot_exam.name,
        )

        for s in streams:
            s_ids = pot_summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
            s_marks = pot_all_marks.filter(student_id__in=s_ids)
            total_pts = sum(m.points for m in s_marks)
            count = s_marks.count() if s_marks.count() > 0 else 1
            mean_pts = round(total_pts / count, 4) if count else 0
            pot_streams_data[s].append(mean_pts)

    import json
    context = {
        'exam': exam,
        'grade_name': grade_name,
        'streams': streams,
        'students_who_sat': students_who_sat,
        'total_students': total_students,
        'subject_rows': subject_rows,
        'stream_stats': stream_stats,
        'overall_mean_points': overall_mean_points,
        'overall_mean_marks': overall_mean_marks,
        'mm_change': mm_change,
        'mp_change': mp_change,
        'overall_plv': overall_plv,
        'overall_plv_label': PLV_LABELS.get(overall_plv, overall_plv),
        'grade_breakdown': grade_breakdown,
        'total_row': total_row,
        'ordered_levels': breakdown_levels,
        'other_exams': other_exams,
        'available_grades': available_grades,
        'exam_groups': exam_groups,
        'current_exam_id': exam.id,
        'pot_labels_json': json.dumps(pot_labels),
        'pot_streams_data_json': json.dumps(pot_streams_data),
        'plv_labels': PLV_LABELS,
        'grade_breakdown_json': json.dumps(grade_breakdown),
        'total_row_json': json.dumps(total_row),
        'plv_labels_json': json.dumps(PLV_LABELS),
        'streams_json': json.dumps(streams),
        'stream_stats_json': json.dumps(stream_stats),
        'subject_breakdowns_json': json.dumps(subject_breakdowns),
        'subject_names': subject_names,
    }
    return render(request, 'students/analyse_exam.html', context)


@login_required(login_url='login')
@school_admin_required
def review_stream_submission(request):
    """
    Admin review screen for a full class stream. Admin decisions are applied to
    every submitted subject sheet in that stream for the selected assessment.
    """
    if not user_has_main_school_admin_override(request.user):
        messages.error(request, "You are not allowed to review submissions.")
        return redirect("select_exam")

    exam_id = request.GET.get("exam_id") or request.POST.get("exam_id")
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect("welcome_page")

    section = get_request_school_section(request)

    # Determine the exam section (Exams don't have LOWER_PRIMARY, they use PRIMARY)
    exam_section_filter = 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS'

    exam = Exam.all_objects.filter(school=school, id=exam_id, is_deleted=False).first() if exam_id else None
    if exam and exam.school_section != exam_section_filter:
        exam = None
    if not exam:
        exam = Exam.all_objects.filter(school=school, status="active", school_section=exam_section_filter, is_deleted=False).order_by("-year", "term", "name").first()
    if not exam:
        exam = Exam.all_objects.filter(school=school, school_section=exam_section_filter, is_deleted=False).order_by("-year", "term", "name").first()
    if not exam:
        messages.error(request, "Create an assessment first before reviewing stream submissions.")
        return redirect("manage_exams")

    class_name = request.GET.get("class_name") or request.POST.get("class_name")
    stream = request.GET.get("stream") or request.POST.get("stream")

    if not class_name or not stream:
        exams = Exam.all_objects.filter(school=school, is_deleted=False).order_by("-year", "term", "name")
        stream_cards = []
        pairs = (
            SubjectAssignment.all_objects.filter(school=school)
            .values("class_name", "stream")
            .distinct()
            .order_by("class_name", "stream")
        )
        section = get_request_school_section(request)

        active_sub = request.GET.get('sub', '').strip().upper()
        if section == 'LOWER_PRIMARY':
            active_sub = 'LOWER'
        elif section == 'PRIMARY' and active_sub not in ('LOWER', 'UPPER'):
            active_sub = request.session.get('active_sub', 'UPPER')
        if section == 'PRIMARY' and active_sub not in ('LOWER', 'UPPER'):
            active_sub = 'UPPER'
        if section == 'PRIMARY':
            request.session['active_sub'] = active_sub
            request.session.modified = True

        if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
            exam_db_section = 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS'
            exams = exams.filter(school_section=exam_db_section)
            pairs = pairs.filter(school_section=exam_db_section)
            if section == 'LOWER_PRIMARY':
                pairs = pairs.filter(sub_section='LOWER')
            elif section == 'PRIMARY':
                pairs = pairs.filter(sub_section=active_sub)
        for pair in pairs:
            _, totals = get_stream_submission_summary(pair["class_name"], pair["stream"], exam)
            stream_cards.append({
                "class_name": pair["class_name"],
                "stream": pair["stream"],
                "totals": totals,
            })

    context = {
        "exam": exam,
        "exams": exams,
        "stream_cards": stream_cards,
        "section": section,
        "active_sub": active_sub,
    }
    if section == 'PRIMARY':
        lower_count = Exam.all_objects.filter(school=school, school_section='PRIMARY', sub_section='LOWER', is_deleted=False).count()
        upper_count = Exam.all_objects.filter(school=school, school_section='PRIMARY', sub_section='UPPER', is_deleted=False).count()
        context["lower_exam_count"] = lower_count
        context["upper_exam_count"] = upper_count
    return render(request, "students/stream_review_list.html", context)

    section = get_request_school_section(request)
    valid_pairs = set(
        SubjectAssignment.all_objects.filter(school=school)
        .values_list("class_name", "stream")
        .distinct()
    )
    if (class_name, stream) not in valid_pairs:
        messages.error(request, "Select a valid class stream.")
        return redirect("manage_exams")

    rows, totals = get_stream_submission_summary(class_name, stream, exam)

    if request.method == "POST":
        action_type = request.POST.get("action_type")
        admin_note = bleach.clean(request.POST.get("admin_note", "").strip())
        submissions = [
            row["submission"] for row in rows
            if row["submission"] and row["submission"].status in ["submitted", "approved", "published"]
        ]

        if not submissions:
            messages.error(request, "No submitted sheets are available for this stream yet.")
            return redirect(f"{request.path}?exam_id={exam.id}&class_name={class_name}&stream={stream}")

        if action_type == "return_subject":
            assignment_id = request.POST.get("assignment_id")
            target_row = next(
                (row for row in rows if str(row["assignment"].id) == str(assignment_id)),
                None,
            )
            target_submission = target_row["submission"] if target_row else None
            if not target_submission:
                messages.error(request, "That subject sheet has not been submitted yet.")
                return redirect(f"{request.path}?exam_id={exam.id}&class_name={class_name}&stream={stream}")

            target_submission.status = "returned"
            target_submission.admin_note = admin_note
            target_submission.reviewed_at = timezone.now()
            target_submission.save()

            # Unlock the assessment so the teacher can edit the returned sheet
            AssessmentLock.objects.filter(
                school=school,
                year=exam.year,
                term=exam.term,
                grade=class_name,
                exam_type=exam.name,
            ).update(is_locked=False)

            messages.success(
                request,
                f"{target_row['subject_name']} has been returned to {target_row['teacher_name']} without affecting the other subjects."
            )

        elif action_type == "return_stream":
            for submission in submissions:
                submission.status = "returned"
                submission.admin_note = admin_note
                submission.reviewed_at = timezone.now()
                submission.save()

            # Unlock the assessment so teachers can edit returned sheets
            AssessmentLock.objects.filter(
                school=school,
                year=exam.year,
                term=exam.term,
                grade=class_name,
                exam_type=exam.name,
            ).update(is_locked=False)

            messages.success(request, f"{class_name} {stream} has been returned to teachers for correction.")

        elif action_type == "approve_stream":
            if not totals["can_approve"]:
                messages.error(request, "This stream cannot be approved until every subject is submitted and every learner has a score or AB.")
                return redirect(f"{request.path}?exam_id={exam.id}&class_name={class_name}&stream={stream}")
            for row in rows:
                submission = row["submission"]
                if not submission:
                    continue
                submission.status = "approved"
                submission.admin_note = admin_note
                submission.reviewed_at = timezone.now()
                submission.save()
            messages.success(request, f"{class_name} {stream} has been approved as a complete stream.")

        elif action_type == "publish_stream":
            if not totals["can_publish"]:
                messages.error(request, "Approve all subject sheets in this stream before publishing.")
                return redirect(f"{request.path}?exam_id={exam.id}&class_name={class_name}&stream={stream}")
            for row in rows:
                submission = row["submission"]
                if not submission:
                    continue
                submission.status = "published"
                submission.admin_note = admin_note
                submission.published_at = timezone.now()
                if not submission.reviewed_at:
                    submission.reviewed_at = timezone.now()
                submission.save()
            messages.success(request, f"{class_name} {stream} results have been published.")

        return redirect(f"{request.path}?exam_id={exam.id}&class_name={class_name}&stream={stream}")

    return render(request, "students/review_stream_submission.html", {
        "exam": exam,
        "class_name": class_name,
        "stream": stream,
        "rows": rows,
        "totals": totals,
    })


@login_required(login_url='login')
def review_submission(request):
    """
    Admin review screen for one assessment sheet.
    Admin can return, approve, or publish a teacher submission.
    """
    if not user_has_main_school_admin_override(request.user):
        messages.error(request, "You are not allowed to review submissions.")
        return redirect("select_exam")

    assignment_id = request.GET.get("assignment_id") or request.POST.get("assignment_id")
    exam_id = request.GET.get("exam_id") or request.POST.get("exam_id")

    assignment = get_school_object_or_403(
        SubjectAssignment,
        request,
        id=assignment_id,
    )

    exam = get_school_object_or_403(Exam, request, id=exam_id)

    submission = MarkSubmission.objects.filter(
        school=get_request_school(request),
        teacher=assignment.teacher_profile,
        subject=assignment.subject,
        class_name=assignment.class_name,
        stream=assignment.stream,
        exam_name=exam.name,
        term=exam.term,
        year=exam.year,
        school_section=assignment.school_section,
    ).first()

    if request.method == "POST":
        action_type = request.POST.get("action_type")
        admin_note = bleach.clean(request.POST.get("admin_note", "").strip())

        if not submission:
            messages.error(request, "This marksheet has not been submitted yet.")
            return redirect(
                f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
            )

        if action_type == "save_admin_scores":
            if not user_has_main_school_admin_override(request.user):
                messages.error(request, "Only the Main School Admin can override published result scores.")
                return redirect(
                    f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                )

            try:
                maximum_marks = int(request.POST.get("maximum_marks") or 100)
            except ValueError:
                maximum_marks = 100

            if maximum_marks <= 0:
                messages.error(request, "Total marks must be greater than zero.")
                return redirect(
                    f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                )

            corrected_count = 0
            students_for_sheet = get_subject_students(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
            )

            school = get_request_school(request)

            # ── BULK READ: fetch existing marks in ONE query (fixes N+1) ──
            existing_marks = Mark.all_objects.filter(
                student__in=students_for_sheet,
                subject=assignment.subject,
                term=exam.term,
                exam_type=exam.name,
                year=exam.year,
                school_section=assignment.school_section,
                sub_section=assignment.sub_section,
            )
            existing_map = {m.student_id: m for m in existing_marks}

            # ── PHASE 1: Validate all inputs + build operation lists ────────
            marks_to_delete_ids = []
            marks_to_create = []
            religion_student_updates = []
            religion_opposite_deletes = []

            for student in students_for_sheet:
                value = request.POST.get(f"score_{student.id}", "").strip()
                if not value:
                    continue

                _adm_lookup = dict(
                    school=school,
                    student=student,
                    subject=assignment.subject,
                    term=exam.term,
                    exam_type=exam.name,
                    year=exam.year,
                    school_section=assignment.school_section,
                    sub_section=assignment.sub_section,
                )

                if value.upper() == "AB":
                    marks_to_create.append(Mark(
                        **_adm_lookup,
                        raw_score=None,
                        maximum_marks=maximum_marks,
                        score=0,
                        is_absent=True,
                    ))
                    if student.id in existing_map:
                        marks_to_delete_ids.append(existing_map[student.id].id)
                    corrected_count += 1
                    if assignment.subject.code in RELIGION_SUBJECTS:
                        religion_tag = RELIGION_TAG.get(assignment.subject.code, "")
                        religion_student_updates.append((student.id, religion_tag))
                        opposite = _resolve_opposite_religion_subject(school, assignment)
                        if opposite:
                            religion_opposite_deletes.append((student.id, opposite))
                    continue

                try:
                    raw_score = int(value)
                except ValueError:
                    messages.error(request, f"Invalid score for {student.name}. Use a number or AB.")
                    return redirect(
                        f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                    )

                if raw_score < 0 or raw_score > maximum_marks:
                    messages.error(request, f"{student.name}'s score exceeds the total marks.")
                    return redirect(
                        f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                    )

                marks_to_create.append(Mark(
                    **_adm_lookup,
                    raw_score=raw_score,
                    maximum_marks=maximum_marks,
                    score=round((raw_score / maximum_marks) * 100),
                    is_absent=False,
                ))
                if student.id in existing_map:
                    marks_to_delete_ids.append(existing_map[student.id].id)
                corrected_count += 1
                if assignment.subject.code in RELIGION_SUBJECTS:
                    religion_tag = RELIGION_TAG.get(assignment.subject.code, "")
                    religion_student_updates.append((student.id, religion_tag))
                    opposite = _resolve_opposite_religion_subject(school, assignment)
                    if opposite:
                        religion_opposite_deletes.append((student.id, opposite))

            # ── PHASE 2: Atomic bulk write — all deletes + creates in one transaction ──
            with transaction.atomic():
                if marks_to_delete_ids:
                    Mark.all_objects.filter(id__in=marks_to_delete_ids).delete()

                if marks_to_create:
                    Mark.all_objects.bulk_create(marks_to_create, batch_size=250)

                if religion_student_updates:
                    student_ids = [sid for sid, _ in religion_student_updates]
                    _, religion_tag = religion_student_updates[0]
                    Student.all_objects.filter(id__in=student_ids).update(religion=religion_tag)

                if religion_opposite_deletes:
                    from django.db.models import Q
                    q_objects = Q()
                    for student_id, opposite_subject in religion_opposite_deletes:
                        q_objects |= Q(student_id=student_id, subject=opposite_subject)
                    Mark.all_objects.filter(
                        q_objects,
                        school=school,
                        term=exam.term,
                        exam_type=exam.name,
                        year=exam.year,
                        school_section=assignment.school_section,
                        sub_section=assignment.sub_section,
                    ).delete()

            submission.admin_note = admin_note
            if submission.status != "published":
                submission.status = "submitted"
                submission.published_at = None
            submission.reviewed_at = timezone.now()
            submission.save()

            # ── Rebuild ExamSummary snapshots for this grade ───────────
            from students.tasks import populate_exam_summaries
            populate_exam_summaries.delay(
                school_id=school.pk,
                grade=assignment.class_name,
                year=exam.year,
                term=exam.term,
                exam_name=exam.name,
                school_section=assignment.school_section,
                sub_section=assignment.sub_section,
            )

            messages.success(request, f"{corrected_count} learner score correction(s) saved.")

        elif action_type == "return_submission":
            submission.status = "returned"
            submission.admin_note = admin_note
            submission.reviewed_at = timezone.now()
            submission.save()

            # Unlock the assessment so the teacher can edit the returned sheet
            AssessmentLock.objects.filter(
                school=school,
                year=exam.year,
                term=exam.term,
                grade=assignment.class_name,
                exam_type=exam.name,
            ).update(is_locked=False)

            messages.success(request, "Assessment sheet has been returned to the teacher for correction.")

        elif action_type == "approve_submission":
            total_students = get_religion_aware_student_count(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
            )

            captured_count = get_subject_marks(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
                exam.term,
                exam.name,
                exam.year,
            ).count()

            missing_count = max(total_students - captured_count, 0)

            if missing_count > 0:
                messages.error(
                    request,
                    f"This sheet cannot be approved because {missing_count} learner(s) still have no score or AB."
                )
                return redirect(
                    f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                )

            submission.status = "approved"
            submission.admin_note = admin_note
            submission.reviewed_at = timezone.now()
            submission.save()

            messages.success(request, "Assessment sheet has been approved.")

        elif action_type == "publish_submission":
            total_students = get_religion_aware_student_count(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
            )

            captured_count = get_subject_marks(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
                exam.term,
                exam.name,
                exam.year,
            ).count()

            missing_count = max(total_students - captured_count, 0)

            if missing_count > 0:
                messages.error(
                    request,
                    f"This sheet cannot be published because {missing_count} learner(s) still have no score or AB."
                )
                return redirect(
                    f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
                )

            submission.status = "published"
            submission.admin_note = admin_note
            submission.published_at = timezone.now()

            if not submission.reviewed_at:
                submission.reviewed_at = timezone.now()

            submission.save()

            # ── Rebuild ExamSummary snapshots for this grade ───────────
            from students.tasks import populate_exam_summaries
            populate_exam_summaries.delay(
                school_id=school.pk,
                grade=assignment.class_name,
                year=exam.year,
                term=exam.term,
                exam_name=exam.name,
                school_section=assignment.school_section,
                sub_section=assignment.sub_section,
            )

            messages.success(request, "Assessment sheet has been published.")
        return redirect(
            f"{request.path}?assignment_id={assignment.id}&exam_id={exam.id}"
        )

    students = get_subject_students(
        assignment.class_name,
        assignment.stream,
        assignment.subject,
    )

    marks_qs = get_subject_marks(
        assignment.class_name,
        assignment.stream,
        assignment.subject,
        exam.term,
        exam.name,
        exam.year,
    ).select_related("student")

    marks_by_student = {
        mark.student_id: mark
        for mark in marks_qs
    }

    learner_rows = []

    for student in students:
        mark = marks_by_student.get(student.id)

        if mark:
            if mark.is_absent:
                score_display = "AB"
                editable_score = "AB"
                percentage_display = "AB"
                performance_level = "Absent"
                points_display = "0"
                row_status = "Absent"
            else:
                score_display = mark.raw_score if mark.raw_score is not None else mark.score
                editable_score = mark.raw_score if mark.raw_score is not None else mark.score
                percentage_display = f"{mark.score}%"
                performance_level, points_display = get_performance_level(mark.score)
                row_status = "Captured"
        else:
            score_display = "-"
            editable_score = ""
            percentage_display = "-"
            performance_level = "Missing"
            points_display = "-"
            row_status = "Missing"

        learner_rows.append({
            "student_id": student.id,
            "admission_no": student.admission_no,
            "name": student.name,
            "score_display": score_display,
            "editable_score": editable_score,
            "percentage_display": percentage_display,
            "performance_level": performance_level,
            "points_display": points_display,
            "row_status": row_status,
        })

    total_students = students.count()
    captured_count = marks_qs.count()
    absent_count = marks_qs.filter(is_absent=True).count()
    missing_count = max(total_students - captured_count, 0)

    mean_score = marks_qs.filter(is_absent=False).aggregate(
        average_score=Avg("score")
    )["average_score"]

    mean_score = round(mean_score, 1) if mean_score is not None else 0

    if submission:
        submission_status = submission.get_status_display()
    elif captured_count == 0:
        submission_status = "Not Started"
    elif missing_count == 0:
        submission_status = "Ready for Submission"
    else:
        submission_status = "In Progress"

    context = {
        "assignment": assignment,
        "exam": exam,
        "teacher": assignment.teacher_profile,
        "subject_name": assignment.subject.name,
        "learner_rows": learner_rows,
        "total_students": total_students,
        "captured_count": captured_count,
        "absent_count": absent_count,
        "missing_count": missing_count,
        "mean_score": mean_score,
        "submission": submission,
        "submission_status": submission_status,
        "current_maximum_marks": marks_qs.first().maximum_marks if marks_qs.first() else 100,
        "admin_override_enabled": user_has_main_school_admin_override(request.user),
    }

    return render(request, "students/review_submission.html", context)

@login_required(login_url='login')
@school_admin_required
def manage_assessment_locks(request):
    """
    Allows the school ICT admin to lock/unlock assessment data-entry screens.
    Supports instant AJAX toggle updates and auto-detects the current Kenyan term.
    """
    current_year  = datetime.date.today().year
    current_month = datetime.date.today().month

    # Derive active term from the Kenyan school calendar
    if 1 <= current_month <= 4:
        calculated_term = 'Term 1'
    elif 5 <= current_month <= 8:
        calculated_term = 'Term 2'
    else:
        calculated_term = 'Term 3'

    current_term = request.GET.get('term', calculated_term).strip().replace('+', ' ')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # --- AJAX toggle ---
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            data         = json.loads(request.body)
            payload_term = data.get('term', current_term).strip().replace('+', ' ')

            # Validate grade belongs to the current section
            section = get_request_school_section(request)
            allowed_grades = LOWER_PRIMARY_GRADE_CHOICES if section == 'LOWER_PRIMARY' else PRIMARY_GRADE_CHOICES if section == 'PRIMARY' else JSS_GRADE_CHOICES
            if data.get('grade') not in allowed_grades:
                return JsonResponse({'status': 'error', 'message': 'Invalid grade for current section.'}, status=403)

            valid_exam_types = ['Opener Assessment', 'Mid Term Assessment', 'End Term Assessment']
            if data.get('exam_type') not in valid_exam_types:
                return JsonResponse({'status': 'error', 'message': 'Invalid assessment type.'}, status=400)

            with transaction.atomic():
                lock_school_section = 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS'
                lock_sub_section = 'LOWER' if section == 'LOWER_PRIMARY' else ('UPPER' if section == 'PRIMARY' else None)
                lock_obj, _ = AssessmentLock.objects.update_or_create(
                    school=school,
                    year=current_year, term=payload_term,
                    grade=data.get('grade'), exam_type=data.get('exam_type'),
                    defaults={
                        'is_locked': data.get('is_locked'),
                        'school_section': lock_school_section,
                        'sub_section': lock_sub_section,
                    }
                )
            return JsonResponse({'status': 'success', 'is_locked': lock_obj.is_locked})
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception("Assessment lock toggle failed")
            return JsonResponse({'status': 'error', 'message': 'An error occurred. Please try again.'}, status=400)

    # --- Build lock state grid ---
    section = get_request_school_section(request)
    grade_choices = LOWER_PRIMARY_GRADE_CHOICES if section == 'LOWER_PRIMARY' else PRIMARY_GRADE_CHOICES if section == 'PRIMARY' else JSS_GRADE_CHOICES
    
    lock_map = {
        (l.grade, l.exam_type): l.is_locked
        for l in AssessmentLock.objects.filter(school=school, year=current_year, term=current_term)
    }
    assessments_list = ['Opener Assessment', 'Mid Term Assessment', 'End Term Assessment']
    portal_data = [
        {
            'grade_name': grade,
            'assessments': [
                {'name': exam, 'is_locked': lock_map.get((grade, exam), False)}
                for exam in assessments_list
            ]
        }
        for grade in grade_choices
    ]

    return render(request, 'students/assessment_locks.html', {
        'current_year': current_year,
        'current_term': current_term,
        'portal_data':  portal_data,
    })


# ==============================================================================
# PRIMARY SECTION — Mark Entry (mirrors JSS select_exam flow)
# ==============================================================================

PRIMARY_GRADE_CHOICES = ['Grade 4', 'Grade 5', 'Grade 6']
LOWER_PRIMARY_GRADE_CHOICES = ['Grade 1', 'Grade 2', 'Grade 3']


def _get_primary_performance(percentage, school=None, section=None, sub_section=None):
    """Return (descriptor, points) for a primary percentage score.
    Uses the unified grading engine for instant cached lookups."""
    import logging
    from ..school_scope import get_current_school, get_current_school_section

    if not school:
        school = get_current_school()
    if not section:
        section = get_current_school_section()

    if school and section:
        from .grading_engine import resolve_scale_fast
        scale_data = resolve_scale_fast(school.pk, section, sub_section, subject_id=None)
        if scale_data:
            return get_subject_level_fast(percentage, scale_data)

    logging.getLogger("students.exams").error(
        "GradingScale.subject_scale missing for school_id=%s section=%s. "
        "Primary descriptor cannot be resolved. "
        "Configure it at /school-admin/grading-config/.",
        getattr(school, 'id', None), section,
    )
    return 'NO CONFIG', 0


@login_required(login_url='login')
@tenant_read_only_required
@rate_limit("mark_entry", max_requests=30, window_seconds=60, methods=["POST"])
def select_exam_primary(request):
    """
    Primary teacher mark entry screen.
    Identical flow to JSS select_exam — teacher enters a score or AB per learner.
    Performance levels use Primary CBC scale (EE/ME/AE/BE) instead of JSS KJSEA.
    """
    if not user_can_mutate_marks(request.user):
        raise PermissionDenied("Only teachers and school admins may enter marks.")

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        messages.error(request, "No teacher profile is linked to this account.")
        return redirect('home_alt')

    assignment_id = request.GET.get('assignment_id') or request.POST.get('assignment_id')
    exam_id = request.GET.get('exam_id') or request.POST.get('exam_id')

    school = get_request_school(request)
    section = get_request_school_section(request)

    # Determine which section and sub_section to use for this view
    if section == 'LOWER_PRIMARY':
        exam_section = 'PRIMARY'
        exam_sub_section = 'LOWER'
    elif section == 'PRIMARY':
        exam_section = 'PRIMARY'
        exam_sub_section = 'UPPER'
    else:
        exam_section = 'JSS'
        exam_sub_section = None

    assignments = (
        SubjectAssignment.all_objects
        .filter(school=school, teacher_profile=teacher, school_section=exam_section)
        .select_related('teacher_profile__user')
        .order_by('class_name', 'stream', 'subject__code')
    )
    if exam_sub_section:
        assignments = assignments.filter(sub_section=exam_sub_section)

    active_exams = Exam.all_objects.filter(
        school=school, status='active', school_section=exam_section, is_deleted=False
    ).order_by('-year', 'term', 'name')
    if exam_sub_section:
        active_exams = active_exams.filter(sub_section=exam_sub_section)

    selected_assignment = None
    selected_exam = None
    students = None
    submission = None
    is_locked = False
    is_submitted = False
    current_maximum_marks = 100

    if assignment_id and exam_id:
        selected_assignment = get_school_object_or_403(
            SubjectAssignment,
            request,
            id=assignment_id,
            teacher_profile=teacher,
        )

        selected_exam = get_school_object_or_403(
            Exam,
            request,
            id=exam_id,
            status='active',
        )

        is_locked = AssessmentLock.objects.filter(
            school=school,
            year=selected_exam.year,
            term=selected_exam.term,
            grade=selected_assignment.class_name,
            exam_type=selected_exam.name,
            is_locked=True,
        ).exists()

        submission = MarkSubmission.objects.filter(
            school=school,
            teacher=teacher,
            subject=selected_assignment.subject,
            class_name=selected_assignment.class_name,
            stream=selected_assignment.stream,
            exam_name=selected_exam.name,
            term=selected_exam.term,
            year=selected_exam.year,
            school_section=selected_assignment.school_section,
        ).first()

        is_submitted = submission is not None and submission.status in [
            "submitted",
            "approved",
            "published",
        ]

        existing_mark_for_max = Mark.objects.filter(
            school=school,
            subject=selected_assignment.subject,
            term=selected_exam.term,
            exam_type=selected_exam.name,
            year=selected_exam.year,
            student__class_name=selected_assignment.class_name,
            student__stream=selected_assignment.stream,
        ).first()

        if existing_mark_for_max:
            current_maximum_marks = existing_mark_for_max.maximum_marks or 100

        students = get_subject_students(
            selected_assignment.class_name,
            selected_assignment.stream,
            selected_assignment.subject,
        )

        # ── BULK READ: fetch all existing marks in ONE query (fixes N+1) ──
        existing_marks = Mark.all_objects.filter(
            student__in=students,
            subject=selected_assignment.subject,
            term=selected_exam.term,
            exam_type=selected_exam.name,
            year=selected_exam.year,
        )
        existing_map = {m.student_id: m for m in existing_marks}

        for student in students:
            existing = existing_map.get(student.id)

            if existing:
                if existing.is_absent:
                    student.current_score = "AB"
                    student.current_points = 0
                    student.current_percentage = "AB"
                elif existing.primary_raw_score:
                    student.current_score = existing.primary_raw_score
                    student.current_points = existing.primary_performance_point
                    student.current_percentage = existing.primary_descriptor
                elif existing.raw_score is not None:
                    student.current_score = existing.raw_score
                    student.current_points = existing.points
                    student.current_percentage = existing.score
                else:
                    student.current_score = existing.score
                    student.current_points = existing.points
                    student.current_percentage = existing.score
            else:
                student.current_score = ""
                student.current_points = ""
                student.current_percentage = ""

        if request.method == 'POST':
            if is_locked:
                messages.error(request, "This assessment sheet is locked by admin.")
                return _htmx_redirect(request,
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            if is_submitted:
                messages.error(request, "This sheet has already been submitted and cannot be edited. Ask the admin to return it first.")
                return _htmx_redirect(request,
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            try:
                maximum_marks = int(request.POST.get('maximum_marks', current_maximum_marks))
            except (ValueError, TypeError):
                maximum_marks = current_maximum_marks

            missing_students = []
            saved_count = 0
            deleted_count = 0

            # ── PHASE 1: Validate all inputs + build operation lists ────────
            marks_to_delete_ids = []
            marks_to_create = []
            religion_student_updates = []
            religion_opposite_deletes = []

            for student in students:
                value = request.POST.get(f'score_{student.id}', '').strip()

                if not value:
                    missing_students.append(student.name)
                    if student.id in existing_map:
                        marks_to_delete_ids.append(existing_map[student.id].id)
                    continue

                _mark_lookup = dict(
                    school=school,
                    student=student,
                    subject=selected_assignment.subject,
                    term=selected_exam.term,
                    exam_type=selected_exam.name,
                    year=selected_exam.year,
                    school_section=exam_section,
                    sub_section=exam_sub_section,
                )

                if value.upper() == "AB":
                    marks_to_create.append(Mark(
                        **_mark_lookup,
                        raw_score=None,
                        maximum_marks=maximum_marks,
                        score=0,
                        is_absent=True,
                        primary_raw_score='AB',
                        primary_performance_point='AB',
                        primary_descriptor='AB',
                    ))
                    if student.id in existing_map:
                        marks_to_delete_ids.append(existing_map[student.id].id)
                    saved_count += 1

                    # Collect religion auto-tag work for batch execution
                    if selected_assignment.subject.code in RELIGION_SUBJECTS:
                        religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                        religion_student_updates.append((student.id, religion_tag))
                        opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                        if opposite:
                            religion_opposite_deletes.append((student.id, opposite))
                    continue

                # ── Numeric score path ──
                try:
                    raw_score = int(value)
                except ValueError:
                    messages.error(request, f"Invalid score for {student.name}. Use a number or AB.")
                    return _htmx_redirect(request,
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                if raw_score < 0 or raw_score > maximum_marks:
                    messages.error(request, f"{student.name}'s score exceeds the total marks.")
                    return _htmx_redirect(request,
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                percentage = round((raw_score / maximum_marks) * 100)
                descriptor, points = _get_primary_performance(percentage, school=school, section=section, sub_section=exam_sub_section)

                marks_to_create.append(Mark(
                    **_mark_lookup,
                    raw_score=raw_score,
                    maximum_marks=maximum_marks,
                    score=percentage,
                    is_absent=False,
                    primary_raw_score=str(raw_score),
                    primary_performance_point=str(points),
                    primary_descriptor=descriptor,
                ))
                if student.id in existing_map:
                    marks_to_delete_ids.append(existing_map[student.id].id)
                saved_count += 1

                # Collect religion auto-tag work for batch execution
                if selected_assignment.subject.code in RELIGION_SUBJECTS:
                    religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                    religion_student_updates.append((student.id, religion_tag))
                    opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                    if opposite:
                        religion_opposite_deletes.append((student.id, opposite))

            # ── PHASE 2: Check validation errors before any writes ──────────
            if missing_students and selected_assignment.subject.code not in RELIGION_SUBJECTS:
                messages.error(request, "Please enter a score or AB for every learner before submitting.")
                return _htmx_redirect(request,
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            # ── PHASE 3: Atomic bulk write — all deletes + creates in one transaction ──
            with transaction.atomic():
                # Bulk delete old marks
                if marks_to_delete_ids:
                    Mark.all_objects.filter(id__in=marks_to_delete_ids).delete()
                    deleted_count = len(marks_to_delete_ids)

                # Bulk create new marks
                if marks_to_create:
                    Mark.all_objects.bulk_create(marks_to_create, batch_size=250)

                # Batch religion student updates
                if religion_student_updates:
                    student_ids = [sid for sid, _ in religion_student_updates]
                    # Use the tag from the last update (all should be the same for one subject)
                    _, religion_tag = religion_student_updates[0]
                    Student.all_objects.filter(id__in=student_ids).update(religion=religion_tag)

                # Batch delete opposite religion marks
                if religion_opposite_deletes:
                    from django.db.models import Q
                    q_objects = Q()
                    for student_id, opposite_subject in religion_opposite_deletes:
                        q_objects |= Q(student_id=student_id, subject=opposite_subject)
                    Mark.all_objects.filter(
                        q_objects,
                        school=school,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                        school_section=exam_section,
                        sub_section=exam_sub_section,
                    ).delete()

            MarkSubmission.objects.update_or_create(
                school=school,
                teacher=teacher,
                subject=selected_assignment.subject,
                class_name=selected_assignment.class_name,
                stream=selected_assignment.stream,
                exam_name=selected_exam.name,
                term=selected_exam.term,
                year=selected_exam.year,
                school_section=exam_section,
                sub_section=exam_sub_section,
                defaults={
                    "status": "submitted",
                    "admin_note": "",
                    "reviewed_at": None,
                    "published_at": None,
                }
            )

            messages.success(request, f"{saved_count} learner records submitted successfully." + (f" {deleted_count} mark(s) cleared." if deleted_count else ""))
            invalidate_report_caches(
                school.pk, selected_assignment.class_name, selected_assignment.stream,
                selected_exam.year, selected_exam.term, selected_exam.name,
            )
            return _htmx_redirect(request, 'select_exam_primary')

    exam_rows = []

    for exam in active_exams:
        for assignment in assignments:
            total_students = get_religion_aware_student_count(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
            )

            uploaded_marks = get_subject_marks(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
                exam.term,
                exam.name,
                exam.year,
            ).count()

            missing_count = max(total_students - uploaded_marks, 0)

            row_submission = MarkSubmission.objects.filter(
                teacher=teacher,
                subject=assignment.subject,
                class_name=assignment.class_name,
                stream=assignment.stream,
                exam_name=exam.name,
                term=exam.term,
                year=exam.year,
                school_section=assignment.school_section,
            ).first()

            status_label = "Not Started"
            status_key = "not_started"

            if row_submission and row_submission.status == "returned":
                status_label = "Returned"
                status_key = "returned"
            elif row_submission and row_submission.status == "approved":
                status_label = "Approved"
                status_key = "approved"
            elif row_submission and row_submission.status == "published":
                status_label = "Published"
                status_key = "published"
            elif row_submission and row_submission.status == "submitted":
                status_label = "Submitted"
                status_key = "submitted"
            elif uploaded_marks == 0:
                status_label = "Not Started"
                status_key = "not_started"
            elif missing_count == 0:
                status_label = "Ready"
                status_key = "ready"
            else:
                status_label = "In Progress"
                status_key = "in_progress"

            exam_rows.append({
                "exam": exam,
                "assignment": assignment,
                "status": status_label,
                "status_label": status_label,
                "status_key": status_key,
                "submission": row_submission,
            })

    exam_rows.sort(key=lambda r: (r['assignment'].class_name, r['assignment'].stream, r['exam'].name))

    is_htmx = request.headers.get('HX-Request') == 'true'
    template_name = 'students/select_exam_details_partial.html' if is_htmx else 'students/select_exam_details.html'
    
    return render(request, template_name, {
        'teacher': teacher,
        'exam_rows': exam_rows,
        'selected_assignment': selected_assignment,
        'selected_exam': selected_exam,
        'students': students,
        'is_locked': is_locked,
        'is_submitted': is_submitted,
        'submission': submission,
        'current_maximum_marks': current_maximum_marks,
        'grading_mode': 'primary',
        'grading_scale_json': _get_grading_scale_json(),
        'back_url': 'select_exam_primary',
    })


@login_required(login_url='login')
@tenant_read_only_required
def clear_mark(request):
    """
    AJAX endpoint to delete a single student's mark before final submission.
    POST: student_id, assignment_id, exam_id
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not user_can_mutate_marks(request.user):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        return JsonResponse({'error': 'No teacher profile'}, status=403)

    student_id = request.POST.get('student_id')
    assignment_id = request.POST.get('assignment_id')
    exam_id = request.POST.get('exam_id')

    if not all([student_id, assignment_id, exam_id]):
        return JsonResponse({'error': 'Missing parameters'}, status=400)

    school = get_request_school(request)

    try:
        assignment = SubjectAssignment.objects.get(id=assignment_id, school=school, teacher_profile=teacher)
    except SubjectAssignment.DoesNotExist:
        return JsonResponse({'error': 'Assignment not found'}, status=404)

    try:
        exam = Exam.objects.get(id=exam_id, school=school, status='active')
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found'}, status=404)

    try:
        student = Student.objects.get(id=student_id, school=school)
    except Student.DoesNotExist:
        return JsonResponse({'error': 'Student not found'}, status=404)

    submission = MarkSubmission.objects.filter(
        school=school,
        teacher=teacher,
        subject=assignment.subject,
        class_name=assignment.class_name,
        stream=assignment.stream,
        exam_name=exam.name,
        term=exam.term,
        year=exam.year,
        school_section=assignment.school_section,
    ).first()

    if submission and submission.status in ('submitted', 'approved', 'published'):
        return JsonResponse({'error': 'This sheet has been submitted and cannot be modified. Ask the admin to return it first.'}, status=403)

    deleted, _ = Mark.all_objects.filter(
        school=school,
        student=student,
        subject=assignment.subject,
        term=exam.term,
        exam_type=exam.name,
        year=exam.year,
        school_section=assignment.school_section,
        sub_section=assignment.sub_section,
    ).delete()

    if submission and submission.status == 'submitted' and deleted:
        remaining = Mark.all_objects.filter(
            school=school,
            subject=assignment.subject,
            term=exam.term,
            exam_type=exam.name,
            year=exam.year,
            school_section=assignment.school_section,
            sub_section=assignment.sub_section,
        ).count()
        if remaining == 0:
            submission.delete()

    return JsonResponse({'ok': True, 'deleted': deleted})


@login_required(login_url='login')
@tenant_read_only_required
def save_mark(request):
    """
    AJAX endpoint to auto-save a single mark without submitting.
    POST: student_id, assignment_id, exam_id, score (number, 'AB', or empty)

    Uses PostgreSQL INSERT ... ON CONFLICT DO UPDATE for atomic upsert.
    Single query, zero dead tuples, zero index bloat, zero race conditions.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not user_can_mutate_marks(request.user):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    # ── RATE LIMIT: 5 saves per second sliding window ──────────────────────
    user_id = request.user.id
    cache_key = f"rate_limit_save_mark_{user_id}"
    request_history = cache.get(cache_key, [])
    now = time.time()
    # Filter out requests older than 1 second
    request_history = [t for t in request_history if now - t < 1.0]
    if len(request_history) >= 5:
        return JsonResponse({'error': 'Too many requests. Slow down.'}, status=429)
    request_history.append(now)
    cache.set(cache_key, request_history, timeout=2)

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        return JsonResponse({'error': 'No teacher profile'}, status=403)

    student_id = request.POST.get('student_id')
    assignment_id = request.POST.get('assignment_id')
    exam_id = request.POST.get('exam_id')
    score_value = request.POST.get('score', '').strip()

    if not all([student_id, assignment_id, exam_id]):
        return JsonResponse({'error': 'Missing parameters'}, status=400)

    school = get_request_school(request)

    try:
        assignment = SubjectAssignment.objects.get(id=assignment_id, school=school, teacher_profile=teacher)
    except SubjectAssignment.DoesNotExist:
        return JsonResponse({'error': 'Assignment not found'}, status=404)

    try:
        exam = Exam.objects.get(id=exam_id, school=school, status='active')
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found'}, status=404)

    try:
        student = Student.objects.get(id=student_id, school=school)
    except Student.DoesNotExist:
        return JsonResponse({'error': 'Student not found'}, status=404)

    submission = MarkSubmission.objects.filter(
        school=school, teacher=teacher, subject=assignment.subject,
        class_name=assignment.class_name, stream=assignment.stream,
        exam_name=exam.name, term=exam.term, year=exam.year,
        school_section=assignment.school_section,
    ).first()

    if submission and submission.status in ('submitted', 'approved', 'published'):
        return JsonResponse({'error': 'This sheet has been submitted and cannot be modified. Ask the admin to return it first.'}, status=403)

    existing_mark = Mark.objects.filter(
        school=school, student=student, subject=assignment.subject,
        term=exam.term, exam_type=exam.name, year=exam.year,
    ).first()
    try:
        maximum_marks = int(request.POST.get('maximum_marks', '100'))
    except (ValueError, TypeError):
        maximum_marks = existing_mark.maximum_marks if existing_mark else 100

    # ── CLEAR: empty score → delete mark ──────────────────────────────────
    if not score_value:
        Mark.all_objects.filter(
            school=school, student=student, subject=assignment.subject,
            term=exam.term, exam_type=exam.name, year=exam.year,
            school_section=assignment.school_section,
            sub_section=assignment.sub_section,
        ).delete()
        return JsonResponse({'ok': True, 'cleared': True})

    # ── Shared lookup for religion handling ────────────────────────────────
    _mark_filter = dict(
        school_id=school.pk, student_id=student.pk,
        subject_id=assignment.subject.pk if assignment.subject else None,
        term=exam.term, exam_type=exam.name, year=exam.year,
        school_section=assignment.school_section,
        sub_section=assignment.sub_section,
    )

    def _handle_religion():
        """Update student religion tag and delete opposite-religion mark if applicable."""
        if assignment.subject.code in RELIGION_SUBJECTS:
            religion_tag = RELIGION_TAG.get(assignment.subject.code, '')
            Student.objects.filter(id=student.id).update(religion=religion_tag)
            opposite = _resolve_opposite_religion_subject(school, assignment)
            if opposite:
                Mark.all_objects.filter(
                    school=school, student=student, subject=opposite,
                    term=exam.term, exam_type=exam.name, year=exam.year,
                    school_section=assignment.school_section,
                    sub_section=assignment.sub_section,
                ).delete()

    # ── ABSENT: score = 'AB' → upsert absent mark ────────────────────────
    if score_value.upper() == 'AB':
        _handle_religion()

        mark_id = upsert_mark(
            school_id=school.pk,
            student_id=student.pk,
            subject_id=assignment.subject.pk if assignment.subject else None,
            school_section=assignment.school_section,
            sub_section=assignment.sub_section,
            score=0,
            raw_score=None,
            maximum_marks=maximum_marks,
            is_absent=True,
            primary_raw_score='AB',
            primary_performance_point='AB',
            primary_descriptor='AB',
            performance_level='AB',
            points=0,
            term=exam.term,
            year=exam.year,
            exam_type=exam.name,
        )
        return JsonResponse({'ok': True, 'absent': True, 'mark_id': mark_id})

    # ── NUMERIC SCORE → upsert with computed grading ──────────────────────
    try:
        raw_score = int(score_value)
    except ValueError:
        return JsonResponse({'error': 'Invalid score'}, status=400)

    if raw_score < 0 or raw_score > maximum_marks:
        return JsonResponse({'error': 'Score exceeds total marks'}, status=400)

    percentage = round((raw_score / maximum_marks) * 100)

    if assignment.school_section == 'PRIMARY':
        pp_level, pp_points = _get_primary_performance(
            percentage, school=school,
            section=assignment.school_section,
            sub_section=assignment.sub_section,
        )
        perf_level = pp_level
        perf_points = pp_points if pp_points else 0
    else:
        from .helpers import get_performance_level
        perf_level, perf_points = get_performance_level(
            percentage, sub_section=assignment.sub_section,
        )
        pp_level = ''
        pp_points = ''

    mark_id = upsert_mark(
        school_id=school.pk,
        student_id=student.pk,
        subject_id=assignment.subject.pk if assignment.subject else None,
        school_section=assignment.school_section,
        sub_section=assignment.sub_section,
        score=percentage,
        raw_score=raw_score,
        maximum_marks=maximum_marks,
        is_absent=False,
        primary_raw_score=str(raw_score),
        primary_performance_point=str(pp_points) if pp_points else '',
        primary_descriptor=pp_level,
        performance_level=perf_level,
        points=perf_points,
        term=exam.term,
        year=exam.year,
        exam_type=exam.name,
    )

    _handle_religion()

    invalidate_report_caches(
        school.pk, assignment.class_name, assignment.stream,
        exam.year, exam.term, exam.name,
    )
    return JsonResponse({'ok': True, 'saved': True, 'mark_id': mark_id})


@login_required(login_url='login')
@tenant_read_only_required
def update_maximum_marks(request):
    """
    AJAX endpoint: recalculate ALL existing marks for a subject/exam when
    the teacher changes the maximum_marks field.
    POST: assignment_id, exam_id, new_maximum_marks
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not user_can_mutate_marks(request.user):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        return JsonResponse({'error': 'No teacher profile'}, status=403)

    assignment_id = request.POST.get('assignment_id')
    exam_id = request.POST.get('exam_id')
    new_maximum = request.POST.get('new_maximum_marks')

    if not all([assignment_id, exam_id, new_maximum]):
        return JsonResponse({'error': 'Missing parameters'}, status=400)

    try:
        new_maximum = int(new_maximum)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid maximum marks'}, status=400)

    if new_maximum < 1 or new_maximum > 500:
        return JsonResponse({'error': 'Maximum marks must be between 1 and 500'}, status=400)

    school = get_request_school(request)

    try:
        assignment = SubjectAssignment.objects.get(id=assignment_id, school=school, teacher_profile=teacher)
    except SubjectAssignment.DoesNotExist:
        return JsonResponse({'error': 'Assignment not found'}, status=404)

    try:
        exam = Exam.objects.get(id=exam_id, school=school, status='active')
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found'}, status=404)

    submission = MarkSubmission.objects.filter(
        school=school, teacher=teacher, subject=assignment.subject,
        class_name=assignment.class_name, stream=assignment.stream,
        exam_name=exam.name, term=exam.term, year=exam.year,
        school_section=assignment.school_section,
    ).first()

    if submission and submission.status in ('submitted', 'approved', 'published'):
        return JsonResponse({'error': 'Sheet is submitted — cannot change total marks now.'}, status=403)

    _mark_filter = dict(
        school=school,
        subject=assignment.subject,
        term=exam.term,
        exam_type=exam.name,
        year=exam.year,
        school_section=assignment.school_section,
    )
    if assignment.sub_section:
        _mark_filter['sub_section'] = assignment.sub_section

    marks = list(Mark.all_objects.filter(**_mark_filter).select_related('student', 'subject'))
    if not marks:
        return JsonResponse({'ok': True, 'updated': 0, 'new_maximum': new_maximum})

    # ── Pre-fetch: Student + Subject in_bulk (zero N+1) ──────────────────
    student_ids = {m.student_id for m in marks}
    subject_ids = {m.subject_id for m in marks if m.subject_id}
    students_map = Student.all_objects.filter(is_active=True).in_bulk(student_ids)
    subjects_map = Subject.all_objects.in_bulk(subject_ids)

    # ── Pre-fetch: GradingScale (single query) ──────────────────────────
    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)

    # ── Pre-fetch: HMAC key (single read) ────────────────────────────────
    from ..security.integrity import compute_mark_checksum, _integrity_key
    hmac_key = _integrity_key()

    # ── Compute all updates in Python, then single bulk_update ────────────
    marks_to_update = []
    with transaction.atomic():
        for mark in marks:
            subject_obj = subjects_map.get(mark.subject_id)
            student_obj = students_map.get(mark.student_id)

            if mark.is_absent:
                mark.maximum_marks = new_maximum
                mark.integrity_checksum = compute_mark_checksum(mark)
                marks_to_update.append(mark)
                continue

            raw = mark.raw_score if mark.raw_score is not None else mark.score
            if raw is not None:
                new_pct = round((raw / new_maximum) * 100)
                mark.maximum_marks = new_maximum
                mark.score = new_pct
                mark.raw_score = raw
                # Recompute grading — subject-specific scale
                mark_subject_id = mark.subject_id or (subjects_map.get(mark.subject_id).pk if mark.subject_id else None)
                mark_scale = resolve_scale_fast(
                    school.pk, assignment.school_section, assignment.sub_section,
                    subject_id=mark_subject_id,
                )
                if mark_scale:
                    mark.performance_level, mark.points = get_subject_level_fast(new_pct, mark_scale)
                else:
                    mark.performance_level = mark.performance_level or '-'
                    mark.points = mark.points or 0
                mark.integrity_checksum = compute_mark_checksum(mark)
                marks_to_update.append(mark)

        if marks_to_update:
            Mark.all_objects.bulk_update(
                marks_to_update,
                ['maximum_marks', 'score', 'raw_score', 'performance_level', 'points', 'integrity_checksum'],
                batch_size=250,
            )

    return JsonResponse({'ok': True, 'updated': len(marks_to_update), 'new_maximum': new_maximum})


@login_required(login_url='login')
@tenant_read_only_required
def return_mark_sheet(request):
    """
    AJAX endpoint: teacher returns their own submitted sheet to editable state.
    POST: assignment_id, exam_id
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if not user_can_mutate_marks(request.user):
        return JsonResponse({'error': 'Permission denied'}, status=403)

    try:
        teacher = get_school_object_or_403(Teacher, request, user=request.user)
    except (PermissionDenied, Http404):
        return JsonResponse({'error': 'No teacher profile'}, status=403)

    assignment_id = request.POST.get('assignment_id')
    exam_id = request.POST.get('exam_id')

    if not all([assignment_id, exam_id]):
        return JsonResponse({'error': 'Missing parameters'}, status=400)

    school = get_request_school(request)

    try:
        assignment = SubjectAssignment.objects.get(id=assignment_id, school=school, teacher_profile=teacher)
    except SubjectAssignment.DoesNotExist:
        return JsonResponse({'error': 'Assignment not found'}, status=404)

    try:
        exam = Exam.objects.get(id=exam_id, school=school, status='active')
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found'}, status=404)

    submission = MarkSubmission.objects.filter(
        school=school, teacher=teacher, subject=assignment.subject,
        class_name=assignment.class_name, stream=assignment.stream,
        exam_name=exam.name, term=exam.term, year=exam.year,
        school_section=assignment.school_section,
    ).first()

    if not submission:
        return JsonResponse({'error': 'No submission found'}, status=404)

    if submission.status in ('approved', 'published'):
        return JsonResponse({'error': 'Already reviewed by admin — cannot return'}, status=403)

    submission.status = 'returned'
    submission.admin_note = 'Returned by teacher for editing'
    submission.reviewed_at = None
    submission.published_at = None
    submission.save()

    return JsonResponse({'ok': True})
