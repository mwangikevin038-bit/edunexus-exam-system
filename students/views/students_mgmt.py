"""
Student Management Views
========================
Handles student registration, class lists, and admin
student management hub with overview, manual entry, directory, and
promotion sub-sections.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.shortcuts import redirect, render

from .constants import GRADE_CHOICES, TERM_CHOICES, get_streams_for_school
from ..forms import StudentForm
from .helpers import (
    get_class_teacher_scope,
    get_learner_contexts_for_user,
    get_next_admission_no,
    get_teacher_for_user,
)
from ..models import Guardian, RemovedStudent, Student
from ..security import get_request_school, get_request_school_section, school_admin_required, user_has_main_school_admin_override

PRIMARY_GRADE_CHOICES = ['Grade 4', 'Grade 5', 'Grade 6']
LOWER_PRIMARY_GRADE_CHOICES = ['Grade 1', 'Grade 2', 'Grade 3']


def _derive_sub_section(class_name):
    """Derive sub_section from class_name. Grade 1-3 → LOWER, Grade 4-6 → UPPER, Grade 7-9 → None."""
    if class_name in ('Grade 1', 'Grade 2', 'Grade 3'):
        return 'LOWER'
    if class_name in ('Grade 4', 'Grade 5', 'Grade 6'):
        return 'UPPER'
    return None


@login_required(login_url='login')
def api_streams_for_grade(request):
    """JSON endpoint: returns streams for a specific grade in the school."""
    from ..models import Stream, Grade
    school = get_request_school(request)
    if not school:
        return JsonResponse({'streams': []})

    grade_name = request.GET.get('grade', '').strip()
    if not grade_name:
        return JsonResponse({'streams': []})

    try:
        grade = Grade.all_objects.get(school=school, name=grade_name)
    except Grade.DoesNotExist:
        return JsonResponse({'streams': []})

    stream_names = list(
        Stream.all_objects.filter(school=school, grade=grade)
        .values_list('name', flat=True)
        .distinct()
        .order_by('name')
    )
    return JsonResponse({'streams': stream_names})


@login_required(login_url='login')
@school_admin_required
def add_student(request):
    """
    Basic student registration form (teacher-facing).
    Auto-assigns the next sequential admission number.
    """
    school = get_request_school(request)
    next_admission_no = get_next_admission_no()
    school_section = get_request_school_section(request) or 'JSS'

    if request.method == 'POST':
        data = request.POST.copy()
        data['admission_no'] = next_admission_no
        form = StudentForm(data, school=school, school_section=school_section)

        if form.is_valid():
            student_instance = form.save(commit=False)
            student_instance.school = school
            student_instance.school_section = school_section
            student_instance.sub_section = _derive_sub_section(student_instance.class_name)
            guardian_obj, _ = Guardian.objects.get_or_create(
                school=school,
                phone=form.cleaned_data['guardian_phone'],
                defaults={
                    'name':        form.cleaned_data['guardian_name'],
                    'school_section': school_section,
                }
            )
            student_instance.guardian = guardian_obj
            student_instance.religion = form.cleaned_data.get('religion')
            student_instance.save()
            messages.success(request, f'Student registered successfully! Admission No: {next_admission_no}')
            return redirect('add_student')

        messages.error(request, 'Registration failed. Please fill in all required fields correctly.')

    else:
        form = StudentForm(school=school, school_section=school_section, initial={'admission_no': next_admission_no})

    section = get_request_school_section(request) or 'JSS'
    grades_for_section = LOWER_PRIMARY_GRADE_CHOICES if section == 'LOWER_PRIMARY' else PRIMARY_GRADE_CHOICES if section == 'PRIMARY' else GRADE_CHOICES

    return render(request, 'students/add_student.html', {
        'form':             form,
        'next_admission_no': next_admission_no,
        'grades':           grades_for_section,
    })


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_add_student(request):
    """
    Admin-facing student management hub with five sub-sections:
      Overview | Manual Entry | Bulk CSV Upload | Directory | Promotions
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    current_year      = datetime.date.today().year
    active_tab        = request.GET.get('tab', 'directory')
    next_admission_no = get_next_admission_no()
    # Store the raw integer for bulk increment calculations
    try:
        next_no = int(next_admission_no)
    except ValueError:
        next_no = 1

    # --------------------------------------------------------------------------
    # POST — Action routing
    # --------------------------------------------------------------------------
    if request.method == 'POST':
        mode = request.POST.get('registration_mode')

        # --- Single manual registration ---
        if mode == 'single':
            submitted_adm = request.POST.get('admission_no', '').strip()
            adm_no = submitted_adm if submitted_adm else next_admission_no

            if Student.objects.filter(school=school, admission_no=adm_no).exists():
                messages.error(request, f"❌ Admission Number '{adm_no}' is already taken.")
                return redirect('/school-admin/registration/?tab=add_student')

            name       = request.POST.get('name', '').strip()
            class_name = request.POST.get('class_name', '').strip()
            stream     = request.POST.get('stream', '').strip()
            religion   = request.POST.get('religion', 'None').strip() or 'None'
            gender     = request.POST.get('gender', 'Not Specified').strip() or 'Not Specified'
            g_name     = request.POST.get('guardian_name', '').strip()
            g_phone    = request.POST.get('guardian_phone', '').strip()

            # Auto-fill term and year from system
            current_month = datetime.date.today().month
            if 1 <= current_month <= 4:
                term = 'Term 1'
            elif 5 <= current_month <= 8:
                term = 'Term 2'
            else:
                term = 'Term 3'
            year = current_year

            try:
                section = get_request_school_section(request)
                guardian_obj, _ = Guardian.objects.get_or_create(
                    school=school,
                    phone=g_phone,
                    defaults={'name': g_name or 'Unknown', 'school_section': section or 'JSS'}
                )
                Student.objects.create(
                    school=school,
                    admission_no=adm_no,
                    assessment_no=request.POST.get('assessment_no', '').strip(),
                    name=name,
                    class_name=class_name,
                    stream=stream,
                    term=term,
                    year=int(year),
                    guardian=guardian_obj,
                    religion=religion,
                    gender=gender,
                    school_section=section or 'JSS',
                    sub_section=_derive_sub_section(class_name),
                )
                messages.success(request, f"✓ {name} enrolled into {class_name} {stream}. ADM: {adm_no}")

                # Handle Save and Exit vs Next button
                action = request.POST.get('action', 'save_exit')
                if action == 'next':
                    return redirect('/school-admin/registration/?tab=add_student')
                else:
                    return redirect('/school-admin/registration/?tab=directory')
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Student admission failed for name=%s", name)
                messages.error(request, "An error occurred during admission. Please try again.")
                return redirect('/school-admin/registration/?tab=add_student')

        # --- Restore removed student ---
        elif mode == 'restore_student':
            removed_id = request.POST.get('removed_student_id')
            if removed_id:
                try:
                    removed = RemovedStudent.objects.get(pk=removed_id, school=school)
                    # Determine admission_no — if conflict, bump
                    adm_no = removed.admission_no
                    if Student.objects.filter(school=school, admission_no=adm_no).exists():
                        base = adm_no.rstrip('ABCD')
                        for letter in 'ABCD':
                            candidate = base + letter
                            if not Student.objects.filter(school=school, admission_no=candidate).exists():
                                adm_no = candidate
                                break
                    # Recreate guardian
                    guardian_obj, _ = Guardian.objects.get_or_create(
                        school=school,
                        phone=removed.guardian_phone,
                        defaults={'name': removed.guardian_name or 'Unknown', 'school_section': removed.school_section or 'JSS'}
                    )
                    Student.objects.create(
                        school=school,
                        admission_no=adm_no,
                        assessment_no=removed.assessment_no or '',
                        name=removed.name,
                        class_name=removed.class_name,
                        stream=removed.stream,
                        term=removed.term,
                        year=removed.year,
                        guardian=guardian_obj,
                        religion=removed.religion or 'None',
                        gender=removed.gender or 'Not Specified',
                        school_section=removed.school_section or 'JSS',
                        sub_section=removed.sub_section,
                    )
                    removed.delete()
                    messages.success(request, f"✓ {removed.name} has been restored to {removed.class_name} {removed.stream}.")
                except RemovedStudent.DoesNotExist:
                    messages.error(request, "Removed student record not found.")
            return redirect('/school-admin/registration/?tab=removed_students')

        # --- Mass promotion / graduation ---
        elif mode == 'promote':
            source_class = request.POST.get('source_class')
            target_class = request.POST.get('target_class')
            confirm = request.POST.get('confirm_delete')
            if source_class and target_class:
                # Validate source_class belongs to current workspace section
                if section == 'LOWER_PRIMARY' and source_class not in LOWER_PRIMARY_GRADE_CHOICES:
                    messages.error(request, f"{source_class} is not in your Lower Primary workspace.")
                    return redirect('/school-admin/registration/?tab=overview')
                elif section == 'PRIMARY' and source_class not in PRIMARY_GRADE_CHOICES:
                    messages.error(request, f"{source_class} is not in your Upper Primary workspace.")
                    return redirect('/school-admin/registration/?tab=overview')
                cohort        = Student.objects.filter(school=school, class_name=source_class)
                affected_count = cohort.count()
                if affected_count > 0:
                    if target_class == 'Graduate/Exit':
                        if confirm != 'yes':
                            messages.warning(request, f"Are you sure you want to graduate/exit {affected_count} students from {source_class}? Submit again with confirmation.")
                            return redirect('/school-admin/registration/?tab=overview')
                        cohort.update(is_active=False, class_name='Graduated')
                        messages.success(request, f"🎓 {affected_count} students graduated from {source_class}.")
                    else:
                        cohort.update(class_name=target_class)
                        messages.success(request, f"🚀 {affected_count} students promoted to {target_class}.")
                else:
                    messages.warning(request, f"⚠️ No students found in {source_class}.")
            return redirect('/school-admin/registration/?tab=overview')

    # --------------------------------------------------------------------------
    # GET — Build query and context
    # --------------------------------------------------------------------------
    section = get_request_school_section(request)
    grades_for_section = LOWER_PRIMARY_GRADE_CHOICES if section == 'LOWER_PRIMARY' else PRIMARY_GRADE_CHOICES if section == 'PRIMARY' else GRADE_CHOICES

    tab = active_tab
    query = request.GET.get('q', '').strip()
    search_type = request.GET.get('search_type', 'adm_no')

    context = {
        'active_tab':       tab,
        'next_admission_no': next_admission_no,
        'grades':           grades_for_section,
        'streams':          get_streams_for_school(school, section),
        'terms':            TERM_CHOICES,
        'current_year':     current_year,
        'total_students':   Student.objects.filter(school=school).count(),
        'guardian_count':   Guardian.objects.filter(school=school).count(),
        'query':            query,
        'search_type':      search_type,
        'search_label':     'Admission Number',
        'students':         Student.objects.none(),
        'move_students':    Student.objects.none(),
        'move_grade':       '',
        'move_stream':      '',
        'move_class_label': '',
        'removed_students': RemovedStudent.objects.none(),
        'removed_query':    '',
    }
    for i, g in enumerate(grades_for_section[:3]):
        context[f'grade_{i+1}_name'] = g
        context[f'grade_{i+1}_count'] = Student.objects.filter(school=school, class_name=g).count()

    # 1. HANDLE DIRECTORY SEARCH STATE
    if tab == 'directory':
        search_qs = Student.all_objects.filter(school=school).select_related('guardian')
        if query:
            if search_type == 'adm_no':
                students = search_qs.filter(admission_no__icontains=query).order_by('name')
            elif search_type == 'name':
                students = search_qs.filter(name__icontains=query).order_by('name')
            elif search_type == 'phone':
                students = search_qs.filter(guardian__phone__icontains=query).order_by('name')
            elif search_type == 'assessment_no':
                students = search_qs.filter(assessment_no__icontains=query).order_by('name')
            else:
                students = search_qs.filter(
                    Q(admission_no__icontains=query) | Q(name__icontains=query)
                ).order_by('name')
        else:
            students = search_qs.none()
        context['students'] = students

    # 2. HANDLE STUDENT PROFILE LOOKUP STATE
    elif tab == 'profile':
        student_id = request.GET.get('id')
        if student_id:
            try:
                student = Student.all_objects.select_related('guardian').get(pk=student_id, school=school)
            except Student.DoesNotExist:
                student = None
            context['student'] = student

    # 3. HANDLE MOVE STUDENTS STATE
    elif tab == 'move_student':
        move_grade = request.GET.get('grade', '').strip()
        move_stream = request.GET.get('stream', '').strip()
        context['move_grade'] = move_grade
        context['move_stream'] = move_stream
        if move_grade and move_stream:
            move_students = Student.all_objects.filter(
                school=school, class_name__iexact=move_grade, stream__iexact=move_stream
            ).order_by('name')
            context['move_students'] = move_students
            context['move_class_label'] = f"{move_grade} {move_stream}"

    # 4. HANDLE REMOVED STUDENTS STATE
    elif tab == 'removed_students':
        removed_query = request.GET.get('q', '').strip()
        removed_qs = RemovedStudent.objects.filter(school=school).select_related('removed_by')
        if removed_query:
            removed_qs = removed_qs.filter(
                Q(name__icontains=removed_query) | Q(admission_no__icontains=removed_query)
            )
        context['removed_students'] = removed_qs
        context['removed_query'] = removed_query

    return render(request, 'students/admin_add_student.html', context)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_search_fields(request):
    """HTMX partial: returns the search input field matching the selected radio type."""
    search_type = request.GET.get('type', 'adm_no')
    school = get_request_school(request)

    if search_type == 'name':
        grades = []
        if school:
            from .constants import GRADE_CHOICES
            grades = GRADE_CHOICES
        grade_options = '<option value="">Form / Grade</option>'
        for g in grades:
            grade_options += f'<option value="{g}">{g}</option>'
        html = (
            '<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 4px; width: 100%;">'
            '  <div style="display: flex; flex-direction: column; gap: 6px;">'
            '    <label style="font-size: 13px; font-weight: 600; color: #0f172a;">Name<span style="color: #ef4444; margin-left: 2px;">*</span></label>'
            '    <input type="text" name="q" class="search-input" placeholder="Name" required>'
            '  </div>'
            '  <div style="display: flex; flex-direction: column; gap: 6px;">'
            '    <label style="font-size: 13px; font-weight: 600; color: #475569;">Form / Grade</label>'
            '    <div style="position: relative; width: 100%;">'
            f'      <select name="form_grade" class="search-input" style="appearance: none; -webkit-appearance: none; padding-right: 32px; cursor: pointer;">'
            f'        {grade_options}'
            '      </select>'
            '      <i class="ph ph-caret-down" style="position: absolute; right: 12px; top: 50%; transform: translateY(-50%); color: #64748b; pointer-events: none;"></i>'
            '    </div>'
            '  </div>'
            '</div>'
        )
    else:
        field_map = {
            'adm_no': {'label': 'Admission Number', 'placeholder': 'Admission Number'},
            'phone': {'label': 'Phone Number', 'placeholder': 'Phone Number'},
            'assessment_no': {'label': 'Assessment Number', 'placeholder': 'Assessment Number'},
        }
        field = field_map.get(search_type, field_map['adm_no'])
        html = (
            '<div style="display: flex; flex-direction: column; gap: 6px; margin-top: 4px; width: 100%;">'
            f'<label style="font-size: 13px; font-weight: 600; color: #0f172a;">{field["label"]}<span style="color: #ef4444; margin-left: 2px;">*</span></label>'
            f'<input type="text" name="q" class="search-input" placeholder="{field["placeholder"]}" required>'
            '</div>'
        )
    return HttpResponse(html)


@login_required(login_url='login')
def class_lists(request):
    """
    Role-aware learner directory. Admins see every stream, class teachers see
    their class stream, and subject teachers see streams they are assigned to.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    view_mode       = request.GET.get('view_mode', 'teacher')
    if view_mode not in ('teacher', 'admin'):
        view_mode = 'teacher'

    teacher = get_teacher_for_user(request.user)
    class_teacher_scope = get_class_teacher_scope(teacher)
    is_admin_view = user_has_main_school_admin_override(request.user)
    contexts = get_learner_contexts_for_user(request.user)
    selected_key = request.GET.get('context')
    selected_context = None
    if selected_key:
        selected_context = next((item for item in contexts if item['context_key'] == selected_key), None)
    if not selected_context and contexts:
        selected_context = contexts[0]

    selected_grade = selected_context['class_name'] if selected_context else None
    selected_stream = selected_context['stream'] if selected_context else None
    can_access_admin_register = (
        is_admin_view or
        (class_teacher_scope == (selected_grade, selected_stream))
    )
    if view_mode == "admin" and not can_access_admin_register:
        view_mode = "teacher"

    students = Student.objects.none()
    if selected_context:
        # Admin view uses all_objects to see students across all sub-sections
        # (e.g. Grade 3 LOWER alongside Grade 4-6 UPPER in PRIMARY workspace).
        student_manager = Student.all_objects if is_admin_view else Student.objects
        students = (
            student_manager
            .filter(school=school, class_name=selected_grade, stream=selected_stream)
            .filter(admission_no__regex=r'^[0-9]+$')
            .select_related('guardian')
            .annotate(adm_int=Cast('admission_no', IntegerField()))
            .order_by('adm_int')
        )

    section = get_request_school_section(request)
    if section == 'LOWER_PRIMARY':
        grades_for_section = LOWER_PRIMARY_GRADE_CHOICES
    elif section == 'PRIMARY':
        grades_for_section = LOWER_PRIMARY_GRADE_CHOICES + PRIMARY_GRADE_CHOICES
    else:
        grades_for_section = GRADE_CHOICES

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    # Use LOWER_PRIMARY color when a Grade 1-3 stream is selected
    if selected_grade in LOWER_PRIMARY_GRADE_CHOICES:
        section_accent = section_colors['LOWER_PRIMARY']
    else:
        section_accent = section_colors.get(section, '#305CDE')

    return render(request, 'students/class_lists.html', {
        'students':         students,
        'selected_grade':   selected_grade,
        'selected_stream':  selected_stream,
        'selected_context_key': selected_context['context_key'] if selected_context else '',
        'learner_contexts': contexts,
        'current_view_mode': view_mode,
        'can_access_admin_register': can_access_admin_register,
        'is_admin_view': is_admin_view,
        'section_accent':   section_accent,
        'access_label': "School-wide learner directory" if is_admin_view else "Assigned learner directory",
        'grades':           grades_for_section,
        'streams':          get_streams_for_school(school, section),
    })
