"""
Exam & Mark Entry Views
========================
Handles teacher mark entry, admin exam management, stream-level and
individual submission reviews, and assessment lock management.
"""

import datetime
import json

import bleach
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Avg, IntegerField
from django.db.models.functions import Cast
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

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
    get_subject_marks,
    get_subject_students,
)
from ..models import (
    AssessmentLock,
    Exam,
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
        .select_related('teacher_profile__user', 'subject')
        .order_by('class_name', 'stream', 'subject__code')
    )

    active_exams = Exam.objects.filter(school=school, status='active', school_section='JSS').order_by('-year', 'term', 'name')

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

        for student in students:
            existing = Mark.objects.filter(
                student=student,
                subject=selected_assignment.subject,
                term=selected_exam.term,
                exam_type=selected_exam.name,
                year=selected_exam.year,
            ).first()

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

            maximum_marks = current_maximum_marks

            missing_students = []
            saved_count = 0
            deleted_count = 0

            for student in students:
                value = request.POST.get(f'score_{student.id}', '').strip()

                if not value:
                    _del_lookup = dict(
                        school=school,
                        student=student,
                        subject=selected_assignment.subject,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                        school_section=selected_assignment.school_section,
                    )
                    _, del_count = Mark.all_objects.filter(**_del_lookup).delete()
                    if del_count:
                        deleted_count += 1
                    missing_students.append(student.name)
                    continue

                if value.upper() == "AB":
                    if selected_assignment.subject.code in RELIGION_SUBJECTS:
                        religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                        Student.objects.filter(id=student.id).update(religion=religion_tag)
                        opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                        if opposite:
                            Mark.all_objects.filter(
                                school=school,
                                student=student,
                                subject=opposite,
                                term=selected_exam.term,
                                exam_type=selected_exam.name,
                                year=selected_exam.year,
                                school_section=selected_assignment.school_section,
                            ).delete()

                    _mark_lookup = dict(
                        school=school,
                        student=student,
                        subject=selected_assignment.subject,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                        school_section=selected_assignment.school_section,
                        sub_section=selected_assignment.sub_section,
                    )
                    Mark.all_objects.filter(**_mark_lookup).delete()
                    Mark.all_objects.create(
                        **_mark_lookup,
                        raw_score=None,
                        maximum_marks=maximum_marks,
                        score=0,
                        is_absent=True,
                    )
                    saved_count += 1
                    continue

                try:
                    raw_score = int(value)
                except ValueError:
                    messages.error(request, f"Invalid score for {student.name}. Use a number or AB.")
                    return redirect(
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                if raw_score < 0 or raw_score > maximum_marks:
                    messages.error(request, f"{student.name}'s score exceeds the total marks.")
                    return redirect(
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                _mark_lookup = dict(
                    school=school,
                    student=student,
                    subject=selected_assignment.subject,
                    term=selected_exam.term,
                    exam_type=selected_exam.name,
                    year=selected_exam.year,
                    school_section=selected_assignment.school_section,
                    sub_section=selected_assignment.sub_section,
                )
                Mark.all_objects.filter(**_mark_lookup).delete()
                Mark.all_objects.create(
                    **_mark_lookup,
                    raw_score=raw_score,
                    maximum_marks=maximum_marks,
                    score=round((raw_score / maximum_marks) * 100),
                    is_absent=False,
                )
                saved_count += 1

                # ================================================================
                # CHANGE 2 — Auto-tag student with religion on first score entry
                # ================================================================
                if selected_assignment.subject.code in RELIGION_SUBJECTS:
                    religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                    Student.objects.filter(id=student.id).update(religion=religion_tag)
                    opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                    if opposite:
                        Mark.all_objects.filter(
                            school=school,
                            student=student,
                            subject=opposite,
                            term=selected_exam.term,
                            exam_type=selected_exam.name,
                            year=selected_exam.year,
                            school_section=selected_assignment.school_section,
                        ).delete()

            # ================================================================
            # CHANGE 3 — Skip must-fill check for IRE/CRE/HRE subjects
            # ================================================================
            if missing_students and selected_assignment.subject.code not in RELIGION_SUBJECTS:
                messages.error(request, "Please enter a score or AB for every learner before submitting.")
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

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
            return redirect('select_exam')

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

    return render(request, 'students/select_exam_details.html', {
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
            section = get_request_school_section(request)

            if section == 'LOWER_PRIMARY':
                exam_db_section = 'PRIMARY'
                exam_sub_section = 'LOWER'
            elif section == 'PRIMARY':
                exam_db_section = 'PRIMARY'
                exam_sub_section = 'UPPER'
            else:
                exam_db_section = 'JSS'
                exam_sub_section = None

            if exam_name and term and year:
                Exam.all_objects.update_or_create(
                    school=school,
                    name=exam_name,
                    term=term,
                    year=year,
                    defaults={
                        "status": status,
                        "school_section": exam_db_section,
                        "sub_section": exam_sub_section,
                    },
                )
                messages.success(request, "Assessment has been saved successfully.")
            else:
                messages.error(request, "Please provide assessment name, term, and year.")

            return redirect("manage_exams")

        if action_type == "toggle_status":
            exam_id = request.POST.get("exam_id")
            exam = get_school_object_or_403(Exam, request, id=exam_id)

            exam.status = "closed" if exam.status == "active" else "active"
            exam.save()

            messages.success(request, "Assessment status has been updated.")
            return redirect("manage_exams")

        if action_type == "delete_exam":
            exam_id = request.POST.get("exam_id")
            exam = get_school_object_or_403(Exam, request, id=exam_id)
            exam.delete()

            messages.success(request, "Assessment has been deleted.")
            return redirect("manage_exams")

    # -----------------------------
    # Exam registry
    # -----------------------------
    section = get_request_school_section(request)
    exams = Exam.objects.filter(school=school)
    if section == 'LOWER_PRIMARY':
        exams = exams.filter(school_section='PRIMARY')
    elif section == 'PRIMARY':
        exams = exams.filter(school_section='PRIMARY')
    elif section == 'JSS':
        exams = exams.filter(school_section='JSS')
    exams = exams.order_by("-year", "term", "name")

    selected_exam_id = request.GET.get("exam_id")
    selected_exam = None

    if selected_exam_id:
        selected_exam = Exam.objects.filter(school=school, id=selected_exam_id).first()
        if selected_exam:
            if section == 'LOWER_PRIMARY' and selected_exam.school_section != 'PRIMARY':
                selected_exam = None
            elif section == 'PRIMARY' and selected_exam.school_section != 'PRIMARY':
                selected_exam = None
            elif section == 'JSS' and selected_exam.school_section != 'JSS':
                selected_exam = None

    if not selected_exam:
        selected_exam = Exam.objects.filter(school=school, status="active")
        if section == 'LOWER_PRIMARY':
            selected_exam = selected_exam.filter(school_section='PRIMARY')
        elif section == 'PRIMARY':
            selected_exam = selected_exam.filter(school_section='PRIMARY')
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
            SubjectAssignment.objects
            .filter(school=school)
            .select_related("teacher_profile", "teacher_profile__user")
            .order_by("class_name", "stream", "subject__code")
        )
        if section == 'LOWER_PRIMARY':
            assignments = assignments.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            assignments = assignments.filter(school_section='PRIMARY', sub_section='UPPER')
        elif section == 'JSS':
            assignments = assignments.filter(school_section='JSS')

        for assignment in assignments:
            total_students = get_religion_aware_student_count(
            assignment.class_name,
            assignment.stream,
            assignment.subject,
            )

            marks_qs = get_subject_marks(
                assignment.class_name,
                assignment.stream,
                assignment.subject,
                selected_exam.term,
                selected_exam.name,
                selected_exam.year,
            )

            captured_count = marks_qs.count()
            absent_count = marks_qs.filter(is_absent=True).count()
            missing_count = max(total_students - captured_count, 0)

            submission = MarkSubmission.objects.filter(
                teacher=assignment.teacher_profile,
                subject=assignment.subject,
                class_name=assignment.class_name,
                stream=assignment.stream,
                exam_name=selected_exam.name,
                term=selected_exam.term,
                year=selected_exam.year,
                school_section=assignment.school_section,
            ).first()

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

    context = {
        "exams": exams,
        "selected_exam": selected_exam,
        "grouped_streams": grouped_streams.values(),
        "monitor_summary": monitor_summary,
        "current_year": current_year,
        "terms": TERM_CHOICES,
        "status_choices": status_choices,
    }

    return render(request, "students/manage_exams.html", context)


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

    exam = Exam.objects.filter(school=school, id=exam_id).first() if exam_id else None
    if exam and exam.school_section != exam_section_filter:
        exam = None
    if not exam:
        exam = Exam.objects.filter(school=school, status="active", school_section=exam_section_filter).order_by("-year", "term", "name").first()
    if not exam:
        exam = Exam.objects.filter(school=school, school_section=exam_section_filter).order_by("-year", "term", "name").first()
    if not exam:
        messages.error(request, "Create an assessment first before reviewing stream submissions.")
        return redirect("manage_exams")

    class_name = request.GET.get("class_name") or request.POST.get("class_name")
    stream = request.GET.get("stream") or request.POST.get("stream")

    if not class_name or not stream:
        exams = Exam.objects.filter(school=school).order_by("-year", "term", "name")
        stream_cards = []
        pairs = (
            SubjectAssignment.objects.filter(school=school)
            .values("class_name", "stream")
            .distinct()
            .order_by("class_name", "stream")
        )
        section = get_request_school_section(request)
        if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
            exam_db_section = 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS'
            exams = exams.filter(school_section=exam_db_section)
            pairs = pairs.filter(school_section=exam_db_section)
        for pair in pairs:
            _, totals = get_stream_submission_summary(pair["class_name"], pair["stream"], exam)
            stream_cards.append({
                "class_name": pair["class_name"],
                "stream": pair["stream"],
                "totals": totals,
            })
        return render(request, "students/stream_review_list.html", {
            "exam": exam,
            "exams": exams,
            "stream_cards": stream_cards,
        })

    valid_pairs = set(
        SubjectAssignment.objects.filter(school=school)
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

            for student in students_for_sheet:
                value = request.POST.get(f"score_{student.id}", "").strip()
                if not value:
                    continue

                if assignment.subject.code in RELIGION_SUBJECTS:
                    religion_tag = RELIGION_TAG.get(assignment.subject.code, "")
                    Student.objects.filter(id=student.id).update(religion=religion_tag)
                    opposite = _resolve_opposite_religion_subject(school, assignment)
                    if opposite:
                        Mark.all_objects.filter(
                            school=school,
                            student=student,
                            subject=opposite,
                            term=exam.term,
                            exam_type=exam.name,
                            year=exam.year,
                            school_section=assignment.school_section,
                            sub_section=assignment.sub_section,
                        ).delete()

                if value.upper() == "AB":
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
                    Mark.all_objects.filter(**_adm_lookup).delete()
                    Mark.all_objects.create(
                        **_adm_lookup,
                        raw_score=None,
                        maximum_marks=maximum_marks,
                        score=0,
                        is_absent=True,
                    )
                    corrected_count += 1
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
                Mark.all_objects.filter(**_adm_lookup).delete()
                Mark.all_objects.create(
                    **_adm_lookup,
                    raw_score=raw_score,
                    maximum_marks=maximum_marks,
                    score=round((raw_score / maximum_marks) * 100),
                    is_absent=False,
                )
                corrected_count += 1

            submission.admin_note = admin_note
            if submission.status != "published":
                submission.status = "submitted"
                submission.published_at = None
            submission.reviewed_at = timezone.now()
            submission.save()
            messages.success(request, f"{corrected_count} learner score correction(s) saved.")

        elif action_type == "return_submission":
            submission.status = "returned"
            submission.admin_note = admin_note
            submission.reviewed_at = timezone.now()
            submission.save()

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

PRIMARY_PERFORMANCE_SCALE = [
    (75, 'EE', 4),
    (50, 'ME', 3),
    (25, 'AE', 2),
    (0,  'BE', 1),
]

PRIMARY_GRADE_CHOICES = ['Grade 4', 'Grade 5', 'Grade 6']
LOWER_PRIMARY_GRADE_CHOICES = ['Grade 1', 'Grade 2', 'Grade 3']


def _get_primary_performance(percentage):
    """Return (descriptor, points) for a primary percentage score.
    Uses school-specific GradingConfig if available."""
    from ..models import GradingConfig
    from ..school_scope import get_current_school, get_current_school_section

    school = get_current_school()
    section = get_current_school_section()

    if school and section:
        config = GradingConfig.all_objects.filter(school=school, school_section=section).first()
        if config and config.subject_scale:
            return config.get_subject_level(percentage)

    for threshold, descriptor, points in PRIMARY_PERFORMANCE_SCALE:
        if percentage >= threshold:
            return descriptor, points
    return 'BE', 1


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
        SubjectAssignment.objects
        .filter(school=school, teacher_profile=teacher, school_section=exam_section)
        .select_related('teacher_profile__user')
        .order_by('class_name', 'stream', 'subject__code')
    )
    if exam_sub_section:
        assignments = assignments.filter(sub_section=exam_sub_section)

    active_exams = Exam.objects.filter(
        school=school, status='active', school_section=exam_section
    ).order_by('-year', 'term', 'name')

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

        for student in students:
            existing = Mark.objects.filter(
                student=student,
                subject=selected_assignment.subject,
                term=selected_exam.term,
                exam_type=selected_exam.name,
                year=selected_exam.year,
            ).first()

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
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            if is_submitted:
                messages.error(request, "This sheet has already been submitted and cannot be edited. Ask the admin to return it first.")
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

            maximum_marks = current_maximum_marks

            missing_students = []
            saved_count = 0
            deleted_count = 0

            for student in students:
                value = request.POST.get(f'score_{student.id}', '').strip()

                if not value:
                    _del_lookup = dict(
                        school=school,
                        student=student,
                        subject=selected_assignment.subject,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                        school_section=exam_section,
                        sub_section=exam_sub_section,
                    )
                    _, del_count = Mark.all_objects.filter(**_del_lookup).delete()
                    if del_count:
                        deleted_count += 1
                    missing_students.append(student.name)
                    continue

                if value.upper() == "AB":
                    # Auto-tag student religion on CRE/IRE for primary
                    if selected_assignment.subject.code in RELIGION_SUBJECTS:
                        religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                        Student.objects.filter(id=student.id).update(religion=religion_tag)
                        opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                        if opposite:
                            Mark.all_objects.filter(
                                school=school,
                                student=student,
                                subject=opposite,
                                term=selected_exam.term,
                                exam_type=selected_exam.name,
                                year=selected_exam.year,
                                school_section=exam_section,
                                sub_section=exam_sub_section,
                            ).delete()

                    _mark_lookup = dict(
                        school=school,
                        student=student,
                        subject=selected_assignment.subject,
                        term=selected_exam.term,
                        exam_type=selected_exam.name,
                        year=selected_exam.year,
                    )
                    Mark.all_objects.filter(**_mark_lookup).delete()
                    Mark.all_objects.create(
                        **_mark_lookup,
                        school_section=exam_section,
                        sub_section=exam_sub_section,
                        raw_score=None,
                        maximum_marks=maximum_marks,
                        score=0,
                        is_absent=True,
                        primary_raw_score='AB',
                        primary_performance_point='AB',
                        primary_descriptor='AB',
                    )
                    saved_count += 1
                    continue

                try:
                    raw_score = int(value)
                except ValueError:
                    messages.error(request, f"Invalid score for {student.name}. Use a number or AB.")
                    return redirect(
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                if raw_score < 0 or raw_score > maximum_marks:
                    messages.error(request, f"{student.name}'s score exceeds the total marks.")
                    return redirect(
                        f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                    )

                percentage = round((raw_score / maximum_marks) * 100)
                descriptor, points = _get_primary_performance(percentage)

                _mark_lookup = dict(
                    school=school,
                    student=student,
                    subject=selected_assignment.subject,
                    term=selected_exam.term,
                    exam_type=selected_exam.name,
                    year=selected_exam.year,
                )
                Mark.all_objects.filter(**_mark_lookup).delete()
                Mark.all_objects.create(
                    **_mark_lookup,
                    school_section=exam_section,
                    sub_section=exam_sub_section,
                    raw_score=raw_score,
                    maximum_marks=maximum_marks,
                    score=percentage,
                    is_absent=False,
                    primary_raw_score=str(raw_score),
                    primary_performance_point=str(points),
                    primary_descriptor=descriptor,
                )
                saved_count += 1

                # Auto-tag student religion on CRE/IRE for primary
                if selected_assignment.subject.code in RELIGION_SUBJECTS:
                    religion_tag = RELIGION_TAG.get(selected_assignment.subject.code, '')
                    Student.objects.filter(id=student.id).update(religion=religion_tag)
                    opposite = _resolve_opposite_religion_subject(school, selected_assignment)
                    if opposite:
                        Mark.all_objects.filter(
                            school=school,
                            student=student,
                            subject=opposite,
                            term=selected_exam.term,
                            exam_type=selected_exam.name,
                            year=selected_exam.year,
                            school_section=exam_section,
                            sub_section=exam_sub_section,
                        ).delete()

            if missing_students and selected_assignment.subject.code not in RELIGION_SUBJECTS:
                messages.error(request, "Please enter a score or AB for every learner before submitting.")
                return redirect(
                    f"{request.path}?assignment_id={selected_assignment.id}&exam_id={selected_exam.id}"
                )

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
            return redirect('select_exam_primary')

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

    return render(request, 'students/select_exam_details.html', {
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

    if not score_value:
        Mark.all_objects.filter(
            school=school, student=student, subject=assignment.subject,
            term=exam.term, exam_type=exam.name, year=exam.year,
            school_section=assignment.school_section,
            sub_section=assignment.sub_section,
        ).delete()
        return JsonResponse({'ok': True, 'cleared': True})

    # Always delete existing mark first, then create fresh — avoids integrity
    # checksum collision in update_or_create (save() verifies checksum before
    # the new field values are applied).
    _mark_lookup = dict(
        school=school, student=student, subject=assignment.subject,
        term=exam.term, exam_type=exam.name, year=exam.year,
        school_section=assignment.school_section,
        sub_section=assignment.sub_section,
    )

    if score_value.upper() == 'AB':
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

        Mark.all_objects.filter(**_mark_lookup).delete()
        Mark.all_objects.create(
            **_mark_lookup,
            raw_score=None,
            maximum_marks=maximum_marks,
            score=0,
            is_absent=True,
        )
        return JsonResponse({'ok': True, 'absent': True})

    try:
        raw_score = int(score_value)
    except ValueError:
        return JsonResponse({'error': 'Invalid score'}, status=400)

    if raw_score < 0 or raw_score > maximum_marks:
        return JsonResponse({'error': 'Score exceeds total marks'}, status=400)

    Mark.all_objects.filter(**_mark_lookup).delete()
    Mark.all_objects.create(
        **_mark_lookup,
        raw_score=raw_score,
        maximum_marks=maximum_marks,
        score=round((raw_score / maximum_marks) * 100),
        is_absent=False,
    )

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

    return JsonResponse({'ok': True, 'saved': True})


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
