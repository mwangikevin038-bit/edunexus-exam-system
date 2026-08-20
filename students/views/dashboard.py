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

from .constants import GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, PRIMARY_GRADE_CHOICES, JSS_GRADE_CHOICES
from .helpers import get_teacher_for_user, get_class_teacher_scope, get_published_contexts_for_user
from ..security import get_request_school, get_request_school_section, school_admin_required, user_has_main_school_admin_override
from ..models import (
    Exam,
    GradingConfig,
    Mark,
    MarkSubmission,
    School,
    Student,
    SubjectAssignment,
    Teacher,
    TermDate,
)


@login_required(login_url='login')
def profile_view(request):
    """
    User profile page with editable fields.
    Handles both Teacher and SchoolAdmin users.
    """
    user = request.user
    try:
        teacher = Teacher.objects.select_related('user').get(user=user)
    except Teacher.DoesNotExist:
        teacher = None

    if request.method == 'POST':
        user.first_name = request.POST.get('first_name', user.first_name).strip()
        user.last_name = request.POST.get('surname', user.last_name).strip()
        user.save()

        if teacher:
            teacher.other_names = request.POST.get('other_names', teacher.other_names).strip()
            teacher.phone_number = request.POST.get('phone_number', teacher.phone_number).strip()
            teacher.email = request.POST.get('personal_email', teacher.email).strip()
            teacher.gender = request.POST.get('gender', teacher.gender)
            teacher.national_id = request.POST.get('national_id', teacher.national_id).strip()
            teacher.bio = request.POST.get('bio', teacher.bio).strip()

            if request.FILES.get('profile_picture'):
                teacher.profile_picture = request.FILES['profile_picture']
            elif request.POST.get('delete_profile_picture') == '1':
                teacher.profile_picture = None

            if request.FILES.get('signature'):
                teacher.signature = request.FILES['signature']
            elif request.POST.get('delete_signature') == '1':
                teacher.signature = None

            teacher.save()

        messages.success(request, 'Profile updated successfully.')
        return redirect('home_alt')

    assignments = SubjectAssignment.objects.none()
    submissions = MarkSubmission.objects.none()
    class_scope = None
    assignment_count = 0
    submitted_count = 0
    returned_count = 0
    published_count = 0

    if teacher:
        assignments = SubjectAssignment.objects.filter(teacher_profile=teacher).order_by(
            'class_name', 'stream', 'subject__code'
        )
        section = get_request_school_section(request)
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
        class_scope = get_class_teacher_scope(teacher)
        assignment_count = assignments.count()
        submitted_count = submissions.filter(status__in=['submitted', 'approved', 'published']).count()
        returned_count = submissions.filter(status='returned').count()
        published_count = submissions.filter(status='published').count()

    return render(request, 'students/profile.html', {
        'user': user,
        'teacher': teacher,
        'assignments': assignments,
        'assignment_count': assignment_count,
        'submitted_count': submitted_count,
        'returned_count': returned_count,
        'published_count': published_count,
        'class_teacher_scope': class_scope,
    })


@login_required(login_url='login')
@school_admin_required
def school_settings(request):
    """School admin: edit school profile (name, logo, contact, etc.)."""
    school = get_request_school(request)
    if not school:
        messages.error(request, "No school found.")
        return redirect('school_admin_dashboard')

    if request.method == 'POST':
        school.name = request.POST.get('name', school.name).strip()
        school.short_name = request.POST.get('short_name', school.short_name or '').strip()
        school.phone_number = request.POST.get('phone_number', school.phone_number or '').strip()
        school.email = request.POST.get('email', school.email or '').strip()
        school.address = request.POST.get('address', school.address or '').strip()
        school.gender_type = request.POST.get('gender_type', school.gender_type)
        school.boarding_status = request.POST.get('boarding_status', school.boarding_status)
        school.motto = request.POST.get('motto', school.motto or '').strip()
        school.vision = request.POST.get('vision', school.vision or '').strip()
        school.mission = request.POST.get('mission', school.mission or '').strip()

        if request.FILES.get('logo'):
            school.logo = request.FILES['logo']

        school.save()
        messages.success(request, "School profile updated successfully.")
        return redirect('school_settings')

    return render(request, 'students/school_settings.html', {
        'school': school,
        'gender_type_choices': School.GENDER_TYPE_CHOICES,
        'boarding_status_choices': School.BOARDING_STATUS_CHOICES,
    })


@login_required(login_url='login')
@school_admin_required
def term_dates(request):
    """School admin: manage term dates (CRUD)."""
    school = get_request_school(request)
    if not school:
        messages.error(request, "No school found.")
        return redirect('school_admin_dashboard')

    from ..models import TermDate
    from datetime import date

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'delete':
            delete_id = request.POST.get('delete_id')
            if delete_id:
                TermDate.objects.filter(id=delete_id, school=school).delete()
                messages.success(request, "Term date deleted successfully.")
            return redirect('term_dates')

        term_id = request.POST.get('term_id')
        academic_year = request.POST.get('academic_year', '').strip()
        term_name = request.POST.get('term', '').strip()
        start_date = request.POST.get('start_date', '').strip()
        end_date = request.POST.get('end_date', '').strip()
        week_starts_on = request.POST.get('week_starts_on') or 'Monday'

        if not all([academic_year, term_name, start_date, end_date]):
            messages.error(request, "All fields are required.")
            return redirect('term_dates')

        try:
            academic_year = int(academic_year)
            start_dt = date.fromisoformat(start_date)
            end_dt = date.fromisoformat(end_date)
        except (ValueError, TypeError):
            messages.error(request, "Invalid date or year format.")
            return redirect('term_dates')

        if end_dt <= start_dt:
            messages.error(request, "End date must be after start date.")
            return redirect('term_dates')

        if term_id:
            td = TermDate.objects.filter(id=term_id, school=school).first()
            if td:
                td.academic_year = academic_year
                td.term = term_name
                td.start_date = start_dt
                td.end_date = end_dt
                td.week_starts_on = week_starts_on
                td.save()
                messages.success(request, "Term date updated successfully.")
        else:
            if TermDate.objects.filter(school=school, academic_year=academic_year, term=term_name).exists():
                messages.error(request, f"{term_name} for {academic_year} already exists.")
                return redirect('term_dates')
            TermDate.objects.create(
                school=school,
                academic_year=academic_year,
                term=term_name,
                start_date=start_dt,
                end_date=end_dt,
                week_starts_on=week_starts_on,
            )
            messages.success(request, "Term date created successfully.")

        return redirect('term_dates')

    term_dates_qs = TermDate.objects.filter(school=school).order_by('-academic_year', 'term')
    current_year = date.today().year
    year_choices = list(range(current_year - 2, current_year + 3))

    return render(request, 'students/term_dates.html', {
        'term_dates': term_dates_qs,
        'year_choices': year_choices,
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
    submissions = MarkSubmission.objects.filter(school=school, teacher=teacher).select_related('subject') if teacher and school else MarkSubmission.objects.none()
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

    # --- Term dates for calendar ---
    import json as _json
    term_dates_qs = TermDate.objects.filter(school=school).order_by('-academic_year', 'term') if school else TermDate.objects.none()
    term_events = []
    for td in term_dates_qs:
        term_events.append({
            'term': td.term,
            'year': td.academic_year,
            'start': td.start_date.isoformat(),
            'end': td.end_date.isoformat(),
        })

    # --- Section-scoped grade choices ---
    if section == 'JSS':
        section_grades = JSS_GRADE_CHOICES
    elif section == 'PRIMARY':
        section_grades = PRIMARY_GRADE_CHOICES
    elif section == 'LOWER_PRIMARY':
        section_grades = LOWER_PRIMARY_GRADE_CHOICES
    else:
        section_grades = LOWER_PRIMARY_GRADE_CHOICES + PRIMARY_GRADE_CHOICES + JSS_GRADE_CHOICES

    # --- Grade performance cards (section-scoped) ---
    SECTION_COLORS = {
        'LOWER_PRIMARY': {'accent': '#B45309', 'bg': '#FFFBEB', 'border': '#fde68a', 'text': '#92400e'},
        'PRIMARY':       {'accent': '#00674F', 'bg': '#ECFDF5', 'border': '#a7f3d0', 'text': '#065f46'},
        'JSS':           {'accent': '#305CDE', 'bg': '#EFF6FF', 'border': '#bfdbfe', 'text': '#1e40af'},
    }

    def _section_key_for_grade(g):
        if g in LOWER_PRIMARY_GRADE_CHOICES:
            return 'LOWER_PRIMARY'
        if g in PRIMARY_GRADE_CHOICES:
            return 'PRIMARY'
        return 'JSS'

    def _mean_grade_from_score(avg_score, section_key):
        scale = GradingConfig.get_default_subject_scale(section_key)
        for entry in scale:
            if entry['min_score'] <= avg_score < entry['max_score'] + 1:
                return entry['level']
        return '\u2014'

    exam_order = Case(
        When(name__icontains='end of term', then=Value(3)),
        When(name__icontains='mid term', then=Value(2)),
        When(name__icontains='opener', then=Value(1)),
        default=Value(0),
        output_field=IntegerField(),
    )

    recent_exams_raw = Exam.all_objects.filter(
        school=school, is_deleted=False
    ).annotate(
        term_num=Cast(Substr('term', 6), output_field=IntegerField()),
        name_order=exam_order,
    ).order_by('-year', '-term_num', '-name_order') if school else Exam.objects.none()

    # Find the two most recent exams with published marks in this section
    section_exam_keys = set(
        Mark.all_objects.filter(
            student__school=school,
            student__class_name__in=section_grades,
        ).values_list('exam_type', 'term', 'year').distinct()
    ) if school and section_grades else set()

    eot_exam = None
    opener_exam = None
    seen_exams = set()
    for ex in recent_exams_raw:
        key = (ex.name, ex.term, ex.year)
        if key in seen_exams:
            continue
        seen_exams.add(key)
        if key not in section_exam_keys:
            continue
        if 'end of term' in ex.name.lower() and not eot_exam:
            eot_exam = ex
        elif 'opener' in ex.name.lower() and not opener_exam:
            opener_exam = ex
        if eot_exam and opener_exam:
            break

    latest_exam = eot_exam or opener_exam
    previous_exam = opener_exam if latest_exam == eot_exam else eot_exam

    exam_label = ''
    if latest_exam:
        exam_label = f"{latest_exam.name.upper()} - ({latest_exam.year} TERM {latest_exam.term})"

    submission_qs = MarkSubmission.all_objects.filter(school=school) if school else MarkSubmission.objects.none()
    mark_qs = Mark.all_objects.filter(school=school) if school else Mark.objects.none()

    def _compute_grade_stats(exam):
        if not exam:
            return {}
        published_subs = submission_qs.filter(
            exam_name=exam.name, term=exam.term, year=exam.year, status="published",
        )
        tuples = set()
        for sub in published_subs:
            if sub.class_name in section_grades:
                tuples.add((sub.class_name, sub.stream, sub.subject_id))
        if tuples:
            class_names = [cls for cls, strm, sid in tuples]
            streams = [strm for cls, strm, sid in tuples]
            subject_ids = [sid for cls, strm, sid in tuples]
            exam_mark_filter = (
                Q(student__class_name__in=class_names) &
                Q(student__stream__in=streams) &
                Q(subject_id__in=subject_ids) &
                Q(exam_type=exam.name, term=exam.term, year=exam.year)
            )
        else:
            exam_mark_filter = Q(
                student__school=school, exam_type=exam.name, term=exam.term, year=exam.year,
                student__class_name__in=section_grades,
            )
        marks = mark_qs.filter(exam_mark_filter)
        stats = {}
        for item in marks.values('student__class_name').annotate(
            avg_score=Avg('score'), avg_points=Avg('points'), student_count=Count('student', distinct=True)
        ):
            g = item['student__class_name']
            if g not in section_grades:
                continue
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

    grade_performance_cards = []
    for g in section_grades:
        sk = _section_key_for_grade(g)
        colors = SECTION_COLORS[sk]
        cur = current_stats.get(g, {})
        prev = previous_stats.get(g, {})
        mean_score = cur.get('mean_score', 0)
        mean_points = cur.get('mean_points', 0)
        mean_grade = cur.get('mean_grade', '\u2014')
        student_count = cur.get('student_count', 0)
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
            'exam_id': latest_exam.id if latest_exam else None,
        })

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
        'term_events_json': _json.dumps(term_events),
        'grade_performance_cards': grade_performance_cards,
        'exam_label': exam_label,
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

    # --- Headline counts (single aggregate query) ---
    from django.db.models import Count as CountAgg, Q as CountQ
    counts = Student.all_objects.filter(school=school).aggregate(
        total_students=CountAgg('id'),
    )
    total_students = counts['total_students']
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

    # Get the TWO most recent DISTINCT exams with marks for deviation calculation
    # For JSS: compare End of Term vs Opener (the two exams with complete marks)
    jss_grades = ['Grade 7', 'Grade 8', 'Grade 9']
    recent_exams_raw = Exam.all_objects.filter(
        school=school, is_deleted=False
    ).annotate(
        term_num=Cast(Substr('term', 6), output_field=IntegerField()),
        name_order=exam_order,
    ).order_by('-year', '-term_num', '-name_order')

    # For JSS, find End of Term and Opener specifically
    eot_exam = None
    opener_exam = None
    seen_exams = set()

    # Batch-fetch all JSS exam keys in ONE query instead of N
    jss_exam_keys = set(
        Mark.all_objects.filter(
            student__school=school,
            student__class_name__in=jss_grades,
        ).values_list('exam_type', 'term', 'year').distinct()
    )

    for ex in recent_exams_raw:
        key = (ex.name, ex.term, ex.year)
        if key in seen_exams:
            continue
        seen_exams.add(key)
        if (ex.name, ex.term, ex.year) not in jss_exam_keys:
            continue
        if 'end of term' in ex.name.lower() and not eot_exam:
            eot_exam = ex
        elif 'opener' in ex.name.lower() and not opener_exam:
            opener_exam = ex
        if eot_exam and opener_exam:
            break

    latest_exam = eot_exam or opener_exam
    previous_exam = opener_exam if latest_exam == eot_exam else eot_exam

    exam_label = ''
    if latest_exam:
        exam_label = f"{latest_exam.name.upper()} - ({latest_exam.year} TERM {latest_exam.term})"

    # Helper: compute per-grade stats for a given exam
    def _compute_grade_stats(exam):
        if not exam:
            return {}
        published_subs = submission_qs.filter(
            exam_name=exam.name, term=exam.term, year=exam.year, status="published",
        )
        tuples = set()
        for sub in published_subs:
            tuples.add((sub.class_name, sub.stream, sub.subject_id))
        if tuples:
            class_names = [cls for cls, strm, sid in tuples]
            streams = [strm for cls, strm, sid in tuples]
            subject_ids = [sid for cls, strm, sid in tuples]
            exam_mark_filter = (
                Q(student__class_name__in=class_names) &
                Q(student__stream__in=streams) &
                Q(subject_id__in=subject_ids) &
                Q(exam_type=exam.name, term=exam.term, year=exam.year)
            )
        else:
            # No submissions — fall back to direct marks lookup
            exam_mark_filter = Q(
                student__school=school, exam_type=exam.name, term=exam.term, year=exam.year,
            )
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
            'exam_id': latest_exam.id if latest_exam else None,
        })

    overall_average = 0
    best_stream_data = None

    # Compute published tuples ONCE and reuse for overall_average + best_stream
    if latest_exam:
        published_tuples_all = set()
        for sub in submission_qs.filter(exam_name=latest_exam.name, term=latest_exam.term, year=latest_exam.year, status="published"):
            published_tuples_all.add((sub.class_name, sub.stream, sub.subject_id))
        if published_tuples_all:
            f = Q()
            for cls, strm, sid in published_tuples_all:
                f |= Q(student__class_name=cls, student__stream=strm, subject_id=sid)
            overall_average = round(mark_qs.filter(f).aggregate(avg_score=Avg('score'))['avg_score'] or 0, 1)
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

    # --- Term dates for calendar ---
    from ..models import TermDate
    import json as _json
    term_dates_qs = TermDate.objects.filter(school=school).order_by('-academic_year', 'term')
    term_events = []
    for td in term_dates_qs:
        term_events.append({
            'term': td.term,
            'year': td.academic_year,
            'start': td.start_date.isoformat(),
            'end': td.end_date.isoformat(),
        })

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
        'term_events_json':     _json.dumps(term_events),
    })