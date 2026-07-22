"""
Results and report card views for the EduNexus student management system.

Provides the official published-results workspace, report card selection,
individual student report card rendering, and bulk report card generation.
"""

import datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Prefetch, Sum
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .constants import (
    ASSESSMENT_MAP,
    GRADE_CHOICES,
    LOWER_PRIMARY_GRADE_CHOICES,
    LOWER_PRIMARY_SUBJECT_NAMES,
    LOWER_PRIMARY_SUBJECT_SHORT_MAP,
    ORDERED_LEVELS,
    PRIMARY_PERF_LEVELS,
    PRIMARY_SUBJECT_NAMES,
    PRIMARY_SUBJECT_SHORT_MAP,
    SUBJECT_SHORT_MAP,
    TERM_CHOICES,
    get_streams_for_school,
    sort_subjects,
)
from .exams import PRIMARY_GRADE_CHOICES, _get_primary_performance
from .helpers import (
    calculate_broadsheet_plv,
    calculate_primary_plv,
    calculate_report_plv,
    get_class_teacher_scope,
    get_performance_level,
    get_published_contexts_for_user,
    get_published_subject_codes,
    get_selected_context,
    get_students_ordered,
    get_teacher_for_user,
    user_can_access_class_stream,
)
from ..models import (
    ClassTeacherMasterComment,
    GradingConfig,
    Mark,
    SchoolHeadteacherComment,
    Student,
    SubjectAssignment,
)
from ..security import (
    get_request_school,
    get_request_school_section,
    get_school_object_or_403,
    rate_limit,
    user_has_main_school_admin_override,
)


PRIMARY_ORDERED_LEVELS = ['EE', 'ME', 'AE', 'BE']


# ==============================================================================
# SECTION 7 — RESULTS & REPORT VIEWS
# ==============================================================================

@login_required(login_url='login')
@never_cache
def results_list(request):
    """
    Official published-results workspace.
    All authenticated teachers get read-only access to compiled results lists.
    Individual report cards remain class-teacher/admin scoped in their own views.
    Workspace-aware: uses Primary grades/template when in Primary workspace.
    """
    is_admin_view = user_has_main_school_admin_override(request.user)
    class_teacher_scope = get_class_teacher_scope(get_teacher_for_user(request.user))

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Determine workspace section for grade choices and template
    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary
    if is_lower_primary:
        grade_choices = LOWER_PRIMARY_GRADE_CHOICES
    elif is_primary:
        grade_choices = PRIMARY_GRADE_CHOICES
    else:
        grade_choices = GRADE_CHOICES

    published_contexts = get_published_contexts_for_user(request.user)
    selected_context = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    year = str(selected_context["year"]) if selected_context else None
    term = selected_context["term"] if selected_context else None
    grade = selected_context["class_name"] if selected_context else None
    stream = selected_context["stream"] if selected_context else None
    exam_type = selected_context["exam_name"] if selected_context else None
    selected_context_key = selected_context["context_key"] if selected_context else ""

    # Use the correct subject map for the workspace section
    if is_lower_primary:
        subject_map = LOWER_PRIMARY_SUBJECT_SHORT_MAP
    elif is_primary:
        subject_map = PRIMARY_SUBJECT_SHORT_MAP
    else:
        subject_map = SUBJECT_SHORT_MAP
    subject_codes = list(subject_map.keys())
    active_levels = PRIMARY_PERF_LEVELS if is_primary else ORDERED_LEVELS

    # Initialise per-subject analysis buckets
    analysis_data = {
        short: {
            'entries': 0, 'total_score': 0, 'mean_score': 0.0,
            'distribution': {lvl: 0 for lvl in active_levels},
            'teacher_name': '—',
        }
        for short in subject_map.values()
    }

    show_table = False
    broadsheet = []
    published_subject_count = 0
    student_count = 0
    published_subjects = []

    if year and term and grade and stream and exam_type:
        show_table = True
        published_subject_codes = get_published_subject_codes(grade, stream, year, term, exam_type)
        published_subject_count = len(published_subject_codes)

        # Get Subject objects for published subjects and keep stable display labels.
        from ..models import Subject
        published_subjects_qs = Subject.objects.filter(school=school, code__in=published_subject_codes)
        subject_label_map = {
            s.code: (subject_map.get(s.code) or s.name or s.code)
            for s in published_subjects_qs
        }
        published_subjects = sort_subjects([
            (code, subject_label_map.get(code, subject_map.get(code, code)))
            for code in published_subject_codes
        ])
        for _code, short in published_subjects:
            analysis_data.setdefault(short, {
                'entries': 0, 'total_score': 0, 'mean_score': 0.0,
                'distribution': {lvl: 0 for lvl in active_levels},
                'teacher_name': '—',
            })

        # Map assigned teachers for this grade/stream
        teacher_map = {}
        sa_qs = SubjectAssignment.objects.filter(school=school, class_name=grade, stream=stream).select_related('teacher_profile__user', 'subject')
        if section == 'LOWER_PRIMARY':
            sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section='UPPER')
        elif section == 'JSS':
            sa_qs = sa_qs.filter(school_section='JSS')
        for a in sa_qs:
            code = a.subject.code if a.subject else None
            if code:
                teacher_map[subject_label_map.get(code, subject_map.get(code, code))] = a.teacher_profile.get_full_title()
        for short in analysis_data:
            analysis_data[short]['teacher_name'] = teacher_map.get(short, '—')

        # Prefetch all relevant marks in one query (only published subject codes)
        marks_prefetch = Prefetch(
            'marks',
            queryset=Mark.objects.filter(
                school=school,
                year=year,
                term=term,
                exam_type=exam_type,
                subject__in=published_subjects_qs,
            ).order_by('subject', '-date_recorded', '-id'),
            to_attr='cached_marks',
        )
        students = Student.objects.filter(
            school=school,
            class_name=grade,
            stream=stream,
        ).prefetch_related(marks_prefetch).order_by('admission_no')

        student_count = students.count()

        for student in students:
            marks_dict  = {}
            for mark in student.cached_marks:
                marks_dict.setdefault(mark.subject.code, mark)
            row_scores  = []
            total_marks = 0
            total_points = 0
            assessed_subjects = 0

            for code, short in published_subjects:
                m = marks_dict.get(code)
                if m and m.score is not None:
                    if m.is_absent:
                        row_scores.append({'score': 'AB', 'level': 'AB'})
                    else:
                        level, points = _get_primary_performance(m.score) if is_primary else get_performance_level(m.score)
                        row_scores.append({'score': m.score, 'level': level})
                        total_marks  += m.score
                        total_points += points
                        assessed_subjects += 1
                    if not m.is_absent:
                        analysis_data[short]['entries']     += 1
                        analysis_data[short]['total_score'] += m.score
                        if level in analysis_data[short]['distribution']:
                            analysis_data[short]['distribution'][level] += 1
                else:
                    row_scores.append({'score': '-', 'level': '-'})

            broadsheet.append({
                'student': student,
                'scores':  row_scores,
                'tps':     total_points,
                'total':   total_marks,
                'plv':     calculate_primary_plv(total_marks, assessed_subjects) if is_primary else calculate_broadsheet_plv(total_marks, total_points),
            })

        broadsheet.sort(key=lambda x: (-x['total'], -x['tps']))

        for short, data in analysis_data.items():
            if data['entries'] > 0:
                data['mean_score'] = round(data['total_score'] / data['entries'], 2)

        # Build ordered analysis rows for only published subjects, in display order
        analysis_rows = [
            {'short': short, **analysis_data[short]} for code, short in published_subjects
        ]
    else:
        analysis_rows = []

    # Use Primary template when in Primary workspace
    template = 'students/results_list_primary.html' if is_primary else 'students/results_list.html'

    # Section accent colors for branding header
    section_colors = {
        'JSS':           '#3A6AD8',
        'PRIMARY':       '#047857',
        'LOWER_PRIMARY': '#B45309',
    }
    section_accent = section_colors.get(section, '#3A6AD8')

    return render(request, template, {
        'broadsheet':      broadsheet,
        'analysis_data':   analysis_data,
        'analysis_rows':   analysis_rows,
        'ordered_levels':  active_levels,
        'show_table':      show_table,
        'selected_year':   year,
        'selected_term':   term,
        'selected_exam':   exam_type,
        'selected_grade':  grade,
        'selected_stream': stream,
        'selected_context_key': selected_context_key,
        'published_contexts': published_contexts,
        'published_subjects': published_subjects,
        'published_subject_count': published_subject_count,
        'student_count': student_count,
        'is_admin_view': is_admin_view,
        'is_primary': is_primary,
        'section_accent': section_accent,
        'access_label': "School-wide official results" if is_admin_view else ("Class teacher view" if class_teacher_scope else "Subject teacher view"),
        'years':           list(range(2024, datetime.date.today().year + 1)),
        'terms':           TERM_CHOICES,
        'grades':          grade_choices,
        'streams':         get_streams_for_school(school, section),
    })


@login_required(login_url='login')
def report_card_select(request):
    """
    Official report-card workspace. Report cards are generated from published
    assessment contexts only, without teacher-side manual year/term filters.
    Workspace-aware: uses Primary grades/template when in Primary workspace.
    """
    teacher = get_teacher_for_user(request.user)
    is_admin_view = user_has_main_school_admin_override(request.user)
    class_teacher_scope = get_class_teacher_scope(teacher)

    # Determine workspace section for grade choices and template
    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary
    if is_lower_primary:
        grade_choices = LOWER_PRIMARY_GRADE_CHOICES
    elif is_primary:
        grade_choices = PRIMARY_GRADE_CHOICES
    else:
        grade_choices = GRADE_CHOICES

    if not is_admin_view and not class_teacher_scope:
        messages.error(request, "Report cards are available to administrators and assigned class teachers only.")
        return redirect('results_list')

    published_contexts = get_published_contexts_for_user(request.user, require_class_teacher=True)
    if not is_admin_view and not published_contexts:
        messages.error(request, "No published report cards are available for your class yet.")
        return redirect('results_list')

    selected_context = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    grade = selected_context["class_name"] if selected_context else None
    stream = selected_context["stream"] if selected_context else None
    year = str(selected_context["year"]) if selected_context else None
    term = selected_context["term"] if selected_context else None
    exam_name = selected_context["exam_name"] if selected_context else None
    assessment = selected_context["assessment_slug"] if selected_context else "opener"

    students = Student.objects.none()
    if selected_context:
        students = get_students_ordered(grade, stream)
    school = get_request_school(request)

    sa_filter = dict(school=school, class_name=grade, stream=stream)
    if is_lower_primary:
        sa_filter['school_section'] = 'PRIMARY'
        sa_filter['sub_section'] = 'LOWER'
    elif is_primary:
        sa_filter['school_section'] = 'PRIMARY'
        sa_filter['sub_section'] = 'UPPER'
    else:
        sa_filter['school_section'] = 'JSS'
    total_required_subjects = SubjectAssignment.objects.filter(**sa_filter).values(
        "subject__code"
    ).distinct().count() if selected_context else 0

    # Use Primary template when in Primary workspace
    template = 'students/report_card_select_primary.html' if is_primary else 'students/report_card_select.html'

    return render(request, template, {
        'students':           students,
        'selected_grade':     grade,
        'selected_stream':    stream,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': assessment,
        'selected_exam':      exam_name,
        'selected_context_key': selected_context["context_key"] if selected_context else "",
        'published_contexts': published_contexts,
        'published_subject_count': selected_context["subject_count"] if selected_context else 0,
        'total_required_subjects': total_required_subjects,
        'student_count': len(students) if selected_context else 0,
        'years':              range(2024, datetime.date.today().year + 1),
        'terms':              TERM_CHOICES,
        'grades':             [class_teacher_scope[0]] if class_teacher_scope and not is_admin_view else grade_choices,
        'streams':            [class_teacher_scope[1]] if class_teacher_scope and not is_admin_view else get_streams_for_school(school, section),
        'assessments':        ['opener', 'mid', 'end'],
        'is_admin_view':      is_admin_view,
        'is_primary':         is_primary,
        'access_label':       "School-wide report cards" if is_admin_view else "Class teacher report cards",
        'class_teacher_scope': class_teacher_scope,
    })


def _grading_config_for(school, section, sub_section):
    """Fetch the GradingConfig for a section/sub_section, falling back gracefully."""
    config = GradingConfig.all_objects.filter(
        school=school, school_section=section, sub_section=sub_section,
    ).first()
    if not config:
        config = GradingConfig.all_objects.filter(
            school=school, school_section=section, sub_section__isnull=True,
        ).first()
    return config


@login_required(login_url='login')
@never_cache
def individual_report(request, student_id):
    """
    Renders a single student's full report card for a given term and assessment.
    Calculates class position, PLV, and class teacher remark automatically.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('report_card_select')

    student = get_school_object_or_403(Student, request, id=student_id)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        messages.error(request, "You are not allowed to open report cards for this class stream.")
        return redirect('report_card_select')

    year       = request.GET.get('year', datetime.date.today().year)
    term       = request.GET.get('term', 'Term 1')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)
    published_subject_codes = get_published_subject_codes(
        student.class_name,
        student.stream,
        year,
        term,
        db_assessment,
    )
    from ..models import Subject
    published_subjects_qs = Subject.objects.filter(school=school, code__in=published_subject_codes)

    # Fetch marks for this student
    marks        = Mark.objects.filter(
        school=school,
        student=student,
        year=year,
        term=term,
        exam_type=db_assessment,
        subject__in=published_subjects_qs,
        school_section=student.school_section,
    ).order_by('subject__code')
    total_marks  = sum(m.score  for m in marks if m.score)
    total_points = sum(m.points for m in marks if m.points)

    # Class position via single aggregation query
    class_scores = (
        Mark.objects.filter(
            school=school,
            student__class_name=student.class_name, student__stream=student.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .values('student_id').annotate(total_score=Sum('score')).order_by('-total_score')
    )
    sorted_ids  = [item['student_id'] for item in class_scores]
    class_count = len(sorted_ids)
    try:
        position = sorted_ids.index(student.id) + 1
    except ValueError:
        position = 0

    # Attach subject name and teacher to each mark
    is_lower_primary = student.school_section == 'PRIMARY' and student.sub_section == 'LOWER'
    is_primary = student.school_section == 'PRIMARY'
    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}
    teacher_map = {
        a.subject.code: a.teacher_profile.get_full_title()
        for a in SubjectAssignment.objects.filter(
            school=school,
            class_name=student.class_name, stream=student.stream
        ).select_related('teacher_profile__user', 'subject')
        if a.subject
    }
    marks_list = list(marks)
    for mark in marks_list:
        mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
        mark.teacher_name = teacher_map.get(mark.subject.code, '—')
        if is_primary and not mark.is_absent:
            pct = mark.score or 0
            mark.performance_level, mark.points = _get_primary_performance(pct)

    # ── Class average per subject (drives the Dev. column + chart) ─────────
    class_subject_avgs = (
        Mark.objects.filter(
            school=school,
            student__class_name=student.class_name, student__stream=student.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .exclude(is_absent=True)
        .values('subject__code')
        .annotate(avg_score=Avg('score'))
    )
    class_avg_map = {row['subject__code']: round(row['avg_score'], 1) for row in class_subject_avgs}

    for mark in marks_list:
        class_avg = class_avg_map.get(mark.subject.code)
        mark.class_average = class_avg
        if class_avg is not None and mark.score is not None and not mark.is_absent:
            mark.deviation = round(mark.score - class_avg, 1)
        else:
            mark.deviation = None

    # ── Grade descriptors, pulled live from GradingConfig (no hardcoding) ──
    grading_config = _grading_config_for(school, student.school_section, student.sub_section)
    grade_descriptors = grading_config.subject_scale if grading_config else []

    # ── Mean points + denominators for the stat boxes ──────────────────────
    assessed_subjects   = sum(1 for m in marks_list if m.score is not None and not m.is_absent)
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))
    mean_points         = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
    max_total_marks     = assessed_subjects * 100
    max_total_points    = assessed_subjects * max_points_per_subj

    # ── Chart payload: student score vs class average, per subject ─────────
    chart_data_json = json.dumps({
        'labels':    [m.subject_name for m in marks_list if not m.is_absent],
        'student':   [m.score for m in marks_list if not m.is_absent],
        'class_avg': [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent],
    })

    # PLV and class teacher remark
    overall_plv = calculate_primary_plv(total_marks, sum(1 for m in marks if m.score)) if is_primary else calculate_report_plv(total_points, total_marks)
    master_comment = ClassTeacherMasterComment.objects.filter(
        school=school,
        year=year, term=term, grade=student.class_name,
        stream=student.stream, exam_type=db_assessment,
    ).first()
    school_ht_comment = SchoolHeadteacherComment.objects.filter(
        school=school,
        year=year, term=term, exam_type=db_assessment,
        school_section=student.school_section,
    ).first()

    # Comment logic: blank by default, live while editable (< 30 days), frozen after
    class_teacher_remark = ""
    headteacher_comment = ""
    closing_date = None
    opening_date = None
    freeze_threshold = datetime.timedelta(days=30)
    now = datetime.datetime.now(datetime.timezone.utc)

    if master_comment and overall_plv != '-':
        ct_comment_field = f"comment_{overall_plv.lower()}"
        live_ct = getattr(master_comment, ct_comment_field, "") or ""
        if live_ct.strip():
            age = now - (master_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if master_comment.last_modified.tzinfo is None else master_comment.last_modified)
            if age < freeze_threshold:
                class_teacher_remark = live_ct
            else:
                class_teacher_remark = live_ct
                for m in marks_list:
                    if not m.frozen_class_teacher_comment:
                        m.frozen_class_teacher_comment = live_ct
                        m.frozen_closing_date = master_comment.closing_date
                        m.frozen_opening_date = master_comment.opening_date
                Mark.objects.filter(id__in=[m.id for m in marks_list]).update(
                    frozen_class_teacher_comment=live_ct,
                    frozen_closing_date=master_comment.closing_date,
                    frozen_opening_date=master_comment.opening_date,
                )
        elif marks_list and marks_list[0].frozen_class_teacher_comment:
            class_teacher_remark = marks_list[0].frozen_class_teacher_comment

    if school_ht_comment and overall_plv != '-':
        ht_comment_field = f"ht_comment_{overall_plv.lower()}"
        live_ht = getattr(school_ht_comment, ht_comment_field, "") or ""
        if live_ht.strip():
            age = now - (school_ht_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if school_ht_comment.last_modified.tzinfo is None else school_ht_comment.last_modified)
            if age < freeze_threshold:
                headteacher_comment = live_ht
            else:
                headteacher_comment = live_ht
                for m in marks_list:
                    if not m.frozen_headteacher_comment:
                        m.frozen_headteacher_comment = live_ht
                Mark.objects.filter(id__in=[m.id for m in marks_list]).update(
                    frozen_headteacher_comment=live_ht,
                )
        elif marks_list and marks_list[0].frozen_headteacher_comment:
            headteacher_comment = marks_list[0].frozen_headteacher_comment

    if master_comment:
        closing_date = master_comment.closing_date
        opening_date = master_comment.opening_date
    if not closing_date and marks_list and marks_list[0].frozen_closing_date:
        closing_date = marks_list[0].frozen_closing_date
    if not opening_date and marks_list and marks_list[0].frozen_opening_date:
        opening_date = marks_list[0].frozen_opening_date

    return render(request, 'students/individual_report_card.html', {
        'student':             student,
        'marks':               marks_list,
        'total_marks':         total_marks,
        'total_points':        total_points,
        'position':            position,
        'class_count':         class_count,
        'overall_plv':         overall_plv,
        'mean_points':         mean_points,
        'mean_points_max':     max_points_per_subj,
        'max_total_marks':     max_total_marks,
        'max_total_points':    max_total_points,
        'grade_descriptors':   grade_descriptors,
        'chart_data_json':     chart_data_json,
        'class_teacher_remark': class_teacher_remark,
        'headteacher_comment': headteacher_comment,
        'closing_date':        closing_date,
        'opening_date':        opening_date,
        'selected_year':       year,
        'selected_term':       term,
        'selected_assessment': ASSESSMENT_MAP.get(assessment, assessment),
        'today':               datetime.date.today(),
        'student_marks_list':  [{
            'student': student, 'marks': marks_list,
            'total_marks': total_marks, 'total_points': total_points,
            'overall_plv': overall_plv,
            'mean_points': mean_points,
            'mean_points_max': max_points_per_subj,
            'max_total_marks': max_total_marks,
            'max_total_points': max_total_points,
            'grade_descriptors': grade_descriptors,
            'chart_data_json': chart_data_json,
            'class_teacher_remark': class_teacher_remark,
            'headteacher_comment': headteacher_comment,
            'closing_date': closing_date,
            'opening_date': opening_date,
            'position': position, 'class_count': class_count,
        }],
    })


@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60)
def bulk_report_cards(request):
    """
    Renders report cards for a selected batch of students in a single pass.
    Uses prefetch_related for performance and calculates true class position for each.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('report_card_select')

    student_ids   = [sid for sid in request.GET.get('ids', '').split(',') if sid]
    year          = request.GET.get('year', datetime.date.today().year)
    term          = request.GET.get('term', 'Term 1')
    assessment    = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    selected_students_base = Student.objects.filter(id__in=student_ids, school=school)
    sample = selected_students_base.first()
    if sample and not user_can_access_class_stream(request.user, sample.class_name, sample.stream, require_class_teacher=True):
        messages.error(request, "You are not allowed to print bulk report cards for this class stream.")
        return redirect('report_card_select')
    if sample:
        selected_students_base = selected_students_base.filter(
            class_name=sample.class_name,
            stream=sample.stream,
        )
        if selected_students_base.count() != len(student_ids):
            messages.error(request, "All selected students must belong to the same class stream.")
            return redirect('report_card_select')

    is_primary = sample.school_section == 'PRIMARY' if sample else False
    is_lower_primary = (sample.school_section == 'PRIMARY' and sample.sub_section == 'LOWER') if sample else False
    from ..models import Subject

    published_subject_codes = set()
    if sample:
        published_subject_codes = get_published_subject_codes(
            sample.class_name,
            sample.stream,
            year,
            term,
            db_assessment,
        )
    published_subjects_qs = Subject.objects.filter(school=school, code__in=published_subject_codes)
    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}

    marks_prefetch = Prefetch(
        'marks',
        queryset=Mark.objects.filter(
            school=school,
            year=year,
            term=term,
            exam_type=db_assessment,
            subject__in=published_subjects_qs,
            school_section=sample.school_section,
        ).order_by('subject__code'),
        to_attr='cached_marks',
    )
    selected_students = selected_students_base.prefetch_related(marks_prefetch)

    if not selected_students:
        return render(request, 'students/bulk_report_cards.html', {'student_marks_list': [], 'class_count': 0})

    # Class-wide leaderboard in one query
    class_scores = (
        Mark.objects.filter(
            school=school,
            student__class_name=sample.class_name, student__stream=sample.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .values('student_id').annotate(total_score=Sum('score')).order_by('-total_score')
    )
    class_leaderboard    = [item['student_id'] for item in class_scores]
    total_class_count    = len(class_leaderboard)

    # Class average per subject (shared across the whole batch — same class/stream)
    class_subject_avgs = (
        Mark.objects.filter(
            school=school,
            student__class_name=sample.class_name, student__stream=sample.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .exclude(is_absent=True)
        .values('subject__code')
        .annotate(avg_score=Avg('score'))
    )
    class_avg_map = {row['subject__code']: round(row['avg_score'], 1) for row in class_subject_avgs}

    grading_config = _grading_config_for(school, sample.school_section, sample.sub_section) if sample else None
    grade_descriptors = grading_config.subject_scale if grading_config else []
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))

    # Teacher map for this class
    teacher_map = {
        a.subject.code: a.teacher_profile.get_full_title()
        for a in SubjectAssignment.objects.filter(
            school=school,
            class_name=sample.class_name, stream=sample.stream
        ).select_related('teacher_profile__user')
    }

    master_comment = ClassTeacherMasterComment.objects.filter(
        school=school,
        year=year, term=term, grade=sample.class_name,
        stream=sample.stream, exam_type=db_assessment,
    ).first()

    school_ht_comment = SchoolHeadteacherComment.objects.filter(
        school=school,
        year=year, term=term, exam_type=db_assessment,
        school_section=sample.school_section,
    ).first()

    freeze_threshold = datetime.timedelta(days=30)
    now = datetime.datetime.now(datetime.timezone.utc)

    student_marks_list = []
    for student in selected_students:
        marks        = student.cached_marks
        total_marks  = sum(m.score  for m in marks if m.score)
        total_points = sum(m.points for m in marks if m.points)

        for mark in marks:
            mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
            mark.teacher_name = teacher_map.get(mark.subject.code, '—')
            if is_primary and not mark.is_absent:
                pct = mark.score or 0
                mark.performance_level, mark.points = _get_primary_performance(pct)
            class_avg = class_avg_map.get(mark.subject.code)
            mark.class_average = class_avg
            if class_avg is not None and mark.score is not None and not mark.is_absent:
                mark.deviation = round(mark.score - class_avg, 1)
            else:
                mark.deviation = None

        assessed_subjects = sum(1 for m in marks if m.score is not None and not m.is_absent)
        mean_points       = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
        max_total_marks   = assessed_subjects * 100
        max_total_points  = assessed_subjects * max_points_per_subj

        chart_data_json = json.dumps({
            'labels':    [m.subject_name for m in marks if not m.is_absent],
            'student':   [m.score for m in marks if not m.is_absent],
            'class_avg': [class_avg_map.get(m.subject.code, 0) for m in marks if not m.is_absent],
        })

        try:
            position = class_leaderboard.index(student.id) + 1
        except ValueError:
            position = 0

        overall_plv          = calculate_primary_plv(total_marks, sum(1 for m in marks if m.score)) if sample.school_section == 'PRIMARY' else calculate_report_plv(total_points, total_marks)
        class_teacher_remark = ""
        headteacher_comment = ""
        closing_date = None
        opening_date = None

        if master_comment and overall_plv != '-':
            ct_comment_field = f"comment_{overall_plv.lower()}"
            live_ct = getattr(master_comment, ct_comment_field, "") or ""
            if live_ct.strip():
                age = now - (master_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if master_comment.last_modified.tzinfo is None else master_comment.last_modified)
                if age < freeze_threshold:
                    class_teacher_remark = live_ct
                else:
                    class_teacher_remark = live_ct
                    Mark.objects.filter(id__in=[m.id for m in marks]).update(
                        frozen_class_teacher_comment=live_ct,
                        frozen_closing_date=master_comment.closing_date,
                        frozen_opening_date=master_comment.opening_date,
                    )
            elif marks and marks[0].frozen_class_teacher_comment:
                class_teacher_remark = marks[0].frozen_class_teacher_comment

        if school_ht_comment and overall_plv != '-':
            ht_comment_field = f"ht_comment_{overall_plv.lower()}"
            live_ht = getattr(school_ht_comment, ht_comment_field, "") or ""
            if live_ht.strip():
                age = now - (school_ht_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if school_ht_comment.last_modified.tzinfo is None else school_ht_comment.last_modified)
                if age < freeze_threshold:
                    headteacher_comment = live_ht
                else:
                    headteacher_comment = live_ht
                    Mark.objects.filter(id__in=[m.id for m in marks]).update(
                        frozen_headteacher_comment=live_ht,
                    )
            elif marks and marks[0].frozen_headteacher_comment:
                headteacher_comment = marks[0].frozen_headteacher_comment

        if master_comment:
            closing_date = master_comment.closing_date
            opening_date = master_comment.opening_date
        if not closing_date and marks and marks[0].frozen_closing_date:
            closing_date = marks[0].frozen_closing_date
        if not opening_date and marks and marks[0].frozen_opening_date:
            opening_date = marks[0].frozen_opening_date

        student_marks_list.append({
            'student':             student,
            'marks':               marks,
            'total_marks':         total_marks,
            'total_points':        total_points,
            'overall_plv':         overall_plv,
            'mean_points':         mean_points,
            'mean_points_max':     max_points_per_subj,
            'max_total_marks':     max_total_marks,
            'max_total_points':    max_total_points,
            'grade_descriptors':   grade_descriptors,
            'chart_data_json':     chart_data_json,
            'class_teacher_remark': class_teacher_remark,
            'headteacher_comment': headteacher_comment,
            'closing_date':        closing_date,
            'opening_date':        opening_date,
            'position':            position,
        })

    student_marks_list.sort(key=lambda x: (x['position'] == 0, x['position']))

    return render(request, 'students/bulk_report_cards.html', {
        'student_marks_list': student_marks_list,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': db_assessment,
        'class_count':        total_class_count,
        'closing_date':       master_comment.closing_date if master_comment else None,
        'opening_date':       master_comment.opening_date if master_comment else None,
    })