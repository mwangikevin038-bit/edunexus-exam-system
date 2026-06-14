"""
Profile and dashboard views for the EduNexus student management system.

Provides teacher-facing and school admin dashboards with metrics,
missing-marks feeds, grade performance, and population statistics.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Q
from django.shortcuts import redirect, render

from .constants import GRADE_CHOICES
from .helpers import get_teacher_for_user, get_class_teacher_scope, get_published_contexts_for_user
from ..security import get_request_school, get_request_school_section, school_admin_required, user_has_main_school_admin_override
from ..models import (
    Exam,
    Mark,
    MarkSubmission,
    Student,
    SubjectAssignment,
    Teacher,
)


@login_required(login_url='login')
def profile_view(request):
    """
    Links the authenticated user session to the interface layout via the
    unified Teacher model.
    """
    try:
        teacher = Teacher.objects.select_related('user').get(user=request.user)
    except Teacher.DoesNotExist:
        teacher = None
    assignments = SubjectAssignment.objects.filter(teacher_profile=teacher).order_by(
        'class_name', 'stream', 'subject_code'
    ) if teacher else SubjectAssignment.objects.none()
    section = get_request_school_section(request)
    submissions = MarkSubmission.objects.filter(teacher=teacher)
    if section in ('PRIMARY', 'JSS'):
        submissions = submissions.filter(school_section=section)
    if not teacher:
        submissions = MarkSubmission.objects.none()
    class_scope = get_class_teacher_scope(teacher)

    return render(request, 'students/profile.html', {
        'user': request.user,
        'teacher': teacher,
        'assignments': assignments,
        'assignment_count': assignments.count(),
        'submitted_count': submissions.filter(status__in=['submitted', 'approved', 'published']).count(),
        'returned_count': submissions.filter(status='returned').count(),
        'published_count': submissions.filter(status='published').count(),
        'class_teacher_scope': class_scope,
    })


@login_required(login_url='login')
def dashboard(request):
    """Teacher-facing summary dashboard."""
    school = get_request_school(request)
    teacher = get_teacher_for_user(request.user)
    if user_has_main_school_admin_override(request.user):
        return redirect('school_admin_dashboard')

    assignments = SubjectAssignment.objects.filter(school=school, teacher_profile=teacher).order_by(
        'class_name', 'stream', 'subject_code'
    ) if teacher and school else SubjectAssignment.objects.none()
    active_exams = Exam.objects.filter(school=school, status='active').order_by('-year', 'term', 'name') if school else Exam.objects.none()
    submissions = MarkSubmission.objects.filter(school=school, teacher=teacher) if teacher and school else MarkSubmission.objects.none()
    section = get_request_school_section(request)
    if section in ('PRIMARY', 'JSS'):
        submissions = submissions.filter(school_section=section)
    class_scope = get_class_teacher_scope(teacher)

    active_sheet_count = assignments.count() * active_exams.count()
    submitted_count = submissions.filter(status__in=['submitted', 'approved', 'published']).count()
    returned_count = submissions.filter(status='returned').count()
    published_count = submissions.filter(status='published').count()
    in_progress_count = submissions.exclude(status__in=['submitted', 'approved', 'published']).count()
    recent_submissions = submissions.order_by('-submitted_at', '-reviewed_at')[:6]
    published_contexts = get_published_contexts_for_user(request.user)

    return render(request, 'students/dashboard.html', {
        'teacher': teacher,
        'assignments': assignments[:6],
        'assignment_count': assignments.count(),
        'active_exam_count': active_exams.count(),
        'active_sheet_count': active_sheet_count,
        'submitted_count': submitted_count,
        'returned_count': returned_count,
        'published_count': published_count,
        'in_progress_count': in_progress_count,
        'recent_submissions': recent_submissions,
        'published_contexts': published_contexts[:4],
        'class_teacher_scope': class_scope,
        'current_year': datetime.date.today().year,
    })


@login_required(login_url='login')
@school_admin_required
def school_admin_dashboard(request):
    """
    Executive metric panel for the School ICT Admin.
    Shows population stats, missing-marks feed, grade performance, and best stream.
    Workspace-aware: filters data by school_section when toggled.
    """
    from .exams import PRIMARY_GRADE_CHOICES

    current_year = datetime.date.today().year

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Determine workspace section for filtering
    section = get_request_school_section(request)
    is_primary = section == 'PRIMARY'
    grade_choices = PRIMARY_GRADE_CHOICES if is_primary else ['Grade 7', 'Grade 8', 'Grade 9']

    # Base querysets filtered by section
    student_qs = Student.objects.filter(school=school)
    teacher_qs = Teacher.objects.filter(school=school)
    exam_qs = Exam.objects.filter(school=school)
    assignment_qs = SubjectAssignment.objects.filter(school=school)
    submission_qs = MarkSubmission.objects.filter(school=school)
    mark_qs = Mark.objects.filter(school=school)

    if section in ('PRIMARY', 'JSS'):
        student_qs = student_qs.filter(school_section=section)
        teacher_qs = teacher_qs.filter(school_section=section)
        exam_qs = exam_qs.filter(school_section=section)
        assignment_qs = assignment_qs.filter(school_section=section)
        submission_qs = submission_qs.filter(school_section=section)
        mark_qs = mark_qs.filter(school_section=section)

    active_exam = exam_qs.filter(status="active").order_by("-year", "term", "name").first()

    # --- Headline counts ---
    total_students = student_qs.count()
    total_teachers = teacher_qs.count()
    total_exams    = exam_qs.count()

    # --- Class/stream population breakdown ---
    distribution = (
        student_qs
        .values('class_name', 'stream')
        .annotate(student_count=Count('id'))
    )
    class_stats = {g: {'streams': {}, 'total': 0} for g in grade_choices}
    for item in distribution:
        cls, strm, cnt = item['class_name'], item['stream'], item['student_count']
        if cls in class_stats:
            class_stats[cls]['streams'][strm] = cnt
            class_stats[cls]['total'] += cnt

    # --- Missing marks tracer ---
    all_assignments      = assignment_qs.select_related('teacher_profile__user').all()
    missing_entries_feed = []
    for assignment in all_assignments:
        submission = None
        if active_exam:
            submission = submission_qs.filter(
                teacher=assignment.teacher_profile,
                subject_code=assignment.subject_code,
                class_name=assignment.class_name,
                stream=assignment.stream,
                exam_name=active_exam.name,
                term=active_exam.term,
                year=active_exam.year,
            ).first()

        if not submission or submission.status in ["returned"]:
            missing_entries_feed.append({
                'teacher_name': assignment.teacher_profile.get_full_title(),
                'subject_name': assignment.get_subject_code_display(),
                'target_class': f"{assignment.class_name} {assignment.stream}",
                'phone':        assignment.teacher_profile.phone_number,
                'status':       submission.get_status_display() if submission else "Not Started",
            })

    total_assignments    = all_assignments.count()
    missing_entries_count = len(missing_entries_feed)
    completed_assignments = max(total_assignments - missing_entries_count, 0)
    completion_rate = round((completed_assignments / total_assignments) * 100) if total_assignments else 0

    # --- Grade performance averages ---
    grade_colors = {
        'Grade 4': '#8ae325', 'Grade 5': '#38bdf8', 'Grade 6': '#f59e0b',
        'Grade 7': '#8ae325', 'Grade 8': '#38bdf8', 'Grade 9': '#f59e0b',
    }
    published_mark_filter = Q(pk__in=[])
    if active_exam:
        published_submissions = submission_qs.filter(
            exam_name=active_exam.name,
            term=active_exam.term,
            year=active_exam.year,
            status="published",
        )
        for submission in published_submissions:
            published_mark_filter |= Q(
                student__class_name=submission.class_name,
                student__stream=submission.stream,
                subject=submission.subject_code,
                exam_type=active_exam.name,
                term=active_exam.term,
                year=active_exam.year,
            )

    published_marks = mark_qs.filter(published_mark_filter)
    grade_average_map = {
        item['student__class_name']: round(item['avg_score'] or 0, 1)
        for item in published_marks
            .values('student__class_name').annotate(avg_score=Avg('score'))
    }
    grade_performance_rows = [
        {'label': g, 'score': grade_average_map.get(g, 0), 'color': grade_colors[g]}
        for g in grade_choices
    ]

    overall_average = round(
        published_marks.aggregate(avg_score=Avg('score'))['avg_score'] or 0, 1
    )

    best_stream_data = (
        published_marks
        .values('student__class_name', 'student__stream')
        .annotate(avg_score=Avg('score'))
        .order_by('-avg_score')
        .first()
    )
    best_stream = (
        f"{best_stream_data['student__class_name']} {best_stream_data['student__stream']}"
        if best_stream_data else "No Data"
    )

    # --- Gender breakdown ---
    gender_counts = (
        student_qs
        .values('gender')
        .annotate(count=Count('id'))
    )
    boys_count = 0
    girls_count = 0
    for g in gender_counts:
        if g['gender'] == 'Male':
            boys_count = g['count']
        elif g['gender'] == 'Female':
            girls_count = g['count']

    # --- Active classes count ---
    active_classes = len([g for g, d in class_stats.items() if d['total'] > 0])

    # --- Section label for template ---
    section_label = 'Upper Primary' if is_primary else 'Junior Secondary' if section == 'JSS' else 'All Sections'

    return render(request, 'students/dashboard_admin.html', {
        'total_students':       total_students,
        'total_teachers':       total_teachers,
        'total_exams':          total_exams,
        'current_year':         current_year,
        'class_stats':          class_stats,
        'missing_entries_feed': missing_entries_feed,
        'missing_entries_count': missing_entries_count,
        'total_assignments':    total_assignments,
        'completed_assignments': completed_assignments,
        'completion_rate':      completion_rate,
        'exam_window_status':   "Open" if missing_entries_count > 0 else "Complete",
        'active_term_label':    f"{active_exam.term} | {active_exam.year}" if active_exam else f"Term 1 | {current_year}",
        'grade_performance_rows': grade_performance_rows,
        'overall_average':      overall_average,
        'best_stream':          best_stream,
        'admin_override_enabled': user_has_main_school_admin_override(request.user),
        'boys_count':           boys_count,
        'girls_count':          girls_count,
        'active_classes':       active_classes,
        'is_primary':           is_primary,
        'section_label':        section_label,
    })