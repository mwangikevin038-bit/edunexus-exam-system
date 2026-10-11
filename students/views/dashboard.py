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
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .constants import LOWER_PRIMARY_GRADE_CHOICES, PRIMARY_GRADE_CHOICES, JSS_GRADE_CHOICES
from .helpers import (
    discard_media_file,
    get_class_teacher_scope,
    get_performance_level,
    get_published_contexts_for_user,
    get_teacher_for_user,
    polish_signature_upload,
    validate_signature_upload,
)
from ..security import get_request_school, get_request_school_section, school_admin_required, user_has_main_school_admin_override
from ..models import (
    Event,
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


# ── Grade performance card helpers (shared by teacher & admin dashboards) ────

def _exam_name_rank(name):
    """Logical order within a term: End of Term > Mid Term > Opener."""
    n = (name or '').lower()
    if 'end of term' in n:
        return 3
    if 'mid term' in n:
        return 2
    if 'opener' in n:
        return 1
    return 0


def _term_number(term):
    try:
        return int(str(term).replace('Term', '').strip() or 0)
    except ValueError:
        return 0


def _exam_sort_key(key):
    """Sort key placing the most recent exam first (year, term, name order)."""
    name, term, year = key
    return (-int(year or 0), -_term_number(term), -_exam_name_rank(name), name or '')


def _latest_exam_keys_by_grade(submission_qs, grades):
    """{grade: [newest_key, prev_key]} built from PUBLISHED submissions only.

    key = (exam_name, term, year). Grades with nothing published get no entry.
    """
    per_grade = {}
    rows = submission_qs.filter(status='published', class_name__in=grades) \
        .values_list('class_name', 'exam_name', 'term', 'year').distinct()
    for cls, name, term, year in rows:
        per_grade.setdefault(cls, set()).add((name, term, year))
    return {
        g: sorted(keys, key=_exam_sort_key)[:2]
        for g, keys in per_grade.items()
    }


def _published_tuples_by_exam(submission_qs, grades, exam_keys):
    """{exam_key: {(class, stream, subject_id)}} — exact published tuples."""
    tuples_by_exam = {k: set() for k in exam_keys}
    if not exam_keys:
        return tuples_by_exam
    q = Q()
    for name, term, year in exam_keys:
        q |= Q(exam_name=name, term=term, year=year)
    for sub in submission_qs.filter(q, status='published', class_name__in=grades):
        key = (sub.exam_name, sub.term, sub.year)
        if key in tuples_by_exam:
            tuples_by_exam[key].add((sub.class_name, sub.stream, sub.subject_id))
    return tuples_by_exam


def _published_exact_q(tuples, include_previous_stream=False):
    """Q matching ONLY the exact (class, stream, subject) published tuples.

    Streams can be merged or split between exams, so students' current stream
    may no longer match the stream their results were published under. When a
    class has published tuples but yields no stats, the caller retries with
    include_previous_stream=True to also match the previous_stream saved when
    students were re-streamed.
    """
    q = Q()
    for cls, strm, sid in tuples:
        stream_q = Q(student__stream=strm)
        if include_previous_stream:
            stream_q |= Q(student__previous_stream=strm)
        q |= Q(student__class_name=cls, subject_id=sid) & stream_q
    return q


def _stats_for_key(mark_qs, key, tuples, include_previous_stream=False):
    """{(exam_key, grade): stats} for one exam key over its published tuples."""
    result = {}
    qs = mark_qs.filter(
        _published_exact_q(tuples, include_previous_stream),
        exam_type=key[0], term=key[1], year=key[2],
        is_absent=False, student__is_active=True,
    )
    for item in qs.values('student__class_name').annotate(
        avg_score=Avg('score'), avg_points=Avg('points'),
        student_count=Count('student', distinct=True),
    ):
        result[(key, item['student__class_name'])] = {
            'mean_score': round(item['avg_score'] or 0, 2),
            'mean_points': round(item['avg_points'] or 0, 4),
            'student_count': item['student_count'],
        }
    return result


def _stats_by_exam_and_grade(mark_qs, tuples_by_exam):
    """{(exam_key, grade): {mean_score, mean_points, student_count}}.

    Exact published tuples only; absent marks and inactive students excluded
    so the means describe the students actually counted. Grades whose students
    were re-streamed between exams (current stream no longer matches the
    published stream) fall back to matching saved previous_stream values.
    """
    stats = {}
    for key, tuples in tuples_by_exam.items():
        if not tuples:
            continue
        stats.update(_stats_for_key(mark_qs, key, tuples))
        for cls in {cls for cls, _, _ in tuples}:
            if (key, cls) not in stats:
                cls_tuples = [t for t in tuples if t[0] == cls]
                stats.update(_stats_for_key(
                    mark_qs, key, cls_tuples, include_previous_stream=True,
                ))
    return stats


def _mean_grade_for(avg_score, section_key, school):
    """Grade letter for a mean score using the school's configured GradingScale,
    falling back to the built-in default scale when no config resolves."""
    level = None
    if school is not None:
        try:
            level, _pts = get_performance_level(avg_score, section=section_key, school=school)
        except (KeyError, TypeError, ValueError):
            level = None
    # '-' / 'NO CONFIG' mean the resolver found no scale — fall through to the
    # built-in default scale instead of showing an unhelpful placeholder.
    if level and level not in ('NO CONFIG', '-'):
        return level
    for entry in GradingConfig.get_default_subject_scale(section_key):
        if entry['min_score'] <= avg_score < entry['max_score'] + 1:
            return entry['level']
    return '\u2014'


def _exam_label_for(key):
    if not key:
        return ''
    name, term, year = key
    term_num = str(term).replace('Term', '').strip() or str(term)
    return f"{(name or '').upper()} - ({year} TERM {term_num})"


def _exam_id_for(school, key):
    """Exam row id for a key; prefers the active row among duplicates."""
    if not key or not school:
        return None
    name, term, year = key
    return (
        Exam.all_objects.filter(
            school=school, name=name, term=term, year=year, is_deleted=False,
        )
        .order_by('status')
        .values_list('id', flat=True)
        .first()
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

            # ── Signature: class teachers only, validated server-side ────────
            # The model validators do not run on a plain save(), so every
            # upload is checked here (type + size) before being accepted.
            is_class_teacher = bool(get_class_teacher_scope(teacher))
            sig_file = request.FILES.get('signature')
            want_delete_sig = request.POST.get('delete_signature') == '1'
            signature_cleanup = None
            if (sig_file or want_delete_sig) and not is_class_teacher:
                messages.warning(request, 'Signatures are only available to class teachers.')
            elif sig_file:
                sig_error = validate_signature_upload(sig_file)
                if sig_error:
                    messages.error(request, sig_error)
                else:
                    signature_cleanup = teacher.signature.name if teacher.signature else None
                    teacher.signature = polish_signature_upload(sig_file, teacher.pk)
            elif want_delete_sig:
                signature_cleanup = teacher.signature.name if teacher.signature else None
                teacher.signature = None

            teacher.save()
            if signature_cleanup and (
                not teacher.signature or teacher.signature.name != signature_cleanup
            ):
                discard_media_file(signature_cleanup)

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
        assignments = SubjectAssignment.objects.filter(teacher_profile=teacher, is_active=True).order_by(
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

    assignments_qs = SubjectAssignment.objects.filter(school=school, teacher_profile=teacher, is_active=True).order_by(
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

    # --- School events for calendar ---
    school_events_qs = Event.objects.filter(school=school).order_by('event_date', 'time') if school else Event.objects.none()
    school_events = []
    for ev in school_events_qs:
        school_events.append({
            'id': ev.id,
            'name': ev.name,
            'date_mode': ev.date_mode,
            'start': ev.event_date.isoformat(),
            'end': ev.end_date.isoformat() if ev.end_date else ev.event_date.isoformat(),
            'time': ev.time.strftime('%H:%M') if ev.time else '',
            'participants': ev.participants,
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

    submission_qs = MarkSubmission.all_objects.filter(school=school) if school else MarkSubmission.objects.none()
    mark_qs = Mark.all_objects.filter(school=school) if school else Mark.objects.none()

    # Per-grade latest two exams — chronological, from PUBLISHED submissions only.
    grade_exam_keys = _latest_exam_keys_by_grade(submission_qs, section_grades)
    needed_keys = sorted(
        {k for keys in grade_exam_keys.values() for k in keys}, key=_exam_sort_key,
    )
    tuples_by_exam = _published_tuples_by_exam(submission_qs, section_grades, needed_keys)
    stats_by_key = _stats_by_exam_and_grade(mark_qs, tuples_by_exam)
    newest_key = min(
        (keys[0] for keys in grade_exam_keys.values() if keys),
        key=_exam_sort_key, default=None,
    )
    exam_label = _exam_label_for(newest_key)

    grade_performance_cards = []
    if newest_key:
        for g in section_grades:
            sk = _section_key_for_grade(g)
            colors = SECTION_COLORS[sk]
            keys = grade_exam_keys.get(g) or []
            cur_key = keys[0] if keys else None
            prev_key = keys[1] if len(keys) > 1 else None
            cur = stats_by_key.get((cur_key, g), {}) if cur_key else {}
            prev = stats_by_key.get((prev_key, g), {}) if prev_key else {}
            mean_score = cur.get('mean_score', 0)
            mean_points = cur.get('mean_points', 0)
            student_count = cur.get('student_count', 0)
            mean_grade = _mean_grade_for(mean_score, sk, school) if student_count else '\u2014'
            score_dev = round(mean_score - prev.get('mean_score', mean_score), 2) if (prev and student_count) else None
            points_dev = round(mean_points - prev.get('mean_points', mean_points), 4) if (prev and student_count) else None
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
                'exam_label': _exam_label_for(cur_key),
                'exam_id': _exam_id_for(school, cur_key) if cur_key else None,
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
        'school_events_json': _json.dumps(school_events),
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
    assignment_qs = SubjectAssignment.all_objects.filter(school=school, is_active=True)
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
    from django.db.models import Count as CountAgg
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
            tp = assignment.teacher_profile
            if not tp:
                continue
            missing_entries_feed.append({
                'teacher_name': tp.get_full_title(),
                'subject_name': assignment.subject.name if assignment.subject else '—',
                'target_class': f"{assignment.class_name} {assignment.stream}",
                'phone':        tp.phone_number,
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

    # Per-grade latest two exams — chronological, from PUBLISHED submissions only.
    grade_exam_keys = _latest_exam_keys_by_grade(submission_qs, grade_choices)
    needed_keys = sorted(
        {k for keys in grade_exam_keys.values() for k in keys}, key=_exam_sort_key,
    )
    tuples_by_exam = _published_tuples_by_exam(submission_qs, grade_choices, needed_keys)
    stats_by_key = _stats_by_exam_and_grade(mark_qs, tuples_by_exam)
    newest_key = min(
        (keys[0] for keys in grade_exam_keys.values() if keys),
        key=_exam_sort_key, default=None,
    )
    exam_label = _exam_label_for(newest_key)

    # Build performance cards list
    grade_performance_cards = []
    if newest_key:
        for g in grade_choices:
            sk = _section_key_for_grade(g)
            colors = SECTION_COLORS[sk]
            keys = grade_exam_keys.get(g) or []
            cur_key = keys[0] if keys else None
            prev_key = keys[1] if len(keys) > 1 else None
            cur = stats_by_key.get((cur_key, g), {}) if cur_key else {}
            prev = stats_by_key.get((prev_key, g), {}) if prev_key else {}

            mean_score = cur.get('mean_score', 0)
            mean_points = cur.get('mean_points', 0)
            student_count = cur.get('student_count', 0)
            mean_grade = _mean_grade_for(mean_score, sk, school) if student_count else '\u2014'

            # Deviation from the grade's previous exam (hidden without current data)
            score_dev = round(mean_score - prev.get('mean_score', mean_score), 2) if (prev and student_count) else None
            points_dev = round(mean_points - prev.get('mean_points', mean_points), 4) if (prev and student_count) else None

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
                'exam_label': _exam_label_for(cur_key),
                'exam_id': _exam_id_for(school, cur_key) if cur_key else None,
            })

    overall_average = 0
    best_stream_data = None

    # Overall KPIs for the newest published exam — same rules as the cards:
    # exact published tuples, absent marks and inactive students excluded.
    newest_tuples = (tuples_by_exam.get(newest_key) or set()) if newest_key else set()
    if newest_tuples:
        base = mark_qs.filter(
            _published_exact_q(newest_tuples),
            exam_type=newest_key[0], term=newest_key[1], year=newest_key[2],
            is_absent=False, student__is_active=True,
        )
        overall_average = round(base.aggregate(avg_score=Avg('score'))['avg_score'] or 0, 1)
        best_stream_data = (
            base.values('student__class_name', 'student__stream')
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

    # --- School events for calendar ---
    from ..models import Event
    school_events_qs = Event.objects.filter(school=school).order_by('event_date', 'time')
    school_events = []
    for ev in school_events_qs:
        school_events.append({
            'id': ev.id,
            'name': ev.name,
            'date_mode': ev.date_mode,
            'start': ev.event_date.isoformat(),
            'end': ev.end_date.isoformat() if ev.end_date else ev.event_date.isoformat(),
            'time': ev.time.strftime('%H:%M') if ev.time else '',
            'participants': ev.participants,
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
        'school_events_json':   _json.dumps(school_events),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Event CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@login_required(login_url='login')
@school_admin_required
def add_event(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    school = get_request_school(request)
    if not school:
        return JsonResponse({'ok': False, 'error': 'No school'}, status=400)

    name = (request.POST.get('name') or '').strip()
    date_mode = (request.POST.get('date_mode') or 'single').strip()
    event_date_str = (request.POST.get('event_date') or '').strip()
    end_date_str = (request.POST.get('end_date') or '').strip()
    time_str = (request.POST.get('time') or '').strip()
    participants = (request.POST.get('participants') or 'All').strip()

    if not name:
        return JsonResponse({'ok': False, 'error': 'Event name is required'}, status=400)
    if not event_date_str:
        return JsonResponse({'ok': False, 'error': 'Event date is required'}, status=400)

    try:
        from datetime import datetime as _dt
        event_date = _dt.strptime(event_date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)

    end_date = None
    if date_mode == 'range' and end_date_str:
        try:
            end_date = _dt.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Invalid end date format'}, status=400)

    event_time = None
    if time_str:
        try:
            event_time = _dt.strptime(time_str, '%H:%M').time()
        except ValueError:
            return JsonResponse({'ok': False, 'error': 'Invalid time format (use HH:MM)'}, status=400)

    from ..models import Event
    ev = Event.objects.create(
        school=school,
        name=name,
        date_mode=date_mode,
        event_date=event_date,
        end_date=end_date,
        time=event_time,
        participants=participants,
        created_by=request.user,
    )

    return JsonResponse({
        'ok': True,
        'event': {
            'id': ev.id,
            'name': ev.name,
            'date_mode': ev.date_mode,
            'start': ev.event_date.isoformat(),
            'end': ev.end_date.isoformat() if ev.end_date else ev.event_date.isoformat(),
            'time': ev.time.strftime('%H:%M') if ev.time else '',
            'participants': ev.participants,
        }
    })


@login_required(login_url='login')
@school_admin_required
def delete_event(request, event_id):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)

    school = get_request_school(request)
    if not school:
        return JsonResponse({'ok': False, 'error': 'No school'}, status=400)

    from ..models import Event
    try:
        ev = Event.objects.get(id=event_id, school=school)
    except Event.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'Event not found'}, status=404)

    ev.delete()
    return JsonResponse({'ok': True})