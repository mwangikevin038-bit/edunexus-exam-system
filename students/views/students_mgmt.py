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
from django.db.models.functions import Cast, Length, Substr
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
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
    school_section = get_request_school_section(request) or 'JSS'
    next_admission_no = get_next_admission_no(school_section=school_section)

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
    school_section    = get_request_school_section(request) or 'JSS'
    next_admission_no = get_next_admission_no(school_section=school_section)
    # Store the raw integer for bulk increment calculations
    try:
        next_no = int(next_admission_no[:-1])
    except (ValueError, IndexError):
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

            # Check both active AND inactive students to prevent unique constraint violations
            existing = Student.all_objects.filter(school=school, admission_no=adm_no).first()
            if existing:
                if existing.is_active:
                    messages.error(request, f"❌ Admission Number '{adm_no}' is already taken.")
                else:
                    messages.warning(request, f"⚠️ Admission Number '{adm_no}' belongs to a removed student ({existing.name}). Use Re-admit to restore them instead.")
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
                section = request.POST.get('school_section', '').strip() or 'JSS'
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
                if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
                    messages.error(request, f"❌ Admission number '{adm_no}' conflicts with an existing record. If this student was removed, use Re-admit from the Removed Students tab.")
                else:
                    messages.error(request, "An error occurred during admission. Please try again.")
                return redirect('/school-admin/registration/?tab=add_student')

        # --- Restore removed student ---
        elif mode == 'restore_student':
            removed_id = request.POST.get('removed_student_id')
            restore_source = request.POST.get('restore_source', 'student')
            if removed_id:
                try:
                    if restore_source == 'student':
                        # New soft-delete approach: just flip flags
                        student = Student.all_objects.get(pk=removed_id, school=school, is_active=False, status='Removed')
                        student.is_active = True
                        student.status = 'Active'
                        student.date_removed = None
                        student.deleted_by = None
                        student.save(update_fields=['is_active', 'status', 'date_removed', 'deleted_by'])
                        messages.success(request, f"✓ {student.name} has been restored to {student.class_name} {student.stream}.")
                    else:
                        # Legacy RemovedStudent table
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
                except (Student.DoesNotExist, RemovedStudent.DoesNotExist):
                    messages.error(request, "Removed student record not found.")
            return redirect('/school-admin/registration/?tab=removed_students')

        # --- Mass promotion / graduation / move ---
        elif mode == 'promote':
            source_class = request.POST.get('source_class')
            confirm = request.POST.get('confirm')
            target_class = request.POST.get('target_class', '').strip()
            target_stream = request.POST.get('target_stream', '').strip()
            student_ids = request.POST.get('student_ids', '').strip()

            if student_ids:
                # Move selected students to specific destination
                id_list = [int(i) for i in student_ids.split(',') if i.strip().isdigit()]
                cohort = Student.all_objects.filter(school=school, id__in=id_list, is_active=True)
                affected_count = cohort.count()
                if affected_count > 0:
                    if confirm != 'yes':
                        dest = f"{target_class} {target_stream}" if target_stream else target_class
                        messages.warning(request, f"Move {affected_count} selected students to {dest}? Submit again with confirmation.")
                        return redirect('/school-admin/registration/?tab=move_student')
                    if target_class == 'Graduated':
                        from django.utils import timezone
                        cohort.update(is_active=False, status='Graduated', class_name='Graduated', date_removed=timezone.now())
                        messages.success(request, f"🎓 {affected_count} students graduated.")
                    else:
                        update_fields = {'class_name': target_class}
                        if target_stream:
                            update_fields['stream'] = target_stream
                        cohort.update(**update_fields)
                        dest = f"{target_class} {target_stream}" if target_stream else target_class
                        messages.success(request, f"✅ {affected_count} students moved to {dest}.")
                else:
                    messages.warning(request, f"⚠️ No valid students selected.")
            elif source_class:
                # Promote/graduate all in source class
                cohort = Student.all_objects.filter(school=school, class_name=source_class, is_active=True)
                affected_count = cohort.count()
                if affected_count > 0:
                    grade_num = int(source_class.replace('Grade ', ''))
                    if grade_num >= 9:
                        # Graduate: remove from active
                        if confirm != 'yes':
                            messages.warning(request, f"Are you sure you want to graduate {affected_count} students from {source_class}? Submit again with confirmation.")
                            return redirect('/school-admin/registration/?tab=move_student')
                        from django.utils import timezone
                        cohort.update(is_active=False, status='Graduated', class_name='Graduated', date_removed=timezone.now())
                        messages.success(request, f"🎓 {affected_count} students graduated from {source_class}.")
                    else:
                        # Promote to next grade
                        next_grade = f"Grade {grade_num + 1}"
                        if confirm != 'yes':
                            messages.warning(request, f"Promote {affected_count} students from {source_class} to {next_grade}? Submit again with confirmation.")
                            return redirect('/school-admin/registration/?tab=move_student')
                        cohort.update(class_name=next_grade)
                        messages.success(request, f"🚀 {affected_count} students promoted from {source_class} to {next_grade}.")
                else:
                    messages.warning(request, f"⚠️ No students found in {source_class}.")
            return redirect('/school-admin/registration/?tab=move_student')

    # --------------------------------------------------------------------------
    # GET — Build query and context
    # --------------------------------------------------------------------------
    section = get_request_school_section(request)
    grades_for_section = LOWER_PRIMARY_GRADE_CHOICES if section == 'LOWER_PRIMARY' else PRIMARY_GRADE_CHOICES if section == 'PRIMARY' else GRADE_CHOICES

    tab = active_tab
    search_type = request.GET.get('search_type', 'adm_no')

    # Extract query from the mode-specific input field
    query = ''
    if search_type == 'adm_no':
        query = request.GET.get('admission_number', '').strip()
    elif search_type == 'name':
        query = request.GET.get('name', '').strip()
    elif search_type == 'phone':
        query = request.GET.get('phone_number', '').strip()
    elif search_type == 'assessment_no':
        query = request.GET.get('assessment_number', '').strip()
    else:
        query = request.GET.get('q', '').strip()

    context = {
        'active_tab':       tab,
        'next_admission_no': next_admission_no,
        'school_section':   'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS',
        'grades':           grades_for_section,
        'move_grades':      GRADE_CHOICES,
        'streams':          get_streams_for_school(school, section),
        'all_streams':      get_streams_for_school(school),
        'terms':            TERM_CHOICES,
        'current_year':     current_year,
        'total_students':   Student.objects.filter(school=school, is_active=True).count(),
        'guardian_count':   Guardian.objects.filter(school=school).count(),
        'query':            query,
        'search_type':      search_type,
        'search_label':     {'adm_no': 'Admission Number', 'name': 'Name', 'phone': 'Phone Number', 'assessment_no': 'Assessment Number'}.get(search_type, 'Admission Number'),
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
        context[f'grade_{i+1}_count'] = Student.objects.filter(school=school, class_name=g, is_active=True).count()

    # 1. HANDLE DIRECTORY SEARCH STATE
    if tab == 'directory':
        search_qs = Student.all_objects.filter(school=school, is_active=True).select_related('guardian')
        grade_query = request.GET.get('grade', '').strip() if search_type == 'name' else ''

        if query:
            import re
            format_ok = True
            if search_type == 'name':
                if not re.match(r'^[A-Za-z\s.\'-]+$', query):
                    format_ok = False
            elif search_type in ('adm_no', 'assessment_no'):
                if not re.match(r'^[A-Za-z0-9]+$', query):
                    format_ok = False
            elif search_type == 'phone':
                if not re.match(r'^[+]?[0-9\s\-()]+$', query):
                    format_ok = False

            if not format_ok:
                messages.warning(request, f"Invalid format for {dict(adm_no='Admission Number', name='Name', phone='Phone Number', assessment_no='Assessment Number').get(search_type, search_type)}. Please enter a valid value.")
                students = search_qs.none()
            elif search_type == 'adm_no':
                students = search_qs.filter(admission_no__icontains=query).order_by('name')
            elif search_type == 'name':
                students = search_qs.filter(name__icontains=query).order_by('name')
                if grade_query:
                    students = students.filter(class_name__iexact=grade_query)
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
        try:
            context['move_grade_num'] = int(move_grade.replace('Grade ', ''))
        except (ValueError, AttributeError):
            context['move_grade_num'] = 0
        if move_grade and move_stream:
            move_students = Student.all_objects.filter(
                school=school, class_name__iexact=move_grade, stream__iexact=move_stream, is_active=True
            )
            move_students = move_students.annotate(
                adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField())
            ).order_by('adm_int')
            context['move_students'] = move_students
            context['move_class_label'] = f"{move_grade} {move_stream}"

    # 4. HANDLE REMOVED STUDENTS STATE
    elif tab == 'removed_students':
        removed_query = request.GET.get('q', '').strip()

        # Soft-deleted students (new approach)
        soft_deleted = Student.all_objects.filter(
            school=school, is_active=False, status='Removed'
        ).select_related('deleted_by', 'guardian').values(
            'id', 'admission_no', 'name', 'class_name', 'stream',
            'gender', 'religion', 'assessment_no', 'school_section',
            'sub_section', 'term', 'year', 'date_removed',
            'deleted_by__first_name', 'deleted_by__last_name',
            'guardian__name', 'guardian__phone',
        )
        # Legacy RemovedStudent records
        legacy_removed = RemovedStudent.objects.filter(
            school=school
        ).select_related('removed_by').values(
            'id', 'admission_no', 'name', 'class_name', 'stream',
            'gender', 'religion', 'assessment_no', 'school_section',
            'sub_section', 'term', 'year', 'removed_at',
            'removed_by__first_name', 'removed_by__last_name',
            'guardian_name', 'guardian_phone',
        )

        # Normalize both into a common format for the template
        removed_list = []
        for s in soft_deleted:
            removed_list.append({
                'id': s['id'],
                'admission_no': s['admission_no'],
                'name': s['name'],
                'class_name': s['class_name'],
                'stream': s['stream'],
                'gender': s['gender'] or 'Not Specified',
                'religion': s['religion'] or 'None',
                'assessment_no': s['assessment_no'] or '',
                'school_section': s['school_section'] or 'JSS',
                'sub_section': s['sub_section'],
                'term': s['term'] or 'Term 1',
                'year': s['year'],
                'removed_at': s['date_removed'],
                'removed_by_name': f"{s['deleted_by__first_name'] or ''} {s['deleted_by__last_name'] or ''}".strip() or 'System',
                'guardian_name': s['guardian__name'] or '',
                'guardian_phone': s['guardian__phone'] or '',
                'source': 'student',
            })
        for r in legacy_removed:
            removed_list.append({
                'id': r['id'],
                'admission_no': r['admission_no'],
                'name': r['name'],
                'class_name': r['class_name'],
                'stream': r['stream'],
                'gender': r['gender'] or 'Not Specified',
                'religion': r['religion'] or 'None',
                'assessment_no': r['assessment_no'] or '',
                'school_section': r['school_section'] or 'JSS',
                'sub_section': r['sub_section'],
                'term': r['term'] or 'Term 1',
                'year': r['year'],
                'removed_at': r['removed_at'],
                'removed_by_name': f"{r['removed_by__first_name'] or ''} {r['removed_by__last_name'] or ''}".strip() or 'System',
                'guardian_name': r['guardian_name'] or '',
                'guardian_phone': r['guardian_phone'] or '',
                'source': 'removed_student',
            })

        # Filter by search query
        if removed_query:
            removed_list = [r for r in removed_list if removed_query.lower() in r['name'].lower() or removed_query.lower() in r['admission_no'].lower()]

        # Sort by removed_at descending
        removed_list.sort(key=lambda x: x['removed_at'] or '', reverse=True)

        context['removed_students'] = removed_list
        context['removed_query'] = removed_query

    return render(request, 'students/admin_add_student.html', context)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_search_fields(request):
    """HTMX partial: returns the search input field matching the selected radio type."""
    search_type = request.GET.get('type', 'adm_no')
    school = get_request_school(request)

    INPUT_STYLE = 'width: 100%; padding: 10px; border: 1px solid #d1d5db; border-radius: 6px; box-sizing: border-box; background-color: #f0fdf4; font-size: 14px;'
    LABEL_STYLE = 'color: #ea580c; font-size: 14px; font-weight: 500;'
    WRAP_STYLE = 'display: flex; flex-direction: column; gap: 6px;'

    if search_type == 'name':
        grades = []
        if school:
            from .constants import GRADE_CHOICES
            grades = GRADE_CHOICES
        grade_options = '<option value="">Form / Grade</option>'
        for g in grades:
            grade_options += f'<option value="{g}">{g}</option>'
        html = (
            f'<div style="grid-column: span 1; {WRAP_STYLE}">'
            f'  <label style="{LABEL_STYLE}">Name<span style="color: #ef4444;">*</span></label>'
            f'  <input type="text" name="name" placeholder="Enter Student Name..." required'
            f"         pattern=\"^[A-Za-z\\s.'-]+\" title=\"Please enter text letters only for names\""
            f'         style="{INPUT_STYLE}">'
            f'</div>'
            f'<div style="grid-column: span 1; {WRAP_STYLE}">'
            f'  <label style="color: #374151; font-size: 14px; font-weight: 500;">Form / Grade</label>'
            f'  <select name="grade"'
            f'          style="{INPUT_STYLE} color: #9ca3af;">'
            f'    {grade_options}'
            f'  </select>'
            f'</div>'
        )
    else:
        field_map = {
            'adm_no':         {'label': 'Admission Number',  'placeholder': 'Enter Admission Number...',  'name': 'admission_number', 'type': 'text',  'pattern': '^[A-Za-z0-9]+$',            'title': 'Admission number must be alphanumeric (e.g. ADM001)'},
            'phone':          {'label': 'Phone Number',      'placeholder': 'Enter Phone Number...',      'name': 'phone_number',     'type': 'tel',  'pattern': '^[+]?[0-9\\s\\-()]+$',       'title': 'Please enter a valid phone number (digits, +, -, spaces, parentheses)'},
            'assessment_no':  {'label': 'Assessment Number', 'placeholder': 'Enter Assessment Number...', 'name': 'assessment_number','type': 'text', 'pattern': '^[A-Za-z0-9]+$',             'title': 'Assessment number must be alphanumeric (e.g. ASS001)'},
        }
        f = field_map.get(search_type, field_map['adm_no'])
        html = (
            f'<div style="grid-column: span 2; {WRAP_STYLE}">'
            f'  <label style="{LABEL_STYLE}">{f["label"]}<span style="color: #ef4444;">*</span></label>'
            f'  <input type="{f["type"]}" name="{f["name"]}" placeholder="{f["placeholder"]}" required'
            f'         pattern="{f["pattern"]}" title="{f["title"]}"'
            f'         style="{INPUT_STYLE}">'
            f'</div>'
        )
    return HttpResponse(html)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_search_submit(request):
    """HTMX endpoint: run the directory search and return results HTML fragment."""
    import re
    from django.db.models import Q

    school = get_request_school(request)
    search_type = request.GET.get('search_type', 'adm_no')

    query = ''
    if search_type == 'adm_no':
        query = request.GET.get('admission_number', '').strip()
    elif search_type == 'name':
        query = request.GET.get('name', '').strip()
    elif search_type == 'phone':
        query = request.GET.get('phone_number', '').strip()
    elif search_type == 'assessment_no':
        query = request.GET.get('assessment_number', '').strip()

    grade_query = request.GET.get('grade', '').strip() if search_type == 'name' else ''

    students = Student.objects.none()
    if query:
        format_ok = True
        if search_type == 'name':
            if not re.match(r"^[A-Za-z\s.'-]+$", query):
                format_ok = False
        elif search_type in ('adm_no', 'assessment_no'):
            if not re.match(r'^[A-Za-z0-9]+$', query):
                format_ok = False
        elif search_type == 'phone':
            if not re.match(r'^[+]?[0-9\s\-()]+$', query):
                format_ok = False

        if format_ok:
            search_qs = Student.all_objects.filter(school=school, is_active=True).select_related('guardian')
            if search_type == 'adm_no':
                students = search_qs.filter(admission_no__icontains=query).order_by('name')
            elif search_type == 'name':
                students = search_qs.filter(name__icontains=query).order_by('name')
                if grade_query:
                    students = students.filter(class_name__iexact=grade_query)
            elif search_type == 'phone':
                students = search_qs.filter(guardian__phone__icontains=query).order_by('name')
            elif search_type == 'assessment_no':
                students = search_qs.filter(assessment_no__icontains=query).order_by('name')

    TYPE_LABELS = {'adm_no': 'Admission Number', 'name': 'Name', 'phone': 'Phone Number', 'assessment_no': 'Assessment Number'}
    label = TYPE_LABELS.get(search_type, 'Admission Number')

    if students.exists():
        rows = ''
        for idx, s in enumerate(students, 1):
            rows += (
                '<tr style="background:#ffffff;">'
                f'  <td style="padding:14px 16px;font-size:14px;font-weight:600;color:#0f172a;border:1px solid #e2e8f0;">{idx}</td>'
                f'  <td style="padding:14px 16px;font-size:14px;font-weight:500;color:#334155;border:1px solid #e2e8f0;">{s.admission_no}</td>'
                f'  <td style="padding:14px 16px;font-size:14px;font-weight:500;color:#334155;border:1px solid #e2e8f0;">{s.name}</td>'
                f'  <td style="padding:14px 16px;font-size:14px;font-weight:500;color:#334155;border:1px solid #e2e8f0;">{s.class_name} {s.stream}</td>'
                '  <td style="padding:12px 16px;text-align:center;border:1px solid #e2e8f0;">'
                '    <div style="display:flex;gap:12px;justify-content:center;align-items:center;">'
                f'      <a hx-get="/school-admin/registration/profile/{s.id}/" hx-target="#search-card-container" hx-swap="innerHTML"'
                '         style="display:inline-block;background:#0f172a;color:#ffffff;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;font-family:\'Inter\',sans-serif;transition:all 0.2s;">Profile</a>'
                f'      <a hx-get="/school-admin/registration/analytics/{s.id}/" hx-target="#search-card-container" hx-swap="innerHTML"'
                '         style="display:inline-block;background:#22c55e;color:#ffffff;padding:8px 20px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;font-family:\'Inter\',sans-serif;transition:all 0.2s;">Analytics</a>'
                '    </div>'
                '  </td>'
                '</tr>'
            )
        html = (
            '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #22c55e;">'
            '  <h3 style="margin:0 0 20px;color:#0f172a;font-size:18px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Search Results</h3>'

            '  <div style="overflow-x:auto;width:100%;margin-bottom:20px;">'
            '    <table style="width:100%;border-collapse:collapse;text-align:left;">'
            '      <thead>'
            '        <tr style="background:#f8fafc;">'
            '          <th style="padding:12px 16px;font-size:12px;font-weight:700;color:#0f172a;width:50px;border:1px solid #e2e8f0;text-transform:uppercase;letter-spacing:0.5px;">#</th>'
            '          <th style="padding:12px 16px;font-size:12px;font-weight:700;color:#0f172a;width:120px;border:1px solid #e2e8f0;text-transform:uppercase;letter-spacing:0.5px;">ADM NO</th>'
            '          <th style="padding:12px 16px;font-size:12px;font-weight:700;color:#0f172a;border:1px solid #e2e8f0;text-transform:uppercase;letter-spacing:0.5px;">NAME</th>'
            '          <th style="padding:12px 16px;font-size:12px;font-weight:700;color:#0f172a;width:180px;border:1px solid #e2e8f0;text-transform:uppercase;letter-spacing:0.5px;">CLASS</th>'
            '          <th style="padding:12px 16px;font-size:12px;font-weight:700;color:#0f172a;width:220px;text-align:center;border:1px solid #e2e8f0;text-transform:uppercase;letter-spacing:0.5px;">ACTIONS</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{rows}</tbody>'
            '    </table>'
            '  </div>'

            '  <div style="display:flex;justify-content:flex-start;">'
            '    <button hx-get="/school-admin/registration/search-reset/" hx-target="#search-card-container" hx-swap="innerHTML"'
            '            style="display:inline-flex;align-items:center;gap:6px;background:#f1f5f9;border:1px solid #cbd5e1;color:#334155;padding:10px 22px;border-radius:8px;font-weight:600;font-size:13px;cursor:pointer;font-family:\'Inter\',sans-serif;transition:all 0.2s ease;">'
            '      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/></svg>'
            '      Back'
            '    </button>'
            '  </div>'
            '</div>'
        )
    else:
        html = (
            '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #22c55e;">'
            '  <h3 style="margin:0 0 20px;color:#0f172a;font-size:18px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Search Results</h3>'
            '  <div style="text-align:center;padding:48px 24px;color:#94a3b8;">'
            '    <svg width="48" height="48" fill="none" stroke="#cbd5e1" stroke-width="1.5" viewBox="0 0 24 24" style="margin-bottom:16px;"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>'
            '    <p style="margin:0 0 6px;font-size:15px;font-weight:500;color:#475569;">No records found</p>'
            f'    <p style="margin:0;font-size:13px;color:#94a3b8;">No students match &ldquo;{query}&rdquo; in {label} search.</p>'
            '  </div>'
            '  <div style="display:flex;justify-content:flex-start;">'
            '    <button hx-get="/school-admin/registration/search-reset/" hx-target="#search-card-container" hx-swap="innerHTML"'
            '            style="display:inline-flex;align-items:center;gap:6px;background:#f1f5f9;border:1px solid #cbd5e1;color:#334155;padding:10px 22px;border-radius:8px;font-weight:600;font-size:13px;cursor:pointer;font-family:\'Inter\',sans-serif;transition:all 0.2s ease;">'
            '      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/></svg>'
            '      Back'
            '    </button>'
            '  </div>'
            '</div>'
        )
    return HttpResponse(html)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_search_form_reset(request):
    """HTMX endpoint: return the empty search form shell to replace results."""
    school = get_request_school(request)
    from .constants import GRADE_CHOICES
    grades = GRADE_CHOICES if school else []

    grade_options = '<option value="">Form / Grade</option>'
    for g in grades:
        grade_options += f'<option value="{g}">{g}</option>'

    html = (
        '<div style="background:white;padding:24px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">'
        '  <h3 style="margin-top:0;color:#333;font-size:16px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;">Search By &mdash; <span id="search-type-label">Admission Number</span></h3>'
        '  <form id="search-form" hx-get="/school-admin/registration/search-submit/" hx-target="#search-card-container" hx-swap="innerHTML">'
        '    <input type="hidden" name="tab" value="directory">'
        '    <div style="display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid #e5e7eb;" id="search-type-radios">'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="adm_no" data-type="adm_no" checked>Admission Number</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="name" data-type="name">Name</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="phone" data-type="phone">Phone Number</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="assessment_no" data-type="assessment_no">Assessment Number</label>'
        '    </div>'
        '    <div id="dynamic-fields" style="display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-bottom:16px;">'
        '      <div style="grid-column:span 2;display:flex;flex-direction:column;gap:6px;">'
        '        <label style="color:#ea580c;font-size:14px;font-weight:500;">Admission Number<span style="color:#ef4444;">*</span></label>'
        '        <input type="text" name="admission_number" placeholder="Enter Admission Number..." required'
        '               pattern="^[A-Za-z0-9]+$" title="Admission number must be alphanumeric (e.g. ADM001)"'
        '               style="width:100%;padding:10px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box;background-color:#f0fdf4;font-size:14px;">'
        '      </div>'
        '    </div>'
        '    <div style="display:flex;justify-content:flex-end;width:100%;">'
        '      <button type="submit" style="background-color:#0ea5e9;color:white;padding:10px 24px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:6px;font-family:\'Inter\',sans-serif;">'
        '        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>'
        '        Search</button>'
        '    </div>'
        '  </form>'
        '</div>'
        '<script>'
        '(function(){'
        '  var L={adm_no:"Admission Number",name:"Name",phone:"Phone Number",assessment_no:"Assessment Number"};'
        '  var U="/school-admin/registration/search-fields/";'
        '  var R=document.querySelectorAll("#search-type-radios input[type=radio]");'
        '  var T=document.getElementById("dynamic-fields");'
        '  var S=document.getElementById("search-type-label");'
        '  R.forEach(function(r){r.addEventListener("click",function(){'
        '    var t=this.getAttribute("data-type");'
        '    if(S)S.textContent=L[t]||t;'
        '    fetch(U+"?type="+encodeURIComponent(t)).then(function(r){return r.text();}).then(function(h){if(T)T.innerHTML=h;});'
        '  });});'
        '})();'
        '</script>'
    )
    return HttpResponse(html)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_profile_card(request, student_id):
    """HTMX endpoint: return the student profile card HTML fragment."""
    school = get_request_school(request)
    try:
        student = Student.all_objects.select_related('guardian').get(pk=student_id, school=school)
    except Student.DoesNotExist:
        return HttpResponse(
            '<style>'
            '  .profile-back-btn { background:#ffffff; border:1px solid #cbd5e1; color:#334155; padding:10px 20px; border-radius:8px; font-weight:500; font-size:13px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:6px; }'
            '  .profile-back-btn:hover { background:#f8fafc; border-color:#94a3b8; }'
            '</style>'
            '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #e2e8f0;">'
            '  <h3 style="margin:0 0 24px;color:#1e3a8a;font-size:17px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Student Profile</h3>'
            '  <div style="text-align:center;padding:48px 24px;color:#94a3b8;">'
            '    <p style="margin:0;font-size:15px;font-weight:500;color:#475569;">Student not found</p>'
            '  </div>'
            '  <div style="margin-top:32px;">'
            '    <button class="profile-back-btn" hx-get="/school-admin/registration/search-reset/" hx-target="#search-card-container" hx-swap="innerHTML">'
            '      &larr; Back to Search'
            '    </button>'
            '  </div>'
            '</div>'
        )

    guardian_name = student.guardian.name if student.guardian else '—'
    guardian_phone = student.guardian.phone if student.guardian else '—'
    gender_display = student.gender or 'Not Specified'
    religion_display = student.religion if hasattr(student, 'religion') and student.religion else '—'

    html = (
        '<style>'
        '  .profile-back-btn { background:linear-gradient(135deg,#1e3a8a 0%,#1e40af 100%); border:none; color:#ffffff; padding:12px 28px; border-radius:10px; font-weight:600; font-size:13px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:8px; box-shadow:0 2px 8px rgba(30,58,138,0.25); }'
        '  .profile-back-btn:hover { background:linear-gradient(135deg,#1e40af 0%,#1d4ed8 100%); box-shadow:0 4px 12px rgba(30,58,138,0.35); transform:translateY(-1px); }'
        '  .profile-back-btn:active { transform:translateY(0); box-shadow:0 2px 6px rgba(30,58,138,0.2); }'
        '  .photo-action-btn { width:36px; height:36px; border-radius:50%; border:none; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all 0.2s ease; backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px); }'
        '  .photo-upload-btn { background:rgba(14,165,233,0.85); }'
        '  .photo-upload-btn:hover { background:rgba(2,132,199,0.95); }'
        '  .photo-delete-btn { background:rgba(239,68,68,0.85); }'
        '  .photo-delete-btn:hover { background:rgba(220,38,38,0.95); }'
        '  .section-edit-btn { background:#ffffff; border:1px solid #e2e8f0; color:#0ea5e9; width:34px; height:34px; border-radius:8px; cursor:pointer; display:inline-flex; align-items:center; justify-content:center; transition:all 0.2s ease; flex-shrink:0; }'
        '  .section-edit-btn:hover { background:#f0f9ff; border-color:#0ea5e9; }'
        '  .student-delete-btn { background:linear-gradient(135deg,#dc2626 0%,#ef4444 100%); border:none; color:#ffffff; padding:12px 28px; border-radius:10px; font-weight:600; font-size:13px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:8px; box-shadow:0 2px 8px rgba(220,38,38,0.25); }'
        '  .student-delete-btn:hover { background:linear-gradient(135deg,#b91c1c 0%,#dc2626 100%); box-shadow:0 4px 12px rgba(220,38,38,0.35); transform:translateY(-1px); }'
        '  .student-delete-btn:active { transform:translateY(0); box-shadow:0 2px 6px rgba(220,38,38,0.2); }'
        '</style>'

        '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #e2e8f0;">'

        # ── Premium Header Banner ──
        '  <div style="background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%);border:1px solid #bfdbfe;border-radius:14px;padding:32px 36px;margin-bottom:28px;display:flex;align-items:center;gap:28px;">'
        '    <div style="position:relative;width:88px;height:88px;flex-shrink:0;">'
        '      <div style="width:88px;height:88px;border-radius:50%;background:rgba(255,255,255,0.6);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid rgba(255,255,255,0.8);box-shadow:0 8px 24px rgba(0,0,0,0.08);display:flex;align-items:center;justify-content:center;">'
        '        <svg width="44" height="44" fill="none" stroke="#64748b" stroke-width="1.5" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'
        '      </div>'
        '      <div style="position:absolute;bottom:-4px;left:-4px;display:flex;gap:6px;">'
        '        <button class="photo-action-btn photo-upload-btn" title="Upload photo" onclick="document.getElementById(\'student-photo-input\').click()">'
        '          <svg width="16" height="16" fill="none" stroke="#fff" stroke-width="2" viewBox="0 0 24 24"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>'
        '        </button>'
        f'        <button class="photo-action-btn photo-delete-btn" title="Remove photo" hx-delete="/school-admin/registration/profile/{student.id}/photo/" hx-target="#search-card-container" hx-swap="innerHTML" hx-confirm="Remove student photo?">'
        '          <svg width="16" height="16" fill="none" stroke="#fff" stroke-width="2" viewBox="0 0 24 24"><line x1="1" y1="1" x2="23" y2="23"/><path d="M21 21H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3m3-3h6l2 3h4a2 2 0 0 1 2 2v9.34m-7.72-2.06a4 4 0 1 1-5.56-5.56"/></svg>'
        '        </button>'
        '      </div>'
        '      <input type="file" id="student-photo-input" accept="image/*" style="display:none;">'
        '    </div>'
        '    <div>'
        f'      <h2 style="margin:0 0 8px;color:#1e3a8a;font-size:24px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;letter-spacing:-0.01em;">{student.name}</h2>'
        f'      <p style="margin:0 0 4px;color:#475569;font-size:14px;font-weight:400;">Admission Number: {student.admission_no}</p>'
        f'      <p style="margin:0;color:#64748b;font-size:13px;font-weight:400;">{student.class_name} {student.stream}</p>'
        '    </div>'
        '  </div>'

        # ── Personal Information Card ──
        '  <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:28px 32px;box-shadow:0 10px 25px -5px rgba(0,0,0,0.05),0 8px 10px -6px rgba(0,0,0,0.05);">'
        '    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;">'
        '      <h3 style="margin:0;color:#059669;font-size:16px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Personal Information</h3>'
        f'      <button class="section-edit-btn" hx-get="/school-admin/registration/profile/{student.id}/edit/" hx-target="#search-card-container" hx-swap="innerHTML" title="Edit personal details">'
        '        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
        '      </button>'
        '    </div>'
        '    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">'

        # Name tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Name</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{student.name.upper()}</p>'
        '      </div>'

        # Admission Number tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Admission Number</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{student.admission_no}</p>'
        '      </div>'

        # Gender tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Gender</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{gender_display}</p>'
        '      </div>'

        # Assessment Number tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Assessment Number</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{student.assessment_no or "—"}</p>'
        '      </div>'

        # Class tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Class</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{student.class_name} {student.stream}</p>'
        '      </div>'

        # Religion tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Religion</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{religion_display}</p>'
        '      </div>'

        # Primary Contact tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Primary Contact</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{guardian_phone}</p>'
        '      </div>'

        # Guardian Name tile
        '      <div style="background:#f8fafc;border-radius:8px;padding:14px 16px;">'
        '        <p style="margin:0 0 4px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Guardian Name</p>'
        f'        <p style="margin:0;font-size:14px;font-weight:500;color:#0f172a;">{guardian_name}</p>'
        '      </div>'

        '    </div>'
        '  </div>'

        # ── Actions Row ──
        '  <div style="margin-top:32px;">'
        '    <button class="profile-back-btn" hx-get="/school-admin/registration/search-reset/" hx-target="#search-card-container" hx-swap="innerHTML">'
        '      &larr; Back to Search'
        '    </button>'
        '  </div>'

        # ── Delete Student (centered, bottom) ──
        '  <div style="margin-top:24px;display:flex;justify-content:center;">'
        f'    <button class="student-delete-btn" hx-post="/school-admin/registration/profile/{student.id}/delete/" hx-target="#search-card-container" hx-swap="innerHTML" hx-confirm="Are you sure you want to delete {student.name}? This action cannot be undone.">'
        '      <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>'
        '      Delete Student'
        '    </button>'
        '  </div>'

        '</div>'
    )
    return HttpResponse(html)


INPUT_STYLE = 'width:100%;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;box-sizing:border-box;font-size:14px;color:#0f172a;background:#ffffff;font-family:"Inter",sans-serif;transition:border-color 0.2s;'
LABEL_STYLE = 'margin:0 0 6px;font-size:11px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;'
TILE_STYLE = 'background:#f8fafc;border-radius:8px;padding:14px 16px;'


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_profile_edit(request, student_id):
    """HTMX endpoint: return an editable form for the student profile."""
    school = get_request_school(request)
    try:
        student = Student.all_objects.select_related('guardian').get(pk=student_id, school=school)
    except Student.DoesNotExist:
        return HttpResponse('<div style="padding:40px;text-align:center;color:#94a3b8;">Student not found.</div>')

    guardian_name = student.guardian.name if student.guardian else ''
    guardian_phone = student.guardian.phone if student.guardian else ''

    from .constants import GRADE_CHOICES
    grades = GRADE_CHOICES if school else []
    grade_options = ''
    for g in grades:
        sel = ' selected' if g == student.class_name else ''
        grade_options += f'<option value="{g}"{sel}>{g}</option>'

    streams = ['Yellow', 'Blue', 'Main']
    stream_options = ''
    for s_val in streams:
        sel = ' selected' if s_val == student.stream else ''
        stream_options += f'<option value="{s_val}"{sel}>{s_val}</option>'

    gender_options = ''
    for g_val in ['Male', 'Female', 'Not Specified']:
        sel = ' selected' if g_val == student.gender else ''
        gender_options += f'<option value="{g_val}"{sel}>{g_val}</option>'

    religion_options = ''
    for r_val in ['CRE', 'IRE', 'None']:
        sel = ' selected' if r_val == student.religion else ''
        religion_options += f'<option value="{r_val}"{sel}>{r_val}</option>'

    html = (
        '<style>'
        '  .ios-edit-wrap { background:#ffffff; padding:0; border-radius:16px; }'
        '  .ios-edit-header { background:linear-gradient(135deg,#eff6ff 0%,#dbeafe 100%); border:1px solid #bfdbfe; border-radius:14px; padding:28px 36px; margin-bottom:24px; display:flex; align-items:center; gap:24px; }'
        '  .ios-edit-avatar { width:72px; height:72px; border-radius:50%; background:rgba(255,255,255,0.6); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); border:1px solid rgba(255,255,255,0.8); box-shadow:0 8px 24px rgba(0,0,0,0.08); display:flex; align-items:center; justify-content:center; flex-shrink:0; }'
        '  .ios-edit-card { background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; padding:28px 32px; box-shadow:0 10px 25px -5px rgba(0,0,0,0.05),0 8px 10px -6px rgba(0,0,0,0.05); margin-bottom:24px; }'
        '  .ios-edit-tile { background:#f8fafc; border-radius:10px; padding:14px 16px; transition:background 0.2s ease; }'
        '  .ios-edit-tile:hover { background:#f1f5f9; }'
        '  .ios-edit-label { margin:0 0 6px; font-size:11px; font-weight:600; color:#64748b; letter-spacing:0.05em; text-transform:uppercase; }'
        '  .ios-edit-input { width:100%; padding:10px 12px; border:1px solid #e2e8f0; border-radius:8px; box-sizing:border-box; font-size:14px; color:#0f172a; background:#ffffff; font-family:"Inter",sans-serif; transition:all 0.2s ease; outline:none; }'
        '  .ios-edit-input:focus { border-color:#0ea5e9; box-shadow:0 0 0 3px rgba(14,165,233,0.12); }'
        '  .ios-edit-select { width:100%; padding:10px 12px; border:1px solid #e2e8f0; border-radius:8px; box-sizing:border-box; font-size:14px; color:#0f172a; background:#ffffff; font-family:"Inter",sans-serif; transition:all 0.2s ease; outline:none; appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'12\' viewBox=\'0 0 24 24\' fill=\'none\' stroke=\'%2394a3b8\' stroke-width=\'2\'%3E%3Cpath d=\'M6 9l6 6 6-6\'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 12px center; padding-right:32px; }'
        '  .ios-edit-select:focus { border-color:#0ea5e9; box-shadow:0 0 0 3px rgba(14,165,233,0.12); }'
        '  .ios-save-btn { background:#059669; color:#ffffff; padding:12px 28px; border:none; border-radius:10px; font-weight:600; font-size:14px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:8px; box-shadow:0 2px 8px rgba(5,150,105,0.25); }'
        '  .ios-save-btn:hover { background:#047857; box-shadow:0 4px 12px rgba(5,150,105,0.35); transform:translateY(-1px); }'
        '  .ios-save-btn:active { transform:translateY(0); }'
        '  .ios-cancel-btn { background:#ffffff; color:#334155; padding:12px 28px; border:1px solid #e2e8f0; border-radius:10px; font-weight:600; font-size:14px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:8px; }'
        '  .ios-cancel-btn:hover { background:#f8fafc; border-color:#94a3b8; }'
        '  .ios-section-title { margin:0 0 24px; color:#059669; font-size:16px; font-family:"Plus Jakarta Sans",sans-serif; font-weight:600; display:flex; align-items:center; gap:8px; }'
        '</style>'

        '<div class="ios-edit-wrap">'

        # ── Header ──
        '  <div class="ios-edit-header">'
        '    <div class="ios-edit-avatar">'
        '      <svg width="36" height="36" fill="none" stroke="#64748b" stroke-width="1.5" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>'
        '    </div>'
        '    <div>'
        f'      <h2 style="margin:0 0 6px;color:#1e3a8a;font-size:20px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Editing: {student.name}</h2>'
        '      <p style="margin:0;color:#475569;font-size:13px;">Update the student\'s personal information below</p>'
        '    </div>'
        '  </div>'

        # ── Edit Form ──
        f'  <form hx-post="/school-admin/registration/profile/{student.id}/save/" hx-target="#search-card-container" hx-swap="innerHTML">'
        f'    <input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">'

        # ── Personal Information Section ──
        '    <div class="ios-edit-card">'
        '      <h3 class="ios-section-title">'
        '        <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>'
        '        Personal Information'
        '      </h3>'
        '      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;">'

        # Name
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Full Name</label>'
        f'          <input type="text" name="name" value="{student.name}" required class="ios-edit-input" placeholder="Enter full name...">'
        '        </div>'

        # Admission Number
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Admission Number</label>'
        f'          <input type="text" name="admission_no" value="{student.admission_no}" required pattern="^[A-Za-z0-9]+$" title="Alphanumeric only (e.g. ADM001)" class="ios-edit-input" placeholder="e.g. ADM001">'
        '        </div>'

        # Gender
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Gender</label>'
        f'          <select name="gender" class="ios-edit-select">{gender_options}</select>'
        '        </div>'

        # Assessment Number
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Assessment Number</label>'
        f'          <input type="text" name="assessment_no" value="{student.assessment_no or ""}" class="ios-edit-input" placeholder="Enter assessment number...">'
        '        </div>'

        # Class
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Class / Grade</label>'
        f'          <select name="class_name" required class="ios-edit-select">{grade_options}</select>'
        '        </div>'

        # Stream
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Stream</label>'
        f'          <select name="stream" required class="ios-edit-select">{stream_options}</select>'
        '        </div>'

        # Religion
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Religion</label>'
        f'          <select name="religion" class="ios-edit-select">{religion_options}</select>'
        '        </div>'

        '      </div>'
        '    </div>'

        # ── Guardian Information Section ──
        '    <div class="ios-edit-card">'
        '      <h3 class="ios-section-title">'
        '        <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
        '        Guardian Information'
        '      </h3>'
        '      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:16px;">'

        # Guardian Name
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Guardian Name</label>'
        f'          <input type="text" name="guardian_name" value="{guardian_name}" class="ios-edit-input" placeholder="Enter guardian name...">'
        '        </div>'

        # Primary Contact
        '        <div class="ios-edit-tile">'
        '          <label class="ios-edit-label">Primary Contact</label>'
        f'          <input type="tel" name="guardian_phone" value="{guardian_phone}" class="ios-edit-input" placeholder="Enter phone number...">'
        '        </div>'

        '      </div>'
        '    </div>'

        # ── Action Buttons ──
        '    <div style="display:flex;gap:12px;align-items:center;">'
        '      <button type="submit" class="ios-save-btn">'
        '        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg>'
        '        Save Changes'
        '      </button>'
        '      <button type="button" class="ios-cancel-btn" hx-get="/school-admin/registration/profile/' + str(student.id) + '/" hx-target="#search-card-container" hx-swap="innerHTML">'
        '        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
        '        Cancel'
        '      </button>'
        '    </div>'
        '  </form>'

        '</div>'
    )
    return HttpResponse(html)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_profile_save(request, student_id):
    """HTMX endpoint: save edited student profile and return the profile card."""
    school = get_request_school(request)
    try:
        student = Student.all_objects.select_related('guardian').get(pk=student_id, school=school)
    except Student.DoesNotExist:
        return HttpResponse('<div style="padding:40px;text-align:center;color:#94a3b8;">Student not found.</div>')

    if request.method != 'POST':
        return admin_student_profile_card(request, student_id)

    student.name = request.POST.get('name', student.name).strip()
    student.admission_no = request.POST.get('admission_no', student.admission_no).strip()
    student.gender = request.POST.get('gender', student.gender)
    assessment_raw = request.POST.get('assessment_no', '').strip()
    student.assessment_no = assessment_raw if assessment_raw else None
    student.class_name = request.POST.get('class_name', student.class_name)
    student.stream = request.POST.get('stream', student.stream)
    student.religion = request.POST.get('religion', student.religion)

    if Student.objects.filter(school=school, admission_no=student.admission_no, is_active=True).exclude(pk=student.pk).exists():
        error_html = (
            '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:12px;padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;gap:12px;">'
            '  <svg width="20" height="20" fill="none" stroke="#ef4444" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>'
            '  <span style="color:#991b1b;font-size:14px;font-weight:500;">Admission number already exists. Please choose a different one.</span>'
            '</div>'
        )
        # Return edit form with error
        edit_html = admin_student_profile_edit(request, student_id)
        return HttpResponse(error_html + edit_html.content.decode('utf-8'))

    try:
        student.save()
    except Exception as e:
        error_html = (
            '<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:12px;padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;gap:12px;">'
            '  <svg width="20" height="20" fill="none" stroke="#ef4444" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>'
            f'  <span style="color:#991b1b;font-size:14px;font-weight:500;">Error saving: {str(e)}</span>'
            '</div>'
        )
        edit_html = admin_student_profile_edit(request, student_id)
        return HttpResponse(error_html + edit_html.content.decode('utf-8'))

    g_name = request.POST.get('guardian_name', '').strip()
    g_phone = request.POST.get('guardian_phone', '').strip()
    if student.guardian and g_name:
        student.guardian.name = g_name
        if g_phone:
            student.guardian.phone = g_phone
        student.guardian.save()
    elif g_name and g_phone:
        guardian, _ = Guardian.all_objects.get_or_create(
            school=school, phone=g_phone,
            defaults={'name': g_name}
        )
        student.guardian = guardian
        student.save()

    messages.success(request, f"Student '{student.name}' updated successfully.")

    student = Student.all_objects.select_related('guardian').get(pk=student.pk, school=school)

    success_html = (
        '<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;gap:12px;">'
        '  <svg width="20" height="20" fill="none" stroke="#16a34a" stroke-width="2" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
        f'  <span style="color:#166534;font-size:14px;font-weight:500;">Student \'{student.name}\' updated successfully.</span>'
        '</div>'
    )

    profile_html = admin_student_profile_card(request, student_id)
    return HttpResponse(success_html + profile_html.content.decode('utf-8'))


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_delete(request, student_id):
    """HTMX endpoint: soft-delete a student and return the search form shell."""
    from django.utils import timezone
    school = get_request_school(request)
    try:
        student = Student.all_objects.get(pk=student_id, school=school)
        student_name = student.name
        student.is_active = False
        student.status = 'Removed'
        student.date_removed = timezone.now()
        student.deleted_by = request.user
        student.save(update_fields=['is_active', 'status', 'date_removed', 'deleted_by'])
        messages.success(request, f"Student '{student_name}' has been removed successfully.")
    except Student.DoesNotExist:
        messages.error(request, "Student not found.")

    # Return the search form shell (same as admin_search_form_reset)
    from .constants import GRADE_CHOICES
    grades = GRADE_CHOICES if school else []

    grade_options = '<option value="">Form / Grade</option>'
    for g in grades:
        grade_options += f'<option value="{g}">{g}</option>'

    html = (
        '<style>'
        '  .sr-back-btn { background:#ffffff; border:1px solid #cbd5e1; color:#334155; padding:10px 20px; border-radius:8px; font-weight:500; font-size:13px; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:6px; }'
        '  .sr-back-btn:hover { background:#f8fafc; border-color:#94a3b8; }'
        '</style>'
        '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #e2e8f0;">'
        '  <h3 style="margin:0 0 20px;color:#1e3a8a;font-size:17px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:600;">Search By &mdash; <span id="search-type-label">Admission Number</span></h3>'
        '  <form id="search-form" hx-get="/school-admin/registration/search-submit/" hx-target="#search-card-container" hx-swap="innerHTML">'
        '    <input type="hidden" name="tab" value="directory">'
        '    <div style="display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid #e5e7eb;" id="search-type-radios">'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="adm_no" data-type="adm_no" checked>Admission Number</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="name" data-type="name">Name</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="phone" data-type="phone">Phone Number</label>'
        '      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:14px;color:#374151;font-weight:500;flex:1;padding:10px 0;justify-content:center;">'
        '        <input type="radio" name="search_type" value="assessment_no" data-type="assessment_no">Assessment Number</label>'
        '    </div>'
        '    <div id="dynamic-fields" style="display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin-bottom:16px;">'
        '      <div style="grid-column:span 2;display:flex;flex-direction:column;gap:6px;">'
        '        <label style="color:#ea580c;font-size:14px;font-weight:500;">Admission Number<span style="color:#ef4444;">*</span></label>'
        '        <input type="text" name="admission_number" placeholder="Enter Admission Number..." required'
        '               pattern="^[A-Za-z0-9]+$" title="Admission number must be alphanumeric (e.g. ADM001)"'
        '               style="width:100%;padding:10px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box;background-color:#f0fdf4;font-size:14px;">'
        '      </div>'
        '    </div>'
        '    <div style="display:flex;justify-content:flex-end;width:100%;">'
        '      <button type="submit" style="background-color:#0ea5e9;color:white;padding:10px 24px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:6px;font-family:\'Inter\',sans-serif;">'
        '        <svg width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>'
        '        Search</button>'
        '    </div>'
        '  </form>'
        '</div>'
        '<script>'
        '(function(){'
        '  var L={adm_no:"Admission Number",name:"Name",phone:"Phone Number",assessment_no:"Assessment Number"};'
        '  var U="/school-admin/registration/search-fields/";'
        '  var R=document.querySelectorAll("#search-type-radios input[type=radio]");'
        '  var T=document.getElementById("dynamic-fields");'
        '  var S=document.getElementById("search-type-label");'
        '  R.forEach(function(r){r.addEventListener("click",function(){'
        '    var t=this.getAttribute("data-type");'
        '    if(S)S.textContent=L[t]||t;'
        '    fetch(U+"?type="+encodeURIComponent(t)).then(function(r){return r.text();}).then(function(h){if(T)T.innerHTML=h;});'
        '  });});'
        '})();'
        '</script>'
    )
    return HttpResponse(html)


@login_required(login_url='login')
@school_admin_required
@never_cache
def admin_student_analytics(request, student_id):
    """HTMX endpoint: premium student analytics dashboard shell."""
    from django.db.models import Avg, Sum, Count, F, Q
    from ..models import ExamSummary, Mark, Subject
    from .helpers import get_performance_level

    school = get_request_school(request)
    try:
        student = Student.all_objects.select_related('guardian').get(pk=student_id, school=school)
    except Student.DoesNotExist:
        return HttpResponse(
            '<div style="background:#ffffff;padding:28px;border-radius:12px;border:1px solid #e2e8f0;text-align:center;">'
            '  <p style="color:#94a3b8;font-size:15px;margin:0;">Student not found.</p>'
            '  <div style="margin-top:24px;">'
            '    <button class="an-back-btn" hx-get="/school-admin/registration/search-reset/" hx-target="#search-card-container" hx-swap="innerHTML">&larr; Back to Search</button>'
            '  </div>'
            '</div>'
        )

    initials = ''.join([w[0] for w in student.name.split()[:2]]).upper()
    guardian_name = student.guardian.name if student.guardian else '—'

    from django.db.models import Case, When, Value, IntegerField
    exam_order = Case(
        When(exam_name__icontains='Opener', then=1),
        When(exam_name__icontains='Mid', then=2),
        When(exam_name__icontains='End', then=3),
        default=0,
        output_field=IntegerField(),
    )
    latest = (
        ExamSummary.all_objects.filter(student=student, school=school)
        .annotate(exam_sort=exam_order)
        .order_by('-year', '-term', '-exam_sort')
        .first()
    )
    all_exams = list(
        ExamSummary.all_objects.filter(student=student, school=school)
        .annotate(exam_sort=exam_order)
        .order_by('-year', '-term', '-exam_sort')[:10]
    )
    total_students_in_class = (
        Student.all_objects.filter(school=school, class_name=student.class_name, stream=student.stream, is_active=True)
        .count()
    )
    total_students_in_grade = (
        Student.all_objects.filter(school=school, class_name=student.class_name, is_active=True)
        .count()
    )

    if latest:
        mean_marks = round(latest.total_marks / latest.subject_count, 1) if latest.subject_count else 0
        total_points = latest.total_points
        overall_pos = f"{latest.grade_rank}/{total_students_in_grade}" if latest.grade_rank else "—"
        stream_pos = f"{latest.stream_rank}/{total_students_in_class}" if latest.stream_rank else "—"
        mean_grade = latest.overall_plv or "—"
        exam_label = f"{latest.exam_name} — {latest.term} {latest.year}"
    else:
        mean_marks = 0
        total_points = 0
        overall_pos = "—"
        stream_pos = "—"
        mean_grade = "—"
        exam_label = "No exam data"

    prev_exam = all_exams[1] if len(all_exams) > 1 else None
    if latest and prev_exam and prev_exam.subject_count:
        prev_mean = round(prev_exam.total_marks / prev_exam.subject_count, 1)
        marks_trend = round(mean_marks - prev_mean, 1)
    else:
        marks_trend = 0

    trend_icon = '▲' if marks_trend > 0 else ('▼' if marks_trend < 0 else '—')
    trend_color = '#16a34a' if marks_trend > 0 else ('#ef4444' if marks_trend < 0 else '#94a3b8')

    term_choices = [('Term 1', 'Term 1'), ('Term 2', 'Term 2'), ('Term 3', 'Term 3')]
    current_term = latest.term if latest else student.term
    term_options = ''
    for t_val, t_label in term_choices:
        sel = ' selected' if t_val == current_term else ''
        term_options += f'<option value="{t_val}"{sel}>{t_label}</option>'

    exam_rows = ''
    for idx, ex in enumerate(all_exams):
        ex_mean = round(ex.total_marks / ex.subject_count, 1) if ex.subject_count else 0
        ex_rows_inner = (
            '<tr style="border-bottom:1px solid #f1f5f9;">'
            f'  <td style="padding:12px 16px;font-size:13px;color:#475569;font-weight:500;">{ex.exam_name}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#475569;">{ex.term}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#475569;">{ex.year}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#0f172a;font-weight:600;">{ex.total_marks}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#0f172a;font-weight:600;">{ex_mean}%</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#0f172a;font-weight:600;">{ex.total_points}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#475569;">{ex.grade_rank or "—"}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;color:#475569;">{ex.stream_rank or "—"}</td>'
            f'  <td style="padding:12px 16px;font-size:13px;">'
            f'    <span style="background:{"#dcfce7" if ex.overall_plv in ["EXCELLENT","GOOD","SATISFACTORY"] else "#fef9c3" if ex.overall_plv == "AVERAGE" else "#fee2e2"};'
            f'    color:{"#166534" if ex.overall_plv in ["EXCELLENT","GOOD","SATISFACTORY"] else "#854d0e" if ex.overall_plv == "AVERAGE" else "#991b1b"};'
            f'    padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;">{ex.overall_plv or "—"}</span>'
            '  </td>'
            '</tr>'
        )
        exam_rows += ex_rows_inner

    if not exam_rows:
        exam_rows = (
            '<tr><td colspan="9" style="padding:40px;text-align:center;color:#94a3b8;font-size:14px;">No exam data available yet.</td></tr>'
        )

    # ── Subject performance + chart data (single pass) ──
    chart_labels = []
    chart_student_data = []
    chart_form_data = []
    chart_target_data = []
    subject_perf_rows = ''
    if latest:
        latest_marks = list(
            Mark.all_objects.filter(
                student=student, school=school,
                year=latest.year, term=latest.term, exam_type=latest.exam_name,
            ).select_related('subject')
        )
        latest_subj_ids = {mk.subject_id for mk in latest_marks if mk.subject_id}
        earlier_marks = Mark.all_objects.filter(
            student=student, school=school,
            year=latest.year, term=latest.term,
        ).exclude(exam_type=latest.exam_name).select_related('subject')
        for mk in earlier_marks:
            if mk.subject_id and mk.subject_id not in latest_subj_ids:
                latest_subj_ids.add(mk.subject_id)
                latest_marks.append(mk)
        subject_names_map = {
            s.code: s.name for s in Subject.all_objects.filter(school=school)
        }
        class_avgs = {}
        class_stream_avg = Mark.all_objects.filter(
            school=school, student__class_name=student.class_name, student__stream=student.stream,
            year=latest.year, term=latest.term, exam_type=latest.exam_name,
            is_absent=False, score__gt=0,
        ).values('subject__code').annotate(avg=Avg('score'))
        for row in class_stream_avg:
            class_avgs[row['subject__code']] = round(row['avg'], 1)

        grade_avgs = {}
        all_grade_avg = Mark.all_objects.filter(
            school=school, student__class_name=student.class_name,
            year=latest.year, term=latest.term, exam_type=latest.exam_name,
            is_absent=False, score__gt=0,
        ).values('subject__code').annotate(avg=Avg('score'))
        for row in all_grade_avg:
            grade_avgs[row['subject__code']] = round(row['avg'], 1)

        target_marks = {}
        if prev_exam:
            prev_marks = Mark.all_objects.filter(
                student=student, school=school,
                year=prev_exam.year, term=prev_exam.term, exam_type=prev_exam.exam_name,
                is_absent=False, score__gt=0,
            ).values('subject__code').annotate(prev_avg=Avg('score'))
            for row in prev_marks:
                target_marks[row['subject__code']] = round(row['prev_avg'], 1)

        subject_class_ranks = {}
        if latest:
            all_students_marks = Mark.all_objects.filter(
                school=school, student__class_name=student.class_name, student__stream=student.stream,
                year=latest.year, term=latest.term,
                is_absent=False, score__gt=0,
            ).select_related('subject').order_by('-exam_type')
            from collections import defaultdict
            subj_scores = defaultdict(list)
            seen_per_student = {}
            for m in all_students_marks:
                key = (m.student_id, m.subject_id)
                if key not in seen_per_student:
                    seen_per_student[key] = m
                    code = m.subject.code if m.subject else ''
                    if code:
                        subj_scores[code].append((m.student_id, m.score))
            for s_code, s_list in subj_scores.items():
                s_list.sort(key=lambda x: -x[1])
                rank_map = {}
                for idx, (sid, _) in enumerate(s_list, 1):
                    rank_map[sid] = idx
                total_in_subj = len(s_list)
                subject_class_ranks[s_code] = (rank_map, total_in_subj)

        SUBJ_SHORT = {
            '901': 'Eng', '902': 'Kisw', '903': 'Maths', '904': 'KSL',
            '905': 'IS', '906': 'Agr', '907': 'Soc', '908': 'CRE',
            '909': 'IRE', '910': 'HRE', '911': 'CAS', '912': 'PRE',
        }
        for mk in sorted(latest_marks, key=lambda x: (x.subject.code if x.subject else '')):
            if mk.is_absent or mk.score is None:
                continue
            code = mk.subject.code if mk.subject else ''
            sc = mk.score
            c_avg = class_avgs.get(code, 0)
            tgt = 0

            chart_labels.append(SUBJ_SHORT.get(code, code))
            chart_student_data.append(sc)
            chart_form_data.append(c_avg)

            subj_name = subject_names_map.get(code, code)
            dev_exam = round(sc - c_avg, 1) if c_avg else 0
            dev_target = round(sc - tgt, 1) if tgt else 0
            dev_exam_clr = '#16a34a' if dev_exam > 0 else ('#ef4444' if dev_exam < 0 else '#94a3b8')
            dev_tgt_clr = '#16a34a' if dev_target > 0 else ('#ef4444' if dev_target < 0 else '#94a3b8')
            dev_exam_svg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="' + dev_exam_clr + '" stroke-width="2.5"><path d="M7 7l5 5 5-5"/></svg>' if dev_exam > 0 else '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="' + dev_exam_clr + '" stroke-width="2.5"><path d="M7 17l5-5 5 5"/></svg>' if dev_exam < 0 else ''
            dev_tgt_svg = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="' + dev_tgt_clr + '" stroke-width="2.5"><path d="M7 7l5 5 5-5"/></svg>' if dev_target > 0 else '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="' + dev_tgt_clr + '" stroke-width="2.5"><path d="M7 17l5-5 5 5"/></svg>' if dev_target < 0 else ''
            rank_data = subject_class_ranks.get(code, (None, 0))
            stream_rank = f"{rank_data[0].get(student.id, '—')} / {rank_data[1]}" if rank_data[0] else "—"
            subject_perf_rows += (
                '<tr style="border-bottom:1px solid #f1f5f9;">'
                f'  <td style="padding:14px 20px;font-size:13px;color:#0f172a;font-weight:600;">{subj_name}</td>'
                f'  <td style="padding:14px 20px;font-size:13px;color:#0f172a;font-weight:500;">{sc}%</td>'
                f'  <td style="padding:14px 20px;font-size:13px;">'
                f'    <span style="display:inline-flex;align-items:center;gap:3px;color:{dev_exam_clr};font-weight:600;">'
                f'      {dev_exam_svg} {dev_exam:+.0f}'
                '    </span>'
                '  </td>'
                f'  <td style="padding:14px 20px;font-size:13px;">'
                f'    <span style="display:inline-flex;align-items:center;gap:3px;color:{dev_tgt_clr};font-weight:600;">'
                f'      {dev_tgt_svg} {dev_target:+.0f}'
                '    </span>'
                '  </td>'
                f'  <td style="padding:14px 20px;font-size:13px;color:#0f172a;font-weight:500;">{mk.performance_level or "—"}</td>'
                f'  <td style="padding:14px 20px;font-size:13px;color:#475569;font-weight:500;">{stream_rank}</td>'
                '</tr>'
            )
    if not chart_labels:
        chart_labels = ['No Data']
        chart_student_data = [0]
        chart_form_data = [0]
        chart_target_data = [0]
    if not subject_perf_rows:
        subject_perf_rows = (
            '<tr><td colspan="6" style="padding:40px;text-align:center;color:#94a3b8;font-size:14px;">No subject marks available for this exam.</td></tr>'
        )

    # ── Build clean JSON for chart data ──
    import json as _json
    has_chart_data = bool(chart_labels) and chart_labels != ['No Data']
    chart_labels_json = _json.dumps(chart_labels)
    chart_student_data_json = _json.dumps(chart_student_data)
    chart_form_data_json = _json.dumps(chart_form_data)
    chart_target_data_json = _json.dumps(chart_target_data)
    chart_student_name = student.name.split()[0].upper() if student.name else 'STUDENT'
    chart_class_label = f"{student.class_name} {student.stream}" if student.class_name else 'Class'

    # ── Performance Over Time chart data (from Mark records, not ExamSummary) ──
    grade_abbr = student.class_name.replace('Grade', 'G').strip() if student.class_name else 'G'
    TERM_SHORT = {'Term 1': 'T1', 'Term 2': 'T2', 'Term 3': 'T3'}
    EXAM_SHORT = {
        'Opener Assessment': 'OPENER',
        'Mid Term Assessment': 'MID-TERM',
        'End of Term Assessment': 'END TERM',
    }

    from django.db.models import Case, When, Value, IntegerField as IntF
    exam_sort_expr = Case(
        When(exam_type__icontains='Opener', then=1),
        When(exam_type__icontains='Mid', then=2),
        When(exam_type__icontains='End', then=3),
        default=0,
        output_field=IntF(),
    )
    mark_exams = (
        Mark.all_objects.filter(student=student, school=school, is_absent=False, score__gt=0)
        .values('exam_type', 'term', 'year')
        .annotate(exam_sort=exam_sort_expr)
        .order_by('-year', '-term', 'exam_sort')
        .distinct()
    )

    timeline_labels = []
    timeline_student_scores = []
    for me in mark_exams:
        ex_marks = list(
            Mark.all_objects.filter(
                student=student, school=school,
                year=me['year'], term=me['term'], exam_type=me['exam_type'],
                is_absent=False, score__gt=0,
            )
        )
        if ex_marks:
            mean_score = round(sum(m.score for m in ex_marks) / len(ex_marks), 1)
        else:
            mean_score = 0
        term_short = TERM_SHORT.get(me['term'], me['term'])
        exam_short = EXAM_SHORT.get(me['exam_type'], me['exam_type'][:8].upper() if me['exam_type'] else 'EXAM')
        timeline_labels.append(f"{grade_abbr} {term_short}, {exam_short}, {me['year']}")
        timeline_student_scores.append(mean_score)

    timeline_labels_json = _json.dumps(timeline_labels)
    timeline_student_json = _json.dumps(timeline_student_scores)
    has_timeline_data = bool(timeline_labels)

    html = (
        '<style>'
        '  .an-back-btn { background:#f1f5f9; border:1px solid #cbd5e1; color:#334155; padding:10px 22px; border-radius:8px; font-weight:600; font-size:13px; cursor:pointer; font-family:"Inter","Plus Jakarta Sans",sans-serif; transition:all 0.2s ease; display:inline-flex; align-items:center; gap:6px; }'
        '  .an-back-btn:hover { background:#e2e8f0; border-color:#94a3b8; }'
        '  .an-pill { display:inline-flex; align-items:center; gap:8px; cursor:pointer; font-size:14px; color:#475569; font-weight:500; padding:12px 16px; border:1px solid #d1d5db; border-radius:8px; background:#ffffff; transition:all 0.2s; font-family:"Inter",sans-serif; }'
        '  .an-pill:hover { border-color:#94a3b8; background:#f9fafb; }'
        '  .an-pill.active { border-color:#22c55e; background:#f0fdf4; color:#166534; font-weight:600; }'
        '  .an-pill input[type=radio] { display:none; }'
        '  .an-curriculum-link { font-size:14px; color:#475569; font-weight:500; cursor:pointer; border-bottom:2px solid #22c55e; padding-bottom:2px; display:inline-block; text-decoration:none; }'
        '  .an-curriculum-link:hover { color:#16a34a; }'
        '  .an-dropdown-btn-green { background:#22c55e; border:none; color:#ffffff; padding:10px 20px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.15s; display:inline-flex; align-items:center; gap:8px; box-shadow:0 2px 6px rgba(34,197,94,0.3); }'
        '  .an-dropdown-btn-green:hover { background:#16a34a; box-shadow:0 4px 10px rgba(34,197,94,0.4); transform:translateY(-1px); }'
        '  .an-dropdown-select { background:#ffffff; border:1px solid #e2e8f0; color:#334155; padding:10px 32px 10px 14px; border-radius:8px; font-size:13px; font-weight:500; cursor:pointer; font-family:"Inter",sans-serif; transition:all 0.15s; appearance:none; background-image:url("data:image/svg+xml,%3Csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'12\' viewBox=\'0 0 24 24\' fill=\'none\' stroke=\'%2394a3b8\' stroke-width=\'2\'%3E%3Cpath d=\'M6 9l6 6 6-6\'/%3E%3C/svg%3E"); background-repeat:no-repeat; background-position:right 10px center; }'
        '  .an-dropdown-select:hover { border-color:#94a3b8; }'
        '</style>'

        # ── Inline <script> data block (dual-path: data-* attrs + global vars) ──
        '<script>'
        f'window.__chartData = {{'
        f'"labels": {chart_labels_json},'
        f'"studentData": {chart_student_data_json},'
        f'"formData": {chart_form_data_json},'
        f'"targetData": {chart_target_data_json},'
        f'"studentName": "{chart_student_name}",'
        f'"classLabel": "{chart_class_label}"'
        f'}};'
        f'window.__timelineData = {{'
        f'"labels": {timeline_labels_json},'
        f'"student": {timeline_student_json}'
        f'}};'
        '</script>'

        '<div style="background:#ffffff;padding:0;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">'

        # ── Profile Header ──
        '  <div style="padding:32px 40px;border-bottom:1px solid #e2e8f0;">'
        '    <div style="display:flex;align-items:center;justify-content:space-between;">'
        '      <div style="display:flex;align-items:center;gap:24px;">'
        f'        <div style="width:72px;height:72px;border-radius:50%;background:linear-gradient(135deg,#f97316 0%,#ef4444 100%);display:flex;align-items:center;justify-content:center;box-shadow:0 8px 24px rgba(249,115,22,0.3);flex-shrink:0;">'
        f'          <span style="color:#ffffff;font-size:26px;font-weight:700;font-family:"Plus Jakarta Sans",sans-serif;letter-spacing:0.02em;">{initials}</span>'
        '        </div>'
        '        <div>'
        f'          <h2 style="margin:0;color:#0f172a;font-size:22px;font-family:"Plus Jakarta Sans",sans-serif;font-weight:700;letter-spacing:-0.01em;">{student.name.upper()} — {student.class_name} {student.stream}</h2>'
        f'          <p style="margin:4px 0 0;color:#64748b;font-size:14px;">Admission Number: {student.admission_no}</p>'
        '        </div>'
        '      </div>'
        '    </div>'
        '  </div>'

        # ── Analysis Heading + Download/Term Controls ──
        '  <div style="padding:28px 40px 24px 40px;display:flex;align-items:flex-start;justify-content:space-between;">'
        '    <div>'
        '      <h3 style="margin:0;color:#0f172a;font-size:20px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;">Analysis</h3>'
        '      <p style="margin:4px 0 0;color:#64748b;font-size:14px;">Student\'s exam performance analysis</p>'
        '    </div>'
        '    <div style="display:flex;align-items:center;gap:12px;">'
        '      <button class="an-dropdown-btn-green">'
        '        <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>'
        '        Download'
        '      </button>'
        f'      <select class="an-dropdown-select">{term_options}</select>'
        '    </div>'
        '  </div>'

        # ── Curriculum Subjects Link ──
        '  <div style="padding:0 40px;">'
        '    <span class="an-curriculum-link">Curriculum Subjects</span>'
        '  </div>'

        # ── Two-Column Split Workspace ──
        '  <div style="padding:20px 40px 32px 40px;">'
        '    <div style="display:grid;grid-template-columns:1.1fr 1fr;gap:24px;margin-top:16px;align-items:stretch;">'

        # ═══ LEFT COLUMN ═══
        '      <div style="display:flex;flex-direction:column;gap:16px;">'

        # ── Mean Grade Card ──
        '        <div style="background:#22c55e;border-radius:12px;padding:14px 18px;display:flex;justify-content:space-between;align-items:center;">'
        '          <div>'
        '            <p style="margin:0;color:rgba(255,255,255,0.8);font-size:12px;font-weight:500;">Mean Grade</p>'
        f'            <p style="margin:0;color:#ffffff;font-size:20px;font-weight:700;font-family:"Plus Jakarta Sans",sans-serif;line-height:1.2;">{mean_grade}</p>'
        '          </div>'
        f'          <p style="margin:0;color:rgba(255,255,255,0.85);font-size:12px;font-weight:500;text-align:right;">{student.class_name} - End term - ({student.year} {current_term})</p>'
        '        </div>'

        # ── Pill Toggle: Compare Analysis ──
        '        <div>'
        '          <p style="margin:0 0 8px;font-size:13px;color:#0f172a;font-weight:600;">Compare analysis using:</p>'
        '          <div style="display:flex;gap:10px;">'
        '            <label class="an-pill active" onclick="this.parentElement.querySelectorAll(\'.an-pill\').forEach(function(p){p.classList.remove(\'active\')});this.classList.add(\'active\');">'
        '              <input type="radio" name="compare_mode" value="previous" checked>'
        '              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="#22c55e" stroke-width="2"/><circle cx="12" cy="12" r="5" fill="#22c55e"/></svg>'
        '              Previous exam results'
        '            </label>'
        '            <label class="an-pill" onclick="this.parentElement.querySelectorAll(\'.an-pill\').forEach(function(p){p.classList.remove(\'active\')});this.classList.add(\'active\');">'
        '              <input type="radio" name="compare_mode" value="targets">'
        '              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="#94a3b8" stroke-width="2"/></svg>'
        '              Student targets'
        '            </label>'
        '          </div>'
        '        </div>'

        # ── Metric Cards (2x2 grid) ──
        '        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;flex:1;">'

        # Card 1: Mean Marks
        '          <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);display:flex;flex-direction:column;justify-content:center;">'
        '            <p style="margin:0 0 2px;font-size:9px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Mean Marks</p>'
        '            <div style="display:flex;align-items:baseline;gap:6px;">'
        f'              <p style="margin:0;font-size:18px;font-weight:800;color:#0f172a;font-family:"Plus Jakarta Sans",sans-serif;">{mean_marks}%</p>'
        f'              <span style="font-size:10px;font-weight:600;color:{trend_color};">{trend_icon} {abs(marks_trend)}%</span>'
        '            </div>'
        '          </div>'

        # Card 2: Total Points
        '          <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);display:flex;flex-direction:column;justify-content:center;">'
        '            <p style="margin:0 0 2px;font-size:9px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Total Points</p>'
        '            <div style="display:flex;align-items:baseline;gap:6px;">'
        f'              <p style="margin:0;font-size:18px;font-weight:800;color:#0f172a;font-family:"Plus Jakarta Sans",sans-serif;">{total_points}</p>'
        f'              <span style="font-size:9px;color:#94a3b8;font-weight:500;">/ {latest.subject_count * 12 if latest else 0}</span>'
        '            </div>'
        '          </div>'

        # Card 3: Overall Position
        '          <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);display:flex;flex-direction:column;justify-content:center;">'
        '            <p style="margin:0 0 2px;font-size:9px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Overall Position</p>'
        '            <div style="display:flex;align-items:baseline;gap:6px;">'
        f'              <p style="margin:0;font-size:18px;font-weight:800;color:#0f172a;font-family:"Plus Jakarta Sans",sans-serif;">{overall_pos}</p>'
        '            </div>'
        '          </div>'

        # Card 4: Stream Position
        '          <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:10px 14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);display:flex;flex-direction:column;justify-content:center;">'
        '            <p style="margin:0 0 2px;font-size:9px;font-weight:600;color:#64748b;letter-spacing:0.05em;text-transform:uppercase;">Stream Position</p>'
        '            <div style="display:flex;align-items:baseline;gap:6px;">'
        f'              <p style="margin:0;font-size:18px;font-weight:800;color:#0f172a;font-family:"Plus Jakarta Sans",sans-serif;">{stream_pos}</p>'
        '            </div>'
        '          </div>'

        '        </div>'
        '      </div>'

        # ═══ RIGHT COLUMN ═══
        '      <div style="position:relative;min-height:0;">'

        # ── Chart Canvas with inline data ──
        '        <div style="position:absolute;inset:0;">'
        + (f'          <canvas id="performanceLineChart" '
           f'data-labels=\'{chart_labels_json}\' '
           f'data-student-data=\'{chart_student_data_json}\' '
           f'data-form-data=\'{chart_form_data_json}\' '
           f'data-target-data=\'{chart_target_data_json}\' '
           f'data-student-name="{chart_student_name}" '
           f'data-class-label="{chart_class_label}"></canvas>'
           if has_chart_data else
           '          <div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px;">'
           '            <svg width="48" height="48" fill="none" stroke="#cbd5e1" stroke-width="1.5" viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M7 16l4-5 4 3 5-7"/></svg>'
           '            <p style="margin:0;color:#94a3b8;font-size:14px;font-weight:500;font-family:Inter,sans-serif;">No performance data records available for this exam term session.</p>'
           '          </div>'
          )
        + '        </div>'

        '      </div>'

        '    </div>'
        '  </div>'

        # ── Subject Performance Table (Full Width) ──
        '  <div style="padding:0 40px 32px 40px;">'
        '    <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;">'
        '      <div style="padding:20px 24px 0 24px;">'
        '        <h3 style="margin:0;color:#0f172a;font-size:17px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;">Subject Performance</h3>'
        '      </div>'
        '      <table style="width:100%;border-collapse:collapse;margin-top:12px;">'
        '        <thead>'
        '          <tr style="border-bottom:1px solid #e2e8f0;">'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Name</th>'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Marks</th>'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Dev Exam <span style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:#e2e8f0;color:#64748b;font-size:10px;font-weight:700;cursor:help;">i</span></th>'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Dev Target <span style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:#e2e8f0;color:#64748b;font-size:10px;font-weight:700;cursor:help;">i</span></th>'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Grade</th>'
        '            <th style="padding:10px 20px;text-align:left;font-size:12px;font-weight:600;color:#94a3b8;">Class rank</th>'
        '          </tr>'
        '        </thead>'
        f'        <tbody>{subject_perf_rows}</tbody>'
        '      </table>'
        '    </div>'
        '  </div>'

        # ── Performance Over Time Chart (Full Width) ──
        '  <div style="padding:0 40px 32px 40px;">'
        '    <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;padding:24px;">'
        '      <h3 style="margin:0 0 16px;color:#0f172a;font-size:17px;font-family:\'Plus Jakarta Sans\',sans-serif;font-weight:700;">Performance Over Time</h3>'
        '      <div style="position:relative;height:280px;">'
        + (f'        <canvas id="performanceTimelineChart" data-tl-labels=\'{timeline_labels_json}\' data-tl-student=\'{timeline_student_json}\'></canvas>'
           if has_timeline_data else
           '          <div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px;">'
           '            <svg width="48" height="48" fill="none" stroke="#cbd5e1" stroke-width="1.5" viewBox="0 0 24 24"><path d="M3 3v18h18"/><path d="M7 16l4-5 4 3 5-7"/></svg>'
           '            <p style="margin:0;color:#94a3b8;font-size:14px;font-weight:500;font-family:Inter,sans-serif;">No performance data records available for this exam term session.</p>'
           '          </div>')
        + '      </div>'
        '    </div>'
        '  </div>'

        # ── Back to Student List Button ──
        '  <div style="padding:0 40px 40px 40px;display:flex;justify-content:flex-start;">'
        '    <button class="an-back-btn" hx-get="/school-admin/registration/search-submit/?tab=directory" hx-target="#search-card-container" hx-swap="innerHTML">'
        '      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/></svg>'
        '      Back'
        '    </button>'
        '  </div>'

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
            .filter(school=school, class_name=selected_grade, stream=selected_stream, is_active=True)
            .filter(admission_no__regex=r'^[0-9]+[PJ]$')
            .select_related('guardian')
            .annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
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


# ── HTMX API: section toggle for Add Student form ──────────────────────
from django.http import JsonResponse

def get_section_info(request):
    """Return next admission number and grade list for a given section."""
    section = request.GET.get('section', 'JSS').strip()
    if section not in ('PRIMARY', 'JSS'):
        section = 'JSS'
    next_adm = get_next_admission_no(school_section=section)
    if section == 'PRIMARY':
        grades = LOWER_PRIMARY_GRADE_CHOICES + PRIMARY_GRADE_CHOICES
    else:
        grades = GRADE_CHOICES
    return JsonResponse({'admission_no': next_adm, 'grades': grades})


@login_required(login_url='login')
@school_admin_required
def printouts_hub(request):
    """
    Premium Printouts hub page — card grid of all printable documents.
    """
    return render(request, 'students/printouts_hub.html')


@login_required(login_url='login')
@school_admin_required
def class_list_printout(request):
    """
    Class List printout page — Grade + Stream selection, then fetches students.
    Supports view_mode: 'admin' (guardian contacts) or 'teacher' (mark sheet grid).
    """
    from ..models import Grade, Stream
    from ..security.roles import user_has_main_school_admin_override

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    grades = Grade.all_objects.filter(school=school).order_by('order').values_list('name', flat=True).distinct()

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    view_mode = request.GET.get('view_mode', 'admin').strip()
    if view_mode not in ('admin', 'teacher'):
        view_mode = 'admin'

    # Access control: admin register requires admin or class teacher of that stream
    is_admin = user_has_main_school_admin_override(request.user)
    can_see_admin = is_admin
    if not is_admin and grade_name and stream_name:
        from ..models import Teacher
        teacher = Teacher.all_objects.filter(school=school, user=request.user, is_active=True).first()
        if teacher and teacher.assigned_task:
            task = teacher.assigned_task
            if task.startswith('Class Teacher'):
                remainder = task.replace('Class Teacher ', '').strip()
                if remainder == f'{grade_name} {stream_name}':
                    can_see_admin = True

    # Force teacher mode if not authorized for admin register
    if view_mode == 'admin' and not can_see_admin:
        view_mode = 'teacher'

    # Section-aware accent color based on grade
    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if grade_name in ['Grade 1', 'Grade 2', 'Grade 3']:
        section_accent = section_colors['LOWER_PRIMARY']
    elif grade_name in ['Grade 4', 'Grade 5', 'Grade 6']:
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    students = []
    streams = []
    selected_grade = grade_name
    selected_stream = stream_name

    if grade_name:
        streams = list(
            Stream.all_objects.filter(school=school, grade__name=grade_name)
            .values_list('name', flat=True).order_by('name')
        )

    if grade_name and stream_name:
        from django.db.models import CharField, Value
        from django.db.models.functions import Substr, Length
        from django.db.models import IntegerField
        from django.db.models.functions import Cast
        from ..models import Student

        if stream_name == 'Combined':
            qs = Student.all_objects.filter(
                school=school, class_name=grade_name, is_active=True
            )
        else:
            qs = Student.all_objects.filter(
                school=school, class_name=grade_name, stream=stream_name, is_active=True
            )
        qs = (
            qs.annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
            .order_by('adm_int')
        )
        for s in qs:
            students.append({
                'id': s.id,
                'admission_no': s.admission_no or '',
                'name': s.name or '',
                'gender': s.gender or '',
                'stream': s.stream or '',
                'assessment_no': s.assessment_no or '',
                'guardian_name': s.guardian.name if s.guardian else '',
                'guardian_phone': s.guardian.phone if s.guardian else '',
                'religion': s.religion or '',
            })

    return render(request, 'students/class_list_printout.html', {
        'grades': grades,
        'streams': streams,
        'students': students,
        'selected_grade': selected_grade,
        'selected_stream': selected_stream,
        'view_mode': view_mode,
        'can_see_admin': can_see_admin,
        'total_count': len(students),
        'boys_count': sum(1 for s in students if s['gender'] == 'Male'),
        'girls_count': sum(1 for s in students if s['gender'] == 'Female'),
        'section_accent': section_accent,
    })


@login_required(login_url='login')
@school_admin_required
def api_streams_for_grade_printout(request):
    """AJAX endpoint: returns streams for a given grade. Adds 'Combined' if 2+ streams."""
    from django.http import JsonResponse
    from ..models import Stream

    school = get_request_school(request)
    if not school:
        return JsonResponse({'streams': []})

    grade_name = request.GET.get('grade', '').strip()
    if not grade_name:
        return JsonResponse({'streams': []})

    streams = list(
        Stream.all_objects.filter(school=school, grade__name=grade_name)
        .values_list('name', flat=True).order_by('name')
    )
    if len(streams) > 1:
        streams.append('Combined')
    return JsonResponse({'streams': streams})


@login_required(login_url='login')
@school_admin_required
def score_sheet(request):
    """
    Score Sheet page — Form + Stream + Subject selection for entering marks.
    """
    from ..models import Grade, Stream, Subject

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    grades = Grade.all_objects.filter(school=school).order_by('order').values_list('name', flat=True).distinct()

    ctx = {
        'grades': grades,
        'school_name': school.name,
        'school_logo': school.logo.url if school.logo else '',
        'school_address': school.address or '',
        'school_phone': school.phone_number or '',
        'school_email': school.email or '',
        'school_motto': school.motto or '',
    }
    return render(request, 'students/score_sheet.html', ctx)


@login_required(login_url='login')
@school_admin_required
def analysis_report(request):
    from ..models import Grade

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    grades = Grade.all_objects.filter(school=school).order_by('order').values_list('name', flat=True).distinct()

    ctx = {
        'grades': grades,
    }
    return render(request, 'students/analysis_report.html', ctx)


@login_required(login_url='login')
@school_admin_required
def api_exams_for_class(request):
    """AJAX endpoint: returns exams for a given grade."""
    from django.http import JsonResponse
    from ..models import Exam

    school = get_request_school(request)
    if not school:
        return JsonResponse({'exams': []})

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    if not grade_name:
        return JsonResponse({'exams': []})

    qs = Exam.all_objects.filter(school=school, is_deleted=False)

    if grade_name.startswith('Grade 1') or grade_name.startswith('Grade 2') or grade_name.startswith('Grade 3'):
        qs = qs.filter(school_section='PRIMARY', sub_section='LOWER')
    elif grade_name.startswith('Grade 4') or grade_name.startswith('Grade 5') or grade_name.startswith('Grade 6'):
        qs = qs.filter(school_section='PRIMARY', sub_section='UPPER')
    else:
        qs = qs.filter(school_section='JSS')

    exams = list(qs.order_by('-year', 'term', 'name').values('id', 'name', 'term', 'year'))

    return JsonResponse({'exams': exams})


@login_required(login_url='login')
@school_admin_required
def api_subjects_for_grade(request):
    """AJAX endpoint: returns subjects for a given grade, grouped by section."""
    from django.http import JsonResponse
    from ..models import Subject

    school = get_request_school(request)
    if not school:
        return JsonResponse({'subjects': []})

    grade_name = request.GET.get('grade', '').strip()
    if not grade_name:
        return JsonResponse({'subjects': []})

    subjects = list(
        Subject.all_objects.filter(school=school, grade=grade_name, is_active=True)
        .order_by('school_section', 'name')
        .values('id', 'name', 'code', 'school_section')
    )
    import logging
    logging.getLogger('students').info(
        'api_subjects_for_grade: school=%s grade=%s count=%d',
        getattr(school, 'pk', None), grade_name, len(subjects),
    )
    return JsonResponse({'subjects': subjects})


@login_required(login_url='login')
@school_admin_required
def api_teacher_for_subject(request):
    """AJAX endpoint: returns the teacher assigned to a given grade+stream+subject."""
    from django.http import JsonResponse
    from ..models import SubjectAssignment

    school = get_request_school(request)
    if not school:
        return JsonResponse({'teacher': None})

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    subject_id = request.GET.get('subject_id', '').strip()

    if not grade_name or not subject_id:
        return JsonResponse({'teacher': None})

    assignment = SubjectAssignment.all_objects.filter(
        school=school,
        class_name=grade_name,
        subject_id=subject_id,
    )
    if stream_name:
        assignment = assignment.filter(stream=stream_name)

    assignment = assignment.select_related('teacher_profile__user').first()

    if assignment and assignment.teacher_profile and assignment.teacher_profile.user:
        teacher_name = assignment.teacher_profile.user.get_full_name() or assignment.teacher_profile.user.username
    else:
        teacher_name = None

    return JsonResponse({'teacher': teacher_name})


@login_required(login_url='login')
@school_admin_required
def api_analysis_data(request):
    """JSON endpoint: returns all analysis data for a given exam+class for inline report rendering."""
    import json
    from collections import Counter, defaultdict
    from django.db.models import Q
    from ..models import Exam, ExamSummary, Subject, Mark, Student

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'No school context'}, status=400)

    exam_id = request.GET.get('exam_id')
    class_name_filter = request.GET.get('class_name', '').strip()
    stream_filter = request.GET.get('stream', '').strip()

    if not exam_id:
        return JsonResponse({'error': 'exam_id required'}, status=400)

    exam = Exam.all_objects.filter(school=school, id=exam_id, is_deleted=False).first()
    if not exam:
        return JsonResponse({'error': 'Exam not found'}, status=404)

    section = exam.school_section
    PRIMARY_ORDERED_LEVELS = ['EE', 'ME', 'AE', 'BE']
    from .constants import ORDERED_LEVELS
    breakdown_levels = PRIMARY_ORDERED_LEVELS if section == 'PRIMARY' else ORDERED_LEVELS
    sub_section = exam.sub_section

    # Auto-detect class_name if not provided
    if not class_name_filter:
        auto_grades = (
            ExamSummary.all_objects.filter(
                school=school, exam_name=exam.name, term=exam.term, year=exam.year,
            )
            .values_list('student__class_name', flat=True)
            .distinct()
        )
        auto_grades_list = list(auto_grades)
        if auto_grades_list:
            class_name_filter = auto_grades_list[0]

    all_students = Student.all_objects.filter(school=school, is_active=True, status='Active')
    if class_name_filter:
        all_students = all_students.filter(class_name=class_name_filter)

    summaries_all = ExamSummary.all_objects.filter(
        school=school, exam_name=exam.name, term=exam.term, year=exam.year,
    )
    if class_name_filter:
        summaries_all = summaries_all.filter(student__class_name=class_name_filter)

    # summaries = stream-filtered subset for stats; summaries_all = full grade for chart
    summaries = summaries_all
    if stream_filter:
        summaries = summaries_all.filter(student__stream=stream_filter)

    # Detect section/sub_section from actual student data (not the exam)
    # Admin accounts have no section restrictions — use what the data tells us
    detected_sec = summaries_all.values_list('school_section', flat=True).distinct()
    detected_sec_list = [s for s in detected_sec if s]
    if detected_sec_list:
        section = detected_sec_list[0]
    detected_sub_vals = summaries_all.values_list('sub_section', flat=True).distinct()
    detected_sub_list = [s for s in detected_sub_vals if s and isinstance(s, str)]
    if detected_sub_list:
        sub_section = detected_sub_list[0]
    else:
        sub_section = None

    breakdown_levels = PRIMARY_ORDERED_LEVELS if section == 'PRIMARY' else ORDERED_LEVELS
    from .grading_engine import prefetch_school_grading, resolve_scale_fast, get_grading_scale
    prefetch_school_grading(school)
    grade_descriptors = resolve_scale_fast(school.pk, section, sub_section, subject_id=None, is_total_calculation=False)
    grading_scale_obj = get_grading_scale(school.pk, section, sub_section, subject_id=None)

    total_students = all_students.count()
    students_who_sat = summaries.values('student_id').distinct().count()
    student_ids = summaries.values_list('student_id', flat=True).distinct()

    # streams for chart = ALL streams in the grade; streams for breakdown = filtered
    streams_all = list(
        summaries_all.values_list('student__stream', flat=True).distinct().order_by('student__stream')
    )
    streams = list(
        summaries.values_list('student__stream', flat=True).distinct().order_by('student__stream')
    )
    grade_name = summaries_all.values_list('student__class_name', flat=True).first() or exam.name

    student_ids_all = summaries_all.values_list('student_id', flat=True).distinct()

    all_marks = Mark.all_objects.filter(
        student__school=school,
        student_id__in=student_ids_all,
        term=exam.term,
        year=exam.year,
        exam_type=exam.name,
    ).select_related('subject', 'student')

    # filtered marks = for stats only (selected stream)
    filtered_marks = Mark.all_objects.filter(
        student__school=school,
        student_id__in=student_ids,
        term=exam.term,
        year=exam.year,
        exam_type=exam.name,
    ).select_related('subject', 'student')

    PLV_LABELS = {
        'EE1': 'Exceeding Expectations', 'EE2': 'Exceeding Expectations',
        'ME1': 'Meeting Expectations', 'ME2': 'Meeting Expectations',
        'AE1': 'Approaching Expectations', 'AE2': 'Approaching Expectations',
        'BE1': 'Below Expectations', 'BE2': 'Below Expectations',
        'EE': 'Exceeding Expectations', 'ME': 'Meeting Expectations',
        'AE': 'Approaching Expectations', 'BE': 'Below Expectations',
    }

    # ── Subject performance ──────────────────────────────────────────────
    subject_perf = {}
    for mark in filtered_marks:
        if mark.subject_id and mark.subject:
            subj_name = mark.subject.name
            if subj_name not in subject_perf:
                subject_perf[subj_name] = {'total_points': 0, 'count': 0, 'scores': []}
            subject_perf[subj_name]['total_points'] += mark.points
            subject_perf[subj_name]['count'] += 1

    subject_rows = []
    for subj_name, data in sorted(subject_perf.items()):
        if data['count'] > 0:
            mean_pts = data['total_points'] / data['count']
            plv = '-'
            if grade_descriptors:
                for ld in grade_descriptors:
                    if ld.get('points') == round(mean_pts):
                        plv = ld.get('level', '-')
                        break
            subject_rows.append({
                'name': subj_name,
                'points': round(mean_pts, 4),
                'change': 0.0,
                'performance_level': plv,
                'performance_level_label': PLV_LABELS.get(plv, plv),
            })
    subject_rows.sort(key=lambda x: x['points'], reverse=True)

    # ── Previous exam for change calculation ─────────────────────────────
    TERM_ORDER = {'Term 1': 1, 'Term 2': 2, 'Term 3': 3}
    EXAM_ORDER = {'Opener Assessment': 1, 'Mid Term Assessment': 2, 'End Term Assessment': 3}
    all_exams = Exam.all_objects.filter(school=school, is_deleted=False)
    exam_list = []
    for ex in all_exams:
        to = TERM_ORDER.get(ex.term, 0)
        eo = EXAM_ORDER.get(ex.name, 4)
        exam_list.append((ex.year, to, eo, ex))
    exam_list.sort(key=lambda x: (x[0], x[1], x[2]))
    prev_exam = None
    for i, (yr, tn, en, ex) in enumerate(exam_list):
        if ex.id == exam.id and i > 0:
            prev_exam = exam_list[i-1][3]
            break
    prev_mean_marks = 0
    prev_mean_points = 0
    prev_subject_perf = {}
    if prev_exam:
        prev_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=prev_exam.name, term=prev_exam.term, year=prev_exam.year,
        )
        if class_name_filter:
            prev_summaries = prev_summaries.filter(student__class_name=class_name_filter)
        prev_student_ids = prev_summaries.values_list('student_id', flat=True).distinct()
        prev_all_marks = Mark.all_objects.filter(
            student__school=school, student_id__in=prev_student_ids,
            term=prev_exam.term, year=prev_exam.year, exam_type=prev_exam.name,
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
        for mark in prev_all_marks:
            if mark.subject_id and mark.subject:
                sn = mark.subject.name
                if sn not in prev_subject_perf:
                    prev_subject_perf[sn] = {'total_points': 0, 'count': 0}
                prev_subject_perf[sn]['total_points'] += mark.points
                prev_subject_perf[sn]['count'] += 1

    for row in subject_rows:
        pd = prev_subject_perf.get(row['name'])
        if pd and pd['count'] > 0:
            row['change'] = round(row['points'] - pd['total_points'] / pd['count'], 4)

    # ── Stream stats (for chart — ALL streams) ────────────────────────────
    stream_stats = {}
    for s in streams_all:
        s_ids = summaries_all.filter(student__stream=s).values_list('student_id', flat=True).distinct()
        s_marks = all_marks.filter(student_id__in=s_ids)
        total_pts = sum(m.points for m in s_marks)
        count = s_marks.count() if s_marks.count() > 0 else 1
        total_marks_sum = sum(m.score for m in s_marks)
        stream_stats[s] = {
            'mean_points': round(total_pts / count, 4),
            'mean_marks': round(total_marks_sum / count, 1),
            'entries': len(s_ids),
        }

    # Overall = from selected stream (summary card data)
    if stream_filter and stream_filter in stream_stats:
        overall_mean_points = stream_stats[stream_filter]['mean_points']
        overall_mean_marks = stream_stats[stream_filter]['mean_marks']
    else:
        all_stream_pts = [v['mean_points'] for v in stream_stats.values()]
        overall_mean_points = round(sum(all_stream_pts) / len(all_stream_pts), 4) if all_stream_pts else 0
        all_stream_marks = [v['mean_marks'] for v in stream_stats.values()]
        overall_mean_marks = round(sum(all_stream_marks) / len(all_stream_marks), 1) if all_stream_marks else 0
    mm_change = round(overall_mean_marks - prev_mean_marks, 1) if prev_mean_marks else 0
    mp_change = round(overall_mean_points - prev_mean_points, 4) if prev_mean_points else 0

    # ── Overall PLV ──────────────────────────────────────────────────────
    overall_plv = '-'
    if grading_scale_obj and grading_scale_obj.total_scale:
        total_marks_800 = overall_mean_marks * 8
        for ld in grading_scale_obj.total_scale:
            if ld.get('min_marks', 0) <= total_marks_800 <= ld.get('max_marks', 0):
                overall_plv = ld.get('level', '-')
                break

    # ── Grade breakdown ──────────────────────────────────────────────────
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
            for ld in grading_scale_obj.total_scale:
                if ld.get('min_marks', 0) <= total_m <= ld.get('max_marks', 0):
                    row['performance_level'] = ld.get('level', '-')
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
            overall_plv = Counter(all_plvs).most_common(1)[0][0]

    total_row = {
        'form': grade_name, 'X': 0, 'Y': 0,
        'entries': sum(r['entries'] for r in grade_breakdown),
        'mean_marks': overall_mean_marks, 'mm_dev': 0,
        'mean_points': overall_mean_points, 'mp_dev': 0,
        'performance_level': overall_plv,
    }
    for lvl in breakdown_levels:
        total_row[lvl] = sum(r.get(lvl, 0) for r in grade_breakdown)

    # ── Subject breakdowns (per stream) ──────────────────────────────────
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
                        for ld in grade_descriptors:
                            converted = (m.score / m.maximum_marks * 100) if m.maximum_marks else 0
                            if ld.get('min_marks', 0) <= converted <= ld.get('max_marks', 0):
                                plv_key = ld.get('level', '-')
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
                for ld in grade_descriptors:
                    if ld.get('points') == round(mean_pts):
                        row['performance_level'] = ld.get('level', '-')
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

    # ── Gender breakdown (overall) ───────────────────────────────────────
    gender_streams = {}
    for s in streams:
        s_student_ids = list(
            summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
        )
        s_students = Student.all_objects.filter(id__in=s_student_ids)
        girls = s_students.filter(gender='Female').count()
        boys = s_students.filter(gender='Male').count()
        gender_streams[s] = {'girls': girls, 'boys': boys}
    total_girls = sum(g['girls'] for g in gender_streams.values())
    total_boys = sum(g['boys'] for g in gender_streams.values())

    # ── Per-subject gender breakdown ─────────────────────────────────────
    subject_gender = {}
    for subj_name in subject_names:
        sg = {}
        for s in streams:
            s_ids_list = list(
                summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
            )
            subj_marks = all_marks.filter(student_id__in=s_ids_list, subject__name=subj_name)
            marked_ids = list(subj_marks.values_list('student_id', flat=True).distinct())
            subj_students = Student.all_objects.filter(id__in=marked_ids)
            sg[s] = {
                'girls': subj_students.filter(gender='Female').count(),
                'boys': subj_students.filter(gender='Male').count(),
            }
        subject_gender[subj_name] = sg

    # ── Top students ─────────────────────────────────────────────────────
    student_totals = defaultdict(lambda: {'total_score': 0, 'total_points': 0, 'subject_count': 0, 'student_id': None})
    for mark in all_marks:
        st = student_totals[mark.student_id]
        st['total_score'] += mark.score
        st['total_points'] += mark.points
        st['subject_count'] += 1
        st['student_id'] = mark.student_id

    student_summaries_map = {}
    for summ in summaries:
        student_summaries_map[summ.student_id] = summ

    ranked_students = []
    for sid, data in student_totals.items():
        summ = student_summaries_map.get(sid)
        if not summ or data['subject_count'] == 0:
            continue
        mean_marks = data['total_score'] / data['subject_count']
        mean_points = data['total_points'] / data['subject_count']
        student_obj = Student.all_objects.filter(id=sid).first()
        if not student_obj:
            continue
        student_name = ''
        if student_obj.user:
            student_name = student_obj.user.get_full_name() or student_obj.user.username
        else:
            student_name = student_obj.name or student_obj.admission_no or f'Student {sid}'
        plv = '-'
        if grading_scale_obj and grading_scale_obj.total_scale:
            total_m80 = mean_marks * 8
            for ld in grading_scale_obj.total_scale:
                if ld.get('min_marks', 0) <= total_m80 <= ld.get('max_marks', 0):
                    plv = ld.get('level', '-')
                    break
        ranked_students.append({
            'admission_no': student_obj.admission_no or '',
            'name': student_name,
            'stream': student_obj.stream or '',
            'gender': student_obj.gender or 'Not Specified',
            'mean_marks': round(mean_marks, 1),
            'mean_points': round(mean_points, 4),
            'total_marks': data['total_score'],
            'performance_level': plv,
            'performance_level_label': PLV_LABELS.get(plv, plv),
        })

    ranked_students.sort(key=lambda x: x['mean_marks'], reverse=True)
    for i, st in enumerate(ranked_students):
        st['rank'] = i + 1

    # Stream rank
    stream_counters = Counter()
    for st in ranked_students:
        stream_counters[st['stream']] += 1
        st['stream_rank'] = stream_counters[st['stream']]

    top_students = ranked_students[:3]
    top_boys = [s for s in ranked_students if s['gender'] == 'Male'][:3]
    top_girls = [s for s in ranked_students if s['gender'] == 'Female'][:3]

    # ── Performance over time ────────────────────────────────────────────
    pot_exams = Exam.all_objects.filter(school=school, is_deleted=False).order_by('year', 'term', 'name')
    seen_pot_names = set()
    pot_unique_exams = []
    for pe in pot_exams:
        key = (pe.name, pe.term, pe.year)
        if key in seen_pot_names:
            continue
        pe_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=pe.name, term=pe.term, year=pe.year,
        )
        if class_name_filter:
            pe_summaries = pe_summaries.filter(student__class_name=class_name_filter)
        if pe_summaries.exists():
            seen_pot_names.add(key)
            pot_unique_exams.append(pe)

    pot_labels = []
    pot_streams_data = {s: [] for s in streams_all}
    for pot_exam in pot_unique_exams:
        pot_summaries = ExamSummary.all_objects.filter(
            school=school, exam_name=pot_exam.name, term=pot_exam.term, year=pot_exam.year,
        )
        if class_name_filter:
            pot_summaries = pot_summaries.filter(student__class_name=class_name_filter)
        if not pot_summaries.exists():
            continue
        pot_labels.append(f"{grade_name} {pot_exam.term}, {pot_exam.name}, {pot_exam.year}")
        pot_all_marks = Mark.all_objects.filter(
            student__school=school,
            student_id__in=pot_summaries.values_list('student_id', flat=True).distinct(),
            term=pot_exam.term, year=pot_exam.year, exam_type=pot_exam.name,
        )
        for s in streams_all:
            s_ids = pot_summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
            s_marks = pot_all_marks.filter(student_id__in=s_ids)
            total_pts = sum(m.points for m in s_marks)
            count = s_marks.count() if s_marks.count() > 0 else 1
            pot_streams_data[s].append(round(total_pts / count, 4))

    # ── Exam groups for dropdown ─────────────────────────────────────────
    all_exams_for_school = Exam.all_objects.filter(school=school, year=exam.year, is_deleted=False).order_by('-year', 'term', 'name')
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
        exam_groups_dict[term_label].append({'id': ex.id, 'name': ex.name})
        seen_exam_keys.add(key)
    exam_groups = [{'label': k, 'exams': v} for k, v in exam_groups_dict.items()]

    available_grades = list(
        Student.all_objects.filter(school=school, is_active=True, status='Active')
        .values_list('class_name', flat=True).distinct().order_by('class_name')
    )

    # ── Subject teachers ─────────────────────────────────────────────────
    from ..models import SubjectAssignment
    subject_teachers = {}
    for subj_name in subject_names:
        assignment = SubjectAssignment.all_objects.filter(
            school=school, class_name=grade_name, subject__name=subj_name,
        ).select_related('teacher_profile__user').first()
        if assignment and assignment.teacher_profile and assignment.teacher_profile.user:
            subject_teachers[subj_name] = assignment.teacher_profile.user.get_full_name() or assignment.teacher_profile.user.username
        else:
            subject_teachers[subj_name] = '-'

    data = {
        'school': {
            'name': school.name,
            'logo': school.logo.url if school.logo else '',
            'address': school.address or '',
            'phone': school.phone_number or '',
            'email': school.email or '',
            'motto': school.motto or '',
        },
        'exam': {
            'id': exam.id,
            'name': exam.name,
            'term': exam.term,
            'year': exam.year,
        },
        'grade_name': grade_name,
        'stream_filter': stream_filter or '',
        'streams': streams,
        'streams_all': streams_all,
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
        'subject_breakdowns': subject_breakdowns,
        'subject_names': subject_names,
        'gender_streams': gender_streams,
        'total_girls': total_girls,
        'total_boys': total_boys,
        'top_students': top_students,
        'top_boys': top_boys,
        'top_girls': top_girls,
        'subject_gender': subject_gender,
        'subject_teachers': subject_teachers,
        'pot_labels': pot_labels,
        'pot_streams_data': pot_streams_data,
        'plv_labels': PLV_LABELS,
        'exam_groups': exam_groups,
        'available_grades': available_grades,
        'current_exam_id': exam.id,
        'all_ranked_students': ranked_students,
    }
    return JsonResponse(data)
