"""
Profile and dashboard views for the EduNexus student management system.

Provides teacher-facing and school admin dashboards with metrics,
missing-marks feeds, grade performance, and population statistics.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Case, Count, IntegerField, Q, When, Value
from django.db.models.functions import Cast, Substr
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .constants import GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, PRIMARY_GRADE_CHOICES
from .helpers import get_teacher_for_user, get_class_teacher_scope, get_published_contexts_for_user
from ..security import get_request_school, get_request_school_section, school_admin_required, user_has_main_school_admin_override
from ..models import (
    Exam,
    GradingConfig,
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
        'class_name', 'stream', 'subject__code'
    ) if teacher else SubjectAssignment.objects.none()
    section = get_request_school_section(request)
    # Scope assignments to current workspace section
    if section == 'LOWER_PRIMARY':
        assignments = assignments.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        assignments = assignments.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        assignments = assignments.filter(school_section='JSS')
    submissions = MarkSubmission.objects.filter(teacher=teacher)
    if section == 'LOWER_PRIMARY':
        submissions = submissions.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        submissions = submissions.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        submissions = submissions.filter(school_section='JSS')
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

    assignments_qs = SubjectAssignment.objects.filter(school=school, teacher_profile=teacher).order_by(
        'class_name', 'stream', 'subject__code'
    ) if teacher and school else SubjectAssignment.objects.none()
    active_exams = Exam.objects.filter(school=school, status='active', is_deleted=False).order_by('-year', 'term', 'name') if school else Exam.objects.none()
    submissions = MarkSubmission.objects.filter(school=school, teacher=teacher) if teacher and school else MarkSubmission.objects.none()
    section = get_request_school_section(request)
    if section == 'LOWER_PRIMARY':
        submissions = submissions.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        submissions = submissions.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        submissions = submissions.filter(school_section='JSS')
    class_scope = get_class_teacher_scope(teacher)

    active_sheet_count = assignments_qs.count() * active_exams.count()
    submitted_count = submissions.filter(status__in=['submitted', 'approved', 'published']).count()
    returned_count = submissions.filter(status='returned').count()
    published_count = submissions.filter(status='published').count()
    in_progress_count = submissions.exclude(status__in=['submitted', 'approved', 'published']).count()
    recent_submissions = submissions.order_by('-submitted_at', '-reviewed_at')[:6]
    published_contexts = get_published_contexts_for_user(request.user)

    assignments = list(assignments_qs[:6])

    active_exam = active_exams.first()
    if active_exam and assignments:
        marks_filter = dict(
            school=school,
            exam_type=active_exam.name,
            term=active_exam.term,
            year=active_exam.year,
            subject__in=[a.subject_id for a in assignments],
        )
        if section == 'LOWER_PRIMARY':
            marks_filter['school_section'] = 'PRIMARY'
            marks_filter['sub_section'] = 'LOWER'
        elif section == 'PRIMARY':
            marks_filter['school_section'] = 'PRIMARY'
            marks_filter['sub_section'] = 'UPPER'
        elif section == 'JSS':
            marks_filter['school_section'] = 'JSS'

        mark_entries = (
            Mark.objects.filter(**marks_filter)
            .values('subject_id', 'student__class_name', 'student__stream')
            .annotate(student_count=Count('student_id', distinct=True))
        )
        mark_map = {}
        for entry in mark_entries:
            key = (entry['subject_id'], entry['student__class_name'], entry['student__stream'])
            mark_map[key] = entry['student_count']

        student_counts = (
            Student.objects.filter(school=school, is_active=True)
            .values('class_name', 'stream')
            .annotate(cnt=Count('id'))
        )
        student_count_map = {(s['class_name'], s['stream']): s['cnt'] for s in student_counts}

        for assignment in assignments:
            mark_key = (assignment.subject_id, assignment.class_name, assignment.stream)
            marks_entered = mark_map.get(mark_key, 0)
            total_students = student_count_map.get((assignment.class_name, assignment.stream), 0)
            assignment.marks_entered = marks_entered
            assignment.total_students = total_students
            assignment.progress_pct = round((marks_entered / total_students) * 100) if total_students else 0
    else:
        for assignment in assignments:
            assignment.marks_entered = 0
            assignment.total_students = 0
            assignment.progress_pct = 0

    return render(request, 'students/dashboard.html', {
        'teacher': teacher,
        'assignments': assignments,
        'assignment_count': assignments_qs.count(),
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
        'section': section,
    })


@login_required(login_url='login')
@school_admin_required
@never_cache
def school_admin_dashboard(request):
    """
    Executive metric panel for the School ICT Admin.
    Shows population stats, missing-marks feed, grade performance, and best stream.
    Admin sees ALL data across all sections — no section filtering.
    """
    current_year = datetime.date.today().year

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Admin sees all grades (no section filtering)
    grade_choices = LOWER_PRIMARY_GRADE_CHOICES + PRIMARY_GRADE_CHOICES + ['Grade 7', 'Grade 8', 'Grade 9']

    # Unfiltered querysets — admin sees everything
    student_qs = Student.all_objects.filter(school=school, is_active=True)
    teacher_qs = Teacher.all_objects.filter(school=school)
    exam_qs = Exam.all_objects.filter(school=school, is_deleted=False)
    assignment_qs = SubjectAssignment.all_objects.filter(school=school)
    submission_qs = MarkSubmission.all_objects.filter(school=school)
    mark_qs = Mark.all_objects.filter(school=school)

    # Exam name logical order: End of Term > Mid Term > Opener
    exam_order = Case(
        When(name__icontains='end of term', then=Value(3)),
        When(name__icontains='mid term', then=Value(2)),
        When(name__icontains='opener', then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )
    active_exam = exam_qs.filter(status="active").annotate(
        term_num=Cast(Substr('term', 6), output_field=IntegerField()),
        name_order=exam_order,
    ).order_by("-year", "-term_num", "-name_order").first()

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
    all_assignments      = assignment_qs.select_related('teacher_profile__user', 'subject').all()

    # Batch-fetch all submissions for active exam (ONE query instead of N)
    submission_map = {}
    if active_exam:
        active_submissions = submission_qs.filter(
            exam_name=active_exam.name,
            term=active_exam.term,
            year=active_exam.year,
        ).select_related('teacher__user', 'subject')
        for sub in active_submissions:
            key = (sub.teacher_id, sub.subject_id, sub.class_name, sub.stream)
            submission_map[key] = sub

    missing_entries_feed = []
    for assignment in all_assignments:
        sub_key = (assignment.teacher_profile_id, assignment.subject_id, assignment.class_name, assignment.stream)
        submission = submission_map.get(sub_key)

        if not submission or submission.status in ["returned"]:
            missing_entries_feed.append({
                'teacher_name': assignment.teacher_profile.get_full_title(),
                'subject_name': assignment.subject.name if assignment.subject else '—',
                'target_class': f"{assignment.class_name} {assignment.stream}",
                'phone':        assignment.teacher_profile.phone_number,
                'status':       submission.get_status_display() if submission else "Not Started",
            })

    total_assignments    = all_assignments.count()
    missing_entries_count = len(missing_entries_feed)
    completed_assignments = max(total_assignments - missing_entries_count, 0)
    completion_rate = round((completed_assignments / total_assignments) * 100) if total_assignments else 0

    # --- Grade performance cards (real data from latest published exam) ---
    # Section-aware accent colors (canonical system colors)
    SECTION_COLORS = {
        'LOWER_PRIMARY': {'accent': '#B45309', 'bg': '#FFFBEB', 'border': '#fde68a', 'text': '#92400e'},
        'PRIMARY':       {'accent': '#00674F', 'bg': '#ECFDF5', 'border': '#a7f3d0', 'text': '#065f46'},
        'JSS':           {'accent': '#305CDE', 'bg': '#EFF6FF', 'border': '#bfdbfe', 'text': '#1e40af'},
    }

    def _section_key_for_grade(g):
        if g in ('Grade 1', 'Grade 2', 'Grade 3'):
            return 'LOWER_PRIMARY'
        if g in ('Grade 4', 'Grade 5', 'Grade 6'):
            return 'PRIMARY'
        return 'JSS'

    def _mean_grade_from_score(avg_score, section_key):
        scale = GradingConfig.get_default_subject_scale(section_key)
        for entry in scale:
            if entry['min_score'] <= avg_score < entry['max_score'] + 1:
                return entry['level']
        return '\u2014'

    # Get the TWO most recent exams for deviation calculation
    # term field is "Term X" — extract the number with Substr for proper numeric sort
    recent_exams = Exam.all_objects.filter(
        school=school, is_deleted=False
    ).annotate(
        term_num=Cast(Substr('term', 6), output_field=IntegerField()),
        name_order=exam_order,
    ).order_by('-year', '-term_num', '-name_order')[:2]
    latest_exam = recent_exams[0] if recent_exams else None
    previous_exam = recent_exams[1] if len(recent_exams) > 1 else None

    exam_label = ''
    if latest_exam:
        exam_label = f"{latest_exam.name.upper()} - ({latest_exam.year} TERM {latest_exam.term})"

    # Helper: compute per-grade stats for a given exam
    def _compute_grade_stats(exam):
        if not exam:
            return {}
        exam_mark_filter = Q(pk__in=[])
        published_subs = submission_qs.filter(
            exam_name=exam.name, term=exam.term, year=exam.year, status="published",
        )
        tuples = set()
        for sub in published_subs:
            tuples.add((sub.class_name, sub.stream, sub.subject_id))
        if tuples:
            f = Q()
            for cls, strm, sid in tuples:
                f |= Q(student__class_name=cls, student__stream=strm, subject_id=sid)
            exam_mark_filter = f
        marks = mark_qs.filter(exam_mark_filter)
        stats = {}
        for item in marks.values('student__class_name').annotate(
            avg_score=Avg('score'), avg_points=Avg('points'), student_count=Count('student', distinct=True)
        ):
            g = item['student__class_name']
            avg_sc = round(item['avg_score'] or 0, 2)
            avg_pts = round(item['avg_points'] or 0, 4)
            sk = _section_key_for_grade(g)
            stats[g] = {
                'mean_score': avg_sc,
                'mean_points': avg_pts,
                'mean_grade': _mean_grade_from_score(avg_sc, sk),
                'student_count': item['student_count'],
            }
        return stats

    current_stats = _compute_grade_stats(latest_exam)
    previous_stats = _compute_grade_stats(previous_exam)

    # Build performance cards list
    grade_performance_cards = []
    for g in grade_choices:
        sk = _section_key_for_grade(g)
        colors = SECTION_COLORS[sk]
        cur = current_stats.get(g, {})
        prev = previous_stats.get(g, {})

        mean_score = cur.get('mean_score', 0)
        mean_points = cur.get('mean_points', 0)
        mean_grade = cur.get('mean_grade', '\u2014')
        student_count = cur.get('student_count', 0)

        # Deviation from previous exam
        score_dev = round(mean_score - prev.get('mean_score', mean_score), 2) if prev else None
        points_dev = round(mean_points - prev.get('mean_points', mean_points), 4) if prev else None

        grade_performance_cards.append({
            'label': g,
            'section_key': sk,
            'accent': colors['accent'],
            'bg': colors['bg'],
            'border': colors['border'],
            'text': colors['text'],
            'mean_score': mean_score,
            'mean_points': mean_points,
            'mean_grade': mean_grade,
            'student_count': student_count,
            'score_dev': score_dev,
            'points_dev': points_dev,
        })

    overall_average = round(
        mark_qs.filter(
            Q(pk__in=[]) if not latest_exam else Q(
                student__class_name__in=grade_choices,
            )
        ).aggregate(avg_score=Avg('score'))['avg_score'] or 0, 1
    ) if latest_exam else 0

    # Recalculate overall from actual published marks
    if latest_exam:
        published_tuples_all = set()
        for sub in submission_qs.filter(exam_name=latest_exam.name, term=latest_exam.term, year=latest_exam.year, status="published"):
            published_tuples_all.add((sub.class_name, sub.stream, sub.subject_id))
        if published_tuples_all:
            f = Q()
            for cls, strm, sid in published_tuples_all:
                f |= Q(student__class_name=cls, student__stream=strm, subject_id=sid)
            overall_average = round(mark_qs.filter(f).aggregate(avg_score=Avg('score'))['avg_score'] or 0, 1)

    best_stream_data = None
    if latest_exam:
        published_tuples_all = set()
        for sub in submission_qs.filter(exam_name=latest_exam.name, term=latest_exam.term, year=latest_exam.year, status="published"):
            published_tuples_all.add((sub.class_name, sub.stream, sub.subject_id))
        if published_tuples_all:
            f = Q()
            for cls, strm, sid in published_tuples_all:
                f |= Q(student__class_name=cls, student__stream=strm, subject_id=sid)
            best_stream_data = (
                mark_qs.filter(f)
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
        'grade_performance_cards': grade_performance_cards,
        'exam_label':           exam_label,
        'grade_performance_rows': [],  # deprecated, kept for compatibility
        'overall_average':      overall_average,
        'best_stream':          best_stream,
        'admin_override_enabled': user_has_main_school_admin_override(request.user),
        'boys_count':           boys_count,
        'girls_count':          girls_count,
        'active_classes':       active_classes,
        'is_primary':           True,
        'section_label':        'All Sections',
    })