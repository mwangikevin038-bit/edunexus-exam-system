"""
Results and report card views for the EduNexus student management system.

Provides the official published-results workspace, report card selection,
individual student report card rendering, and bulk report card generation.
"""

import datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Q, Sum
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
    SUBJECT_DISPLAY_ORDER,
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
    get_cached_class_averages,
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
    teacher = get_teacher_for_user(request.user)
    class_teacher_scope = get_class_teacher_scope(teacher)

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Determine workspace section for grade choices and template
    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    # ── Sub-section access control ──────────────────────────────────────
    # Determine what sub-sections this user is allowed to see.
    # Admins/both-access: can switch freely.  Section-scoped teachers: locked.
    teacher_sub_section = None
    if is_admin_view:
        can_switch_sub = True
    elif teacher and teacher.school_section == 'PRIMARY' and teacher.sub_section:
        can_switch_sub = False
        teacher_sub_section = teacher.sub_section  # 'LOWER' or 'UPPER'
    elif teacher and teacher.school_section == 'BOTH':
        can_switch_sub = True
    elif is_lower_primary:
        can_switch_sub = False
        teacher_sub_section = 'LOWER'
    else:
        can_switch_sub = True

    active_sub = request.GET.get('sub', '').strip().upper()
    if is_lower_primary:
        active_sub = 'LOWER'
    elif is_primary:
        # Enforce teacher's sub-section lock
        if not can_switch_sub and teacher_sub_section:
            active_sub = teacher_sub_section
        elif active_sub not in ('LOWER', 'UPPER'):
            active_sub = request.session.get('active_sub', 'UPPER')
        if active_sub not in ('LOWER', 'UPPER'):
            active_sub = 'UPPER'
    if is_primary:
        request.session['active_sub'] = active_sub
        request.session.modified = True

    if is_lower_primary:
        grade_choices = LOWER_PRIMARY_GRADE_CHOICES
    elif is_primary:
        grade_choices = PRIMARY_GRADE_CHOICES
    else:
        grade_choices = GRADE_CHOICES

    published_contexts = get_published_contexts_for_user(request.user, sub_section=active_sub if is_primary else None)
    selected_context = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    if not selected_context and published_contexts:
        selected_context = published_contexts[0]

    year = str(selected_context["year"]) if selected_context else None
    term = selected_context["term"] if selected_context else None
    grade = selected_context["class_name"] if selected_context else None
    stream = selected_context["stream"] if selected_context else None
    exam_type = selected_context["exam_name"] if selected_context else None
    selected_context_key = selected_context["context_key"] if selected_context else ""

    # Use the correct subject map for the workspace section
    if is_lower_primary or (is_primary and active_sub == 'LOWER'):
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
        published_subject_codes = get_published_subject_codes(grade, stream, year, term, exam_type, sub_section=active_sub if is_primary else None, is_admin=is_admin_view)
        published_subject_count = len(published_subject_codes)

        # Get Subject objects for published subjects and keep stable display labels.
        from ..models import Subject
        published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)
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
        sa_qs = SubjectAssignment.all_objects.filter(school=school, class_name=grade, stream=stream).select_related('teacher_profile__user', 'subject')
        if section == 'LOWER_PRIMARY':
            sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section=active_sub)
        elif section == 'JSS':
            sa_qs = sa_qs.filter(school_section='JSS')
        for a in sa_qs:
            code = a.subject.code if a.subject else None
            if code:
                teacher_map[subject_label_map.get(code, subject_map.get(code, code))] = a.teacher_profile.get_full_title()
        for short in analysis_data:
            analysis_data[short]['teacher_name'] = teacher_map.get(short, '—')

        # ── Read from ExamSummary cache (populated by Celery task on Publish) ──
        from ..models import ExamSummary
        # Map workspace section to DB school_section
        if is_lower_primary:
            db_section = 'PRIMARY'
            db_sub = 'LOWER'
        elif is_primary:
            db_section = 'PRIMARY'
            db_sub = active_sub
        else:
            db_section = 'JSS'
            db_sub = None
        summaries_qs = ExamSummary.all_objects.filter(
            school=school,
            student__class_name=grade,
            year=year,
            term=term,
            exam_name=exam_type,
            school_section=db_section,
            sub_section=db_sub,
        )
        totals_map = {s.student_id: s for s in summaries_qs}

        # Fetch all marks for this class in ONE query (no N+1 prefetch)
        all_marks = Mark.all_objects.filter(
            school=school,
            student__class_name=grade,
            student__stream=stream,
            year=year, term=term, exam_type=exam_type,
            subject__in=published_subjects_qs,
        ).select_related('subject').order_by('subject', '-date_recorded', '-id')

        # Group marks by student_id
        marks_by_student = {}
        for mark in all_marks:
            marks_by_student.setdefault(mark.student_id, []).append(mark)

        students = Student.all_objects.filter(
            school=school, class_name=grade, stream=stream, is_active=True,
        ).order_by('admission_no')
        student_count = students.count()

        # ── Pre-compute per-subject analysis from flat mark query (single pass) ──
        for mark in all_marks:
            if mark.is_absent or mark.score is None:
                continue
            code = mark.subject.code
            short = subject_label_map.get(code, subject_map.get(code, code))
            if short not in analysis_data:
                continue
            analysis_data[short]['entries'] += 1
            analysis_data[short]['total_score'] += mark.score
            if is_primary:
                lv, _ = _get_primary_performance(mark.score, school=school, section=section, sub_section=active_sub if is_primary else None)
            else:
                lv, _ = get_performance_level(mark.score)
            if lv in analysis_data[short]['distribution']:
                analysis_data[short]['distribution'][lv] += 1

        for student in students:
            student_marks = marks_by_student.get(student.id, [])
            marks_dict = {}
            for mark in student_marks:
                marks_dict.setdefault(mark.subject.code, mark)

            # Read from ExamSummary cache
            t = totals_map.get(student.id)
            total_marks = t.total_marks if t else 0
            total_points = t.total_points if t else 0
            assessed_subjects = t.subject_count if t else 0

            # Pure read-only fallback: compute from marks if ExamSummary is empty/zero
            if not t or (total_marks == 0 and total_points == 0):
                # student_marks is a list, not a queryset — aggregate manually
                total_marks = sum(m.score for m in student_marks if m.score is not None)
                total_points = sum(m.points for m in student_marks if m.points is not None)
                assessed_subjects = sum(1 for m in student_marks if m.score is not None and not m.is_absent)

            row_scores = []
            for code, short in published_subjects:
                m = marks_dict.get(code)
                if m and m.score is not None:
                    if m.is_absent:
                        row_scores.append({'score': 'AB', 'level': 'AB'})
                    else:
                        level, points = _get_primary_performance(m.score, school=school, section=section, sub_section=active_sub if is_primary else None) if is_primary else get_performance_level(m.score)
                        row_scores.append({'score': m.score, 'level': level})
                else:
                    row_scores.append({'score': '-', 'level': '-'})

            broadsheet.append({
                'student': student,
                'scores':  row_scores,
                'tps':     total_points,
                'total':   total_marks,
                'plv':     calculate_primary_plv(total_marks, assessed_subjects, sub_section=active_sub if is_primary else None, school=school, section=section) if is_primary else calculate_broadsheet_plv(total_marks, total_points),
            })

        # Sort by DB-computed rank (total_marks DESC, total_points DESC)
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
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if grade and grade in LOWER_PRIMARY_GRADE_CHOICES:
        section_accent = section_colors['LOWER_PRIMARY']
    elif is_primary:
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors.get(section, '#305CDE')

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
        'section':         section,
        'active_sub':      active_sub,
        'can_switch_sub':  can_switch_sub,
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

    # ── Sub-section access control ──────────────────────────────────────
    teacher_sub_section = None
    if is_admin_view:
        can_switch_sub = True
    elif teacher and teacher.school_section == 'PRIMARY' and teacher.sub_section:
        can_switch_sub = False
        teacher_sub_section = teacher.sub_section
    elif teacher and teacher.school_section == 'BOTH':
        can_switch_sub = True
    elif is_lower_primary:
        can_switch_sub = False
        teacher_sub_section = 'LOWER'
    else:
        can_switch_sub = True

    active_sub = request.GET.get('sub', '').strip().upper()
    if is_lower_primary:
        active_sub = 'LOWER'
    elif is_primary:
        if not can_switch_sub and teacher_sub_section:
            active_sub = teacher_sub_section
        elif active_sub not in ('LOWER', 'UPPER'):
            active_sub = request.session.get('active_sub', 'UPPER')
        if active_sub not in ('LOWER', 'UPPER'):
            active_sub = 'UPPER'
    if is_primary:
        request.session['active_sub'] = active_sub
        request.session.modified = True

    if is_lower_primary:
        grade_choices = LOWER_PRIMARY_GRADE_CHOICES
    elif is_primary:
        grade_choices = PRIMARY_GRADE_CHOICES
    else:
        grade_choices = GRADE_CHOICES

    if not is_admin_view and not class_teacher_scope:
        messages.error(request, "Report cards are available to administrators and assigned class teachers only.")
        return redirect('results_list')

    published_contexts = get_published_contexts_for_user(request.user, require_class_teacher=True, sub_section=active_sub if is_primary else None)
    if not is_admin_view and not published_contexts:
        messages.error(request, "No published report cards are available for your class yet.")
        return redirect('results_list')

    selected_context = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    if not selected_context and published_contexts:
        selected_context = published_contexts[0]

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
        sa_filter['sub_section'] = active_sub
    else:
        sa_filter['school_section'] = 'JSS'
    total_required_subjects = SubjectAssignment.all_objects.filter(**sa_filter).values(
        "subject__code"
    ).distinct().count() if selected_context else 0

    # Use Primary template when in Primary workspace
    template = 'students/report_card_select_primary.html' if is_primary else 'students/report_card_select.html'

    context_data = {
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
        'section':            section,
        'active_sub':         active_sub,
        'can_switch_sub':     can_switch_sub,
        'section_accent':     '#B45309' if (is_primary and active_sub == 'LOWER') else ('#00674F' if is_primary else '#305CDE'),
    }
    if is_primary:
        from ..models import Exam
        school_obj = get_request_school(request)
        context_data['lower_exam_count'] = Exam.all_objects.filter(school=school_obj, school_section='PRIMARY', sub_section='LOWER', status='active', is_deleted=False).count()
        context_data['upper_exam_count'] = Exam.all_objects.filter(school=school_obj, school_section='PRIMARY', sub_section='UPPER', status='active', is_deleted=False).count()

    return render(request, template, context_data)


_grading_config_cache = {}  # Kept for backward compat — delegates to grading_engine



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

    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        messages.error(request, "You are not allowed to open report cards for this class stream.")
        return redirect('report_card_select')

    is_admin_view = user_has_main_school_admin_override(request.user)

    year       = request.GET.get('year', datetime.date.today().year)
    term       = request.GET.get('term', 'Term 1')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    # Determine sub_section from grade for Lower Primary filtering
    student_sub_section = 'LOWER' if student.class_name in LOWER_PRIMARY_GRADE_CHOICES else ('UPPER' if student.school_section == 'PRIMARY' else None)

    published_subject_codes = get_published_subject_codes(
        student.class_name,
        student.stream,
        year,
        term,
        db_assessment,
        sub_section=student_sub_section,
        is_admin=is_admin_view,
    )
    from ..models import Subject
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

    # Fetch marks for this student
    marks        = Mark.all_objects.filter(
        school=school,
        student=student,
        year=year,
        term=term,
        exam_type=db_assessment,
        subject__in=published_subjects_qs,
        school_section=student.school_section,
    )
    # Sort marks by SUBJECT_DISPLAY_ORDER instead of alphabetical
    marks = sorted(marks, key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))

    # ── Read from ExamSummary cache (populated by Celery task on Publish) ──
    from ..models import ExamSummary
    summary = ExamSummary.all_objects.filter(
        school=school,
        student=student,
        year=year,
        term=term,
        exam_name=db_assessment,
        school_section=student.school_section,
        sub_section=student.sub_section,
    ).first()

    # Grade-wide rank: count summaries with higher total_marks
    grade_summaries = ExamSummary.all_objects.filter(
        school=school,
        year=year,
        term=term,
        exam_name=db_assessment,
        school_section=student.school_section,
        sub_section=student.sub_section,
    )

    # READ-ONLY FALLBACK: If ExamSummary not populated, compute from live marks
    class_count = grade_summaries.count()
    if summary:
        total_marks  = summary.total_marks
        total_points = summary.total_points
        position     = summary.grade_rank
        assessed_subjects = summary.subject_count
    else:
        # FIXED: Use safe in-memory list operations instead of QuerySet aggregations
        valid_scores = [m.score for m in marks if m.score is not None]
        valid_points = [m.points for m in marks if m.points is not None]

        total_marks = sum(valid_scores)
        total_points = sum(valid_points)
        assessed_subjects = len(valid_scores) if len(valid_scores) > 0 else 1

        # READ-ONLY RANKING ENGINE: Compare this student's totals against all grade peers
        grade_scores_qs = (
            Mark.all_objects.filter(
                school=school,
                year=year, term=term,
                exam_type=db_assessment, school_section=student.school_section,
            )
            .values('student_id')
            .annotate(student_total=Sum('score'))
        )
        current_student_score = total_marks
        better_performing = sum(1 for c in grade_scores_qs if (c['student_total'] or 0) > current_student_score)
        position = better_performing + 1 if current_student_score > 0 else 0
        class_count = len(grade_scores_qs) if grade_scores_qs else 1

    if not class_count:
        class_count = grade_summaries.count()

    # FIXED: Combine position and class count into the exact display string the template uses
    total_students = class_count if class_count > 0 else 1
    position_display = f"{position}/{total_students}" if position > 0 else "-"

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
        for a in SubjectAssignment.all_objects.filter(
            school=school,
            class_name=student.class_name, stream=student.stream
        ).select_related('teacher_profile__user', 'subject')
        if a.subject
    }
    # Class teacher name for this class/stream — determined by assigned_task field
    from ..models import Teacher
    class_teacher_name = ""
    ct_q = Teacher.all_objects.filter(
        school=school,
        assigned_task__icontains=student.class_name,
    ).filter(
        Q(assigned_task__icontains=student.stream),
    ).select_related('user').first()
    if ct_q:
        class_teacher_name = ct_q.get_full_title()
    marks_list = list(marks)
    for mark in marks_list:
        mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
        mark.teacher_name = teacher_map.get(mark.subject.code, '—')
        if is_primary and not mark.is_absent:
            pct = mark.score or 0
            mark.performance_level, mark.points = _get_primary_performance(pct)

    # ── Class average per subject (drives the Dev. column + chart) ─────────
    class_subject_avgs = (
        Mark.all_objects.filter(
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
    grade_descriptors = resolve_scale_fast(school.pk, student.school_section, student.sub_section)

    # ── Mean points + denominators for the stat boxes ──────────────────────
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))
    mean_points         = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
    max_total_marks     = assessed_subjects * 100
    max_total_points    = assessed_subjects * max_points_per_subj

    # ── Chart payload: student score vs class average, per subject ─────────
    from .constants import SUBJECT_SHORT_MAP as _JSS_SHORT, PRIMARY_SUBJECT_SHORT_MAP as _PRI_SHORT
    _short = _PRI_SHORT if is_primary else _JSS_SHORT
    chart_data_json = json.dumps({
        'labels':       [m.subject_name for m in marks_list if not m.is_absent],
        'short_labels': [_short.get(m.subject.code, m.subject_name) for m in marks_list if not m.is_absent],
        'student':      [m.score for m in marks_list if not m.is_absent],
        'class_avg':    [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent],
        'student_name': student.name.split()[0] if student.name else 'Student',
        'class_name':   f"{student.class_name} {student.stream}".strip(),
    })

    # PLV — read from ExamSummary cache
    overall_plv = summary.overall_plv if summary else ('-' if assessed_subjects == 0 else calculate_primary_plv(total_marks, assessed_subjects, sub_section=student.sub_section, school=school, section=student.school_section) if is_primary else calculate_report_plv(total_points, total_marks, school=school, section=student.school_section))
    ct_comment_mgr = ClassTeacherMasterComment.all_objects if is_admin_view else ClassTeacherMasterComment.objects
    master_comment = ct_comment_mgr.filter(
        school=school,
        year=year, term=term, grade=student.class_name,
        stream=student.stream, exam_type=db_assessment,
    ).first()
    ht_comment_mgr = SchoolHeadteacherComment.all_objects if is_admin_view else SchoolHeadteacherComment.objects
    school_ht_comment = ht_comment_mgr.filter(
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
                Mark.all_objects.filter(id__in=[m.id for m in marks_list]).update(
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
                Mark.all_objects.filter(id__in=[m.id for m in marks_list]).update(
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

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if student.school_section == 'PRIMARY' and student.sub_section == 'LOWER':
        section_accent = section_colors['LOWER_PRIMARY']
    elif student.school_section == 'PRIMARY':
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    return render(request, 'students/report_card.html', {
        'student':             student,
        'marks':               marks_list,
        'total_marks':         total_marks,
        'total_points':        total_points,
        'position':            position,
        'position_display':    position_display,
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
        'selected_grade':      student.class_name,
        'selected_stream':     student.stream,
        'today':               datetime.date.today(),
        'section_accent':      section_accent,
        'view_mode':           'individual',
        'show_mobile_shell':   True,
        'show_header':         True,
        'show_control_panel':  False,
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
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment': headteacher_comment,
            'closing_date': closing_date,
            'opening_date': opening_date,
            'position': position, 'position_display': position_display, 'class_count': class_count,
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

    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)

    student_ids   = [sid for sid in request.GET.get('ids', '').split(',') if sid]
    year          = request.GET.get('year', datetime.date.today().year)
    term          = request.GET.get('term', 'Term 1')
    assessment    = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    selected_students_base = Student.all_objects.filter(id__in=student_ids, school=school)
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
    is_admin_view = user_has_main_school_admin_override(request.user)
    from ..models import Subject

    published_subject_codes = set()
    if sample:
        published_subject_codes = get_published_subject_codes(
            sample.class_name,
            sample.stream,
            year,
            term,
            db_assessment,
            sub_section=sample.sub_section if is_primary else None,
            is_admin=is_admin_view,
        )
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)
    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}

    # ── Single flat mark query (no ORM Prefetch, no N+1) ──
    all_marks_bulk = Mark.all_objects.filter(
        school=school,
        year=year,
        term=term,
        exam_type=db_assessment,
        subject__in=published_subjects_qs,
        school_section=sample.school_section,
        student__class_name=sample.class_name,
        student__stream=sample.stream,
    ).select_related('subject').order_by('subject', '-date_recorded', '-id')

    marks_by_student_bulk = {}
    for mark in all_marks_bulk:
        marks_by_student_bulk.setdefault(mark.student_id, []).append(mark)

    selected_students = list(selected_students_base)

    if not selected_students:
        return render(request, 'students/report_card.html', {
            'student_marks_list': [],
            'class_count': 0,
            'view_mode': 'bulk',
            'show_mobile_shell': False,
            'show_header': True,
            'show_control_panel': False,
            'is_async': False,
        })

    # ── Read from ExamSummary cache (populated by Celery task on Publish) ──
    from ..models import ExamSummary
    summaries_qs = ExamSummary.all_objects.filter(
        school=school,
        student__class_name=sample.class_name,
        year=year,
        term=term,
        exam_name=db_assessment,
        school_section=sample.school_section,
        sub_section=sample.sub_section,
    )
    # Grade-wide map: student_id → ExamSummary
    all_summaries = {s.student_id: s for s in summaries_qs}
    total_class_count = len(all_summaries)

    # Grade-wide rank: sort ALL summaries by total_marks DESC, total_points DESC
    grade_sorted = sorted(summaries_qs, key=lambda s: (-s.total_marks, -s.total_points))
    grade_rank_map = {s.student_id: rank for rank, s in enumerate(grade_sorted, start=1)}

    # Class average per subject — cached in Redis for 1 hour
    class_avg_map = get_cached_class_averages(
        school, sample.class_name, sample.stream,
        year, term, db_assessment, published_subjects_qs,
    )

    grade_descriptors = resolve_scale_fast(school.pk, sample.school_section, sample.sub_section) if sample else []
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))

    # Teacher map for this class
    teacher_map = {
        a.subject.code: a.teacher_profile.get_full_title()
        for a in SubjectAssignment.all_objects.filter(
            school=school,
            class_name=sample.class_name, stream=sample.stream
        ).select_related('teacher_profile__user', 'subject')
    }

    # Class teacher name for this class/stream — determined by assigned_task field
    from ..models import Teacher
    class_teacher_name = ""
    ct_q = Teacher.all_objects.filter(
        school=school,
        assigned_task__icontains=sample.class_name,
    ).filter(
        Q(assigned_task__icontains=sample.stream),
    ).select_related('user').first()
    if ct_q:
        class_teacher_name = ct_q.get_full_title()

    ct_comment_mgr = ClassTeacherMasterComment.all_objects if is_admin_view else ClassTeacherMasterComment.objects
    master_comment = ct_comment_mgr.filter(
        school=school,
        year=year, term=term, grade=sample.class_name,
        stream=sample.stream, exam_type=db_assessment,
    ).first()

    ht_comment_mgr = SchoolHeadteacherComment.all_objects if is_admin_view else SchoolHeadteacherComment.objects
    school_ht_comment = ht_comment_mgr.filter(
        school=school,
        year=year, term=term, exam_type=db_assessment,
        school_section=sample.school_section,
    ).first()

    freeze_threshold = datetime.timedelta(days=30)
    now = datetime.datetime.now(datetime.timezone.utc)

    student_marks_list = []
    for student in selected_students:
        marks        = sorted(marks_by_student_bulk.get(student.id, []), key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))

        # Read from ExamSummary cache
        summary = all_summaries.get(student.id)
        if summary:
            total_marks  = summary.total_marks
            total_points = summary.total_points
            assessed_subjects = summary.subject_count
        else:
            # FIXED: Use safe in-memory list operations instead of QuerySet aggregations
            valid_scores = [m.score for m in marks if m.score is not None]
            valid_points = [m.points for m in marks if m.points is not None]
            total_marks = sum(valid_scores)
            total_points = sum(valid_points)
            assessed_subjects = len(valid_scores) if len(valid_scores) > 0 else 1

        for mark in marks:
            mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
            mark.teacher_name = teacher_map.get(mark.subject.code, '—')
            if is_primary and not mark.is_absent:
                pct = mark.score or 0
                mark.performance_level, mark.points = _get_primary_performance(pct, school=school, section=student.school_section, sub_section=student.sub_section)
            class_avg = class_avg_map.get(mark.subject.code)
            mark.class_average = class_avg
            if class_avg is not None and mark.score is not None and not mark.is_absent:
                mark.deviation = round(mark.score - class_avg, 1)
            else:
                mark.deviation = None

        mean_points       = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
        max_total_marks   = assessed_subjects * 100
        max_total_points  = assessed_subjects * max_points_per_subj

        from .constants import SUBJECT_SHORT_MAP as _JSS_SHORT2, PRIMARY_SUBJECT_SHORT_MAP as _PRI_SHORT2
        _short2 = _PRI_SHORT2 if is_primary else _JSS_SHORT2
        chart_data_json = json.dumps({
            'labels':       [m.subject_name for m in marks if not m.is_absent],
            'short_labels': [_short2.get(m.subject.code, m.subject_name) for m in marks if not m.is_absent],
            'student':      [m.score for m in marks if not m.is_absent],
            'class_avg':    [class_avg_map.get(m.subject.code, 0) for m in marks if not m.is_absent],
            'student_name': student.name.split()[0] if student.name else 'Student',
            'class_name':   f"{student.class_name} {student.stream}".strip(),
        })

        position = grade_rank_map.get(student.id, 0)  # Stream-specific rank from ExamSummary

        overall_plv          = summary.overall_plv if summary else ('-' if assessed_subjects == 0 else calculate_primary_plv(total_marks, assessed_subjects, sub_section=sample.sub_section, school=school, section=sample.school_section) if sample.school_section == 'PRIMARY' else calculate_report_plv(total_points, total_marks, school=school, section=sample.school_section))
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
                    Mark.all_objects.filter(id__in=[m.id for m in marks]).update(
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
                    Mark.all_objects.filter(id__in=[m.id for m in marks]).update(
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
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment': headteacher_comment,
            'closing_date':        closing_date,
            'opening_date':        opening_date,
            'position':            position,
            'class_count':         total_class_count,
        })

    student_marks_list.sort(key=lambda x: (x['position'] == 0, x['position']))

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if is_lower_primary:
        section_accent = section_colors['LOWER_PRIMARY']
    elif is_primary:
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    return render(request, 'students/report_card.html', {
        'student_marks_list': student_marks_list,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': db_assessment,
        'selected_grade':     sample.class_name if sample else '',
        'selected_stream':    sample.stream if sample else '',
        'class_count':        total_class_count,
        'closing_date':       master_comment.closing_date if master_comment else None,
        'opening_date':       master_comment.opening_date if master_comment else None,
        'section_accent':     section_accent,
        'view_mode':          'bulk',
        'show_mobile_shell':  False,
        'show_header':        True,
        'show_control_panel': False,
        'is_async':           False,
    })


@login_required(login_url='login')
def report_card_poll_status(request):
    """
    AJAX endpoint for polling report card generation status.
    Returns JSON with {ready: bool, loaded: int, total: int, html: str}.
    """
    from django.http import JsonResponse
    from django.template.loader import render_to_string

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context required'}, status=400)

    # For now, report cards are generated synchronously, so always ready
    # This endpoint can be extended later for async Celery-based generation
    return JsonResponse({
        'ready': True,
        'loaded': 0,
        'total': 0,
        'html': '',
    })


def build_broadsheet_for_merit_list(request, school, grade, stream, exam):
    """
    Build the broadsheet data for the inline merit list display.
    Returns dict with broadsheet, published_subjects, ordered_levels, etc.
    """
    from .constants import (
        ORDERED_LEVELS,
        PRIMARY_PERF_LEVELS,
        PRIMARY_SUBJECT_SHORT_MAP,
        SUBJECT_SHORT_MAP,
        LOWER_PRIMARY_SUBJECT_SHORT_MAP,
        sort_subjects,
    )
    from .exams import _get_primary_performance
    from .helpers import (
        calculate_broadsheet_plv,
        calculate_primary_plv,
        get_performance_level,
        get_published_subject_codes,
        user_has_main_school_admin_override,
    )
    from ..models import ExamSummary, Subject

    is_admin_view = user_has_main_school_admin_override(request.user)
    section = exam.school_section or 'JSS'
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary
    is_combined = stream == 'Combined' or not stream

    # Resolve actual streams for Combined / no-stream mode
    actual_streams = []
    if is_combined:
        from ..models import Stream
        actual_streams = list(
            Stream.all_objects.filter(school=school, grade__name=grade)
            .values_list('name', flat=True).order_by('name')
        )

    # Map workspace section to active_sub for Primary
    if is_lower_primary:
        active_sub = 'LOWER'
    elif is_primary:
        active_sub = exam.sub_section or 'UPPER'
        if active_sub not in ('LOWER', 'UPPER'):
            active_sub = 'UPPER'
    else:
        active_sub = None

    if is_lower_primary or (is_primary and active_sub == 'LOWER'):
        subject_map = LOWER_PRIMARY_SUBJECT_SHORT_MAP
    elif is_primary:
        subject_map = PRIMARY_SUBJECT_SHORT_MAP
    else:
        subject_map = SUBJECT_SHORT_MAP

    active_levels = PRIMARY_PERF_LEVELS if is_primary else ORDERED_LEVELS
    if is_combined:
        # Union published subjects across all streams
        published_subject_codes = set()
        for s_name in actual_streams:
            published_subject_codes |= get_published_subject_codes(
                grade, s_name, exam.year, exam.term, exam.name,
                sub_section=active_sub if is_primary else None,
                is_admin=is_admin_view,
            )
    else:
        published_subject_codes = get_published_subject_codes(
            grade, stream, exam.year, exam.term, exam.name,
            sub_section=active_sub if is_primary else None,
            is_admin=is_admin_view,
        )
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)
    subject_label_map = {
        s.code: (subject_map.get(s.code) or s.name or s.code)
        for s in published_subjects_qs
    }
    published_subjects = sort_subjects([
        (code, subject_label_map.get(code, subject_map.get(code, code)))
        for code in published_subject_codes
    ])

    # Read ExamSummary cache
    if is_lower_primary:
        db_section = 'PRIMARY'
        db_sub = 'LOWER'
    elif is_primary:
        db_section = 'PRIMARY'
        db_sub = active_sub
    else:
        db_section = 'JSS'
        db_sub = None
    summaries_qs = ExamSummary.all_objects.filter(
        school=school,
        student__class_name=grade,
        year=exam.year,
        term=exam.term,
        exam_name=exam.name,
        school_section=db_section,
    )
    if db_sub:
        summaries_qs = summaries_qs.filter(sub_section=db_sub)
    else:
        summaries_qs = summaries_qs.filter(Q(sub_section__isnull=True) | Q(sub_section=''))
    totals_map = {s.student_id: s for s in summaries_qs}

    all_marks = Mark.all_objects.filter(
        school=school,
        student__class_name=grade,
        student__stream__in=actual_streams if is_combined else [stream],
        year=exam.year, term=exam.term, exam_type=exam.name,
        subject__in=published_subjects_qs,
    ).select_related('subject').order_by('subject', '-date_recorded', '-id')

    marks_by_student = {}
    for mark in all_marks:
        marks_by_student.setdefault(mark.student_id, []).append(mark)

    students = Student.all_objects.filter(
        school=school, class_name=grade, stream__in=actual_streams if is_combined else [stream], is_active=True,
    ).order_by('admission_no')

    broadsheet = []
    for student in students:
        student_marks = marks_by_student.get(student.id, [])
        marks_dict = {}
        for mark in student_marks:
            marks_dict.setdefault(mark.subject.code, mark)

        t = totals_map.get(student.id)
        total_marks = t.total_marks if t else 0
        total_points = t.total_points if t else 0
        assessed_subjects = t.subject_count if t else 0

        if not t or (total_marks == 0 and total_points == 0):
            # student_marks is a list, not a queryset — aggregate manually
            total_marks = sum(m.score for m in student_marks if m.score is not None)
            total_points = sum(m.points for m in student_marks if m.points is not None)
            assessed_subjects = sum(1 for m in student_marks if m.score is not None and not m.is_absent)

        row_scores = []
        for code, short in published_subjects:
            m = marks_dict.get(code)
            if m and m.score is not None:
                if m.is_absent:
                    row_scores.append({'score': 'AB', 'level': 'AB', 'subject_id': m.subject_id})
                else:
                    if is_primary:
                        level, _ = _get_primary_performance(
                            m.score,
                            school=school,
                            section=section,
                            sub_section=active_sub if is_primary else None,
                            subject_id=m.subject_id,
                        )
                    else:
                        # Subject-specific scale is honored by passing the live subject_id;
                        # resolve_scale_fast falls back to the general row automatically.
                        # Pass school=school, section=section explicitly — thread-local
                        # ContextVar is 'BOTH' for admin users and never matches a cached key.
                        level, _ = get_performance_level(
                            m.score,
                            sub_section=active_sub,
                            subject_id=m.subject_id,
                            school=school,
                            section=section,
                        )
                    row_scores.append({'score': m.score, 'level': level, 'subject_id': m.subject_id})
            else:
                row_scores.append({'score': '-', 'level': '-', 'subject_id': None})

        # PLV column uses the broadsheet's `total_scale` (aggregated marks → level)
        total_level, total_pts = get_performance_level(
            total_marks,
            sub_section=active_sub,
            is_total_calculation=True,
            school=school,
            section=section,
        )

        broadsheet.append({
            'student': student,
            'scores':  row_scores,
            'tps':     total_points,
            'total':   total_marks,
            'plv':     total_level,
        })

    broadsheet.sort(key=lambda x: (-x['total'], -x['tps']))

    # Assign positions
    for i, row in enumerate(broadsheet, start=1):
        row['position'] = i

    # ── Subject Performance Analysis Data Builder ───────
    analysis_data = {
        short: {
            'entries': 0, 'total_score': 0, 'mean_score': 0.0, 'mean_points': 0.0,
            'performance_text': '—',
            'distribution': {lvl: 0 for lvl in active_levels},
            'teacher_name': '—',
        }
        for _code, short in published_subjects
    }

    # Map assigned teachers for this grade/stream
    from ..models import SubjectAssignment
    teacher_map = {}
    sa_qs = SubjectAssignment.all_objects.filter(school=school, class_name=grade, stream__in=actual_streams if is_combined else [stream]).select_related('teacher_profile__user', 'subject')
    if section == 'LOWER_PRIMARY':
        sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        sa_qs = sa_qs.filter(school_section='PRIMARY', sub_section=active_sub)
    elif section == 'JSS':
        sa_qs = sa_qs.filter(school_section='JSS')
    for a in sa_qs:
        code = a.subject.code if a.subject else None
        if code:
            teacher_map[subject_label_map.get(code, subject_map.get(code, code))] = a.teacher_profile.get_full_title()
    for short in analysis_data:
        analysis_data[short]['teacher_name'] = teacher_map.get(short, '—')

    # ── FIXED SINGLE-PASS EXAM MARK AGGREGATOR ──
    for mark in all_marks:
        # Guarantee we only look at valid, recorded scores for the active exam sheet
        if mark.is_absent or mark.score is None:
            continue

        subj_code = mark.subject.code if mark.subject else None
        if not subj_code:
            continue

        # Convert database code to short template label name
        short = subject_label_map.get(subj_code, subject_map.get(subj_code, subj_code))
        if short not in analysis_data:
            continue

        # Add to tracking metrics row matrix safely
        analysis_data[short]['entries'] += 1
        analysis_data[short]['total_score'] += mark.score

        # Calculate level codes using our explicit section override context rules
        if is_primary:
            lv, pts = _get_primary_performance(
                mark.score, school=school, section=section,
                sub_section=active_sub if is_primary else None, subject_id=mark.subject_id,
            )
        else:
            lv, pts = get_performance_level(
                mark.score, sub_section=active_sub,
                subject_id=mark.subject_id, school=school, section=section,
            )

        if lv in analysis_data[short]['distribution']:
            analysis_data[short]['distribution'][lv] += 1

    # Finalize means, points lookups, and textual level descriptors per row
    for short, data in analysis_data.items():
        if data['entries'] > 0:
            data['mean_score'] = round(data['total_score'] / data['entries'], 2)

            # Resolve the mean text descriptor and points based on the calculated mean score
            if is_primary:
                txt, pts_val = _get_primary_performance(data['mean_score'], school=school, section=section, sub_section=active_sub if is_primary else None)
            else:
                txt, pts_val = get_performance_level(data['mean_score'], sub_section=active_sub, school=school, section=section)

            data['mean_points'] = round(pts_val, 4)
            data['performance_text'] = txt

    analysis_rows = [
        {'short': short, **analysis_data[short]} for _code, short in published_subjects
    ]

    # ── Grade Breakdown: per-stream performance from ExamSummary ───────────
    # totals_map already has ALL students in the grade (no stream filter)
    all_summaries = list(totals_map.values())

    grade_breakdown_rows = []
    ov_entries = 0
    ov_total_marks = 0
    ov_total_subj_count = 0
    ov_dist = {lvl: 0 for lvl in active_levels}

    # Collect unique stream names from the summaries
    seen_streams = set()
    for s in all_summaries:
        seen_streams.add(s.student.stream)
    all_streams = sorted(seen_streams)

    for s_name in all_streams:
        entries = 0
        total_marks = 0
        total_subj_count = 0
        dist = {lvl: 0 for lvl in active_levels}

        for summ in all_summaries:
            if summ.student.stream != s_name:
                continue
            if summ.total_marks == 0 and summ.total_points == 0:
                continue
            entries += 1
            total_marks += summ.total_marks
            total_subj_count += summ.subject_count or 0
            plv = (summ.overall_plv or '-').strip().upper()
            if plv in dist:
                dist[plv] += 1

        # Mean marks = total marks across all students / total subject entries
        mean_m = round(total_marks / total_subj_count, 1) if total_subj_count else 0
        # Mean points from ExamSummary.mean_points (already per-student mean)
        s_summ_list = [summ for summ in all_summaries if summ.student.stream == s_name
                       and (summ.total_marks != 0 or summ.total_points != 0)]
        mean_p = round(sum(summ.mean_points for summ in s_summ_list) / entries, 4) if entries else 0
        if entries:
            if is_primary:
                plv_txt, _ = _get_primary_performance(mean_m, school=school, section=section, sub_section=active_sub)
            else:
                plv_txt, _ = get_performance_level(mean_m, sub_section=active_sub, school=school, section=section)
        else:
            plv_txt = '—'

        grade_breakdown_rows.append({
            'label': f'{grade} {s_name}',
            'dist': dist,
            'entries': entries,
            'mean_score': mean_m,
            'mean_points': mean_p,
            'performance_text': plv_txt,
        })
        ov_entries += entries
        ov_total_marks += total_marks
        ov_total_subj_count += total_subj_count
        for lvl in active_levels:
            ov_dist[lvl] += dist[lvl]

    # Overall row
    ov_mean = round(ov_total_marks / ov_total_subj_count, 1) if ov_total_subj_count else 0
    ov_pts = round(sum(summ.mean_points for summ in all_summaries
                       if (summ.total_marks != 0 or summ.total_points != 0)) / ov_entries, 4) if ov_entries else 0
    if ov_entries:
        if is_primary:
            ov_plv, _ = _get_primary_performance(ov_mean, school=school, section=section, sub_section=active_sub)
        else:
            ov_plv, _ = get_performance_level(ov_mean, sub_section=active_sub, school=school, section=section)
    else:
        ov_plv = '—'

    grade_breakdown_rows.append({
        'label': grade,
        'dist': ov_dist,
        'entries': ov_entries,
        'mean_score': ov_mean,
        'mean_points': ov_pts,
        'performance_text': ov_plv,
        'is_overall': True,
    })

    # ── Gender Summary: boys vs girls in the current stream ────────────
    gender_rows = []
    for gender_label, gender_val in [('Girls', 'Female'), ('Boys', 'Male')]:
        g_entries = 0
        g_total_marks = 0
        g_total_subj_count = 0
        g_dist = {lvl: 0 for lvl in active_levels}

        for summ in all_summaries:
            if is_combined:
                if summ.student.stream not in actual_streams:
                    continue
            else:
                if summ.student.stream != stream:
                    continue
            if summ.student.gender != gender_val:
                continue
            if summ.total_marks == 0 and summ.total_points == 0:
                continue
            g_entries += 1
            g_total_marks += summ.total_marks
            g_total_subj_count += summ.subject_count or 0
            plv = (summ.overall_plv or '-').strip().upper()
            if plv in g_dist:
                g_dist[plv] += 1

        g_mean = round(g_total_marks / g_total_subj_count, 1) if g_total_subj_count else 0
        g_summ_list = [summ for summ in all_summaries if (summ.student.stream in actual_streams if is_combined else summ.student.stream == stream)
                       and summ.student.gender == gender_val
                       and (summ.total_marks != 0 or summ.total_points != 0)]
        g_pts = round(sum(summ.mean_points for summ in g_summ_list) / g_entries, 4) if g_entries else 0
        if g_entries:
            if is_primary:
                g_plv, _ = _get_primary_performance(g_mean, school=school, section=section, sub_section=active_sub)
            else:
                g_plv, _ = get_performance_level(g_mean, sub_section=active_sub, school=school, section=section)
        else:
            g_plv = '—'

        gender_rows.append({
            'label': gender_label,
            'dist': g_dist,
            'entries': g_entries,
            'mean_score': g_mean,
            'mean_points': g_pts,
            'performance_text': g_plv,
        })

    # Section accent color based on exam section
    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    sec = exam.school_section or 'JSS'
    if sec == 'PRIMARY' and exam.sub_section == 'LOWER':
        section_accent = section_colors['LOWER_PRIMARY']
    elif sec == 'PRIMARY':
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors.get(sec, '#305CDE')

    return {
        'broadsheet': broadsheet,
        'published_subjects': published_subjects,
        'ordered_levels': active_levels,
        'student_count': len(broadsheet),
        'selected_year': exam.year,
        'selected_term': exam.term,
        'selected_exam': exam.name,
        'is_primary': is_primary,
        'analysis_rows': analysis_rows,
        'grade_breakdown_rows': grade_breakdown_rows,
        'gender_rows': gender_rows,
        'section_accent': section_accent,
    }