"""
Class and stream management views for school administrators.

Provides CRUD operations for grades (Grade 1-12) and their streams,
with automatic single-stream naming and enrollment checks.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from ..security import get_request_school, school_admin_required


# Section-to-grade mapping
ALL_GRADES = [f'Grade {i}' for i in range(1, 10)]
GRADE_ORDER = {f'Grade {i}': i for i in range(1, 10)}


@login_required(login_url='login')
@school_admin_required
def manage_classes(request):
    """
    School admin view to manage grades and streams.
    Admin sees ALL grades across all sections with no restrictions.
    """
    from ..models import Grade, Stream

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')

        # ── Add a new grade ───────────────────────────────────────────────────
        if action == 'add_grade':
            grade_name = request.POST.get('grade_name', '').strip()
            if not grade_name:
                messages.error(request, "Please select a grade.")
                return redirect('manage_classes')

            if Grade.all_objects.filter(school=school, name=grade_name).exists():
                messages.error(request, f"{grade_name} already exists for this school.")
                return redirect('manage_classes')

            # Auto-detect section from grade number
            grade_num = int(grade_name.replace('Grade ', ''))
            if grade_num <= 3:
                db_section = 'PRIMARY'
                sub_section = 'LOWER'
            elif grade_num <= 6:
                db_section = 'PRIMARY'
                sub_section = 'UPPER'
            else:
                db_section = 'JSS'
                sub_section = None

            grade = Grade.all_objects.create(
                school=school,
                name=grade_name,
                school_section=db_section,
                sub_section=sub_section,
                order=GRADE_ORDER.get(grade_name, 99),
            )
            Stream.all_objects.create(
                school=school,
                grade=grade,
                name='Main',
                school_section=db_section,
            )
            messages.success(request, f"{grade_name} created with one stream: Main.")
            return redirect('manage_classes')

        # ── Add a stream to an existing grade ─────────────────────────────────
        elif action == 'add_stream':
            grade_id = request.POST.get('grade_id')
            stream_name = request.POST.get('stream_name', '').strip().title()

            if not stream_name:
                messages.error(request, "Stream name cannot be empty.")
                return redirect('manage_classes')

            try:
                grade = Grade.all_objects.get(id=grade_id, school=school)
            except Grade.DoesNotExist:
                messages.error(request, "Grade not found.")
                return redirect('manage_classes')

            if Stream.all_objects.filter(school=school, grade=grade, name=stream_name).exists():
                messages.error(request, f"Stream '{stream_name}' already exists in {grade.name}.")
                return redirect('manage_classes')

            Stream.all_objects.create(
                school=school,
                grade=grade,
                name=stream_name,
                school_section=grade.school_section,
            )
            messages.success(request, f"Stream '{stream_name}' added to {grade.name}.")
            return redirect('manage_classes')

        # ── Rename a stream ───────────────────────────────────────────────────
        elif action == 'rename_stream':
            stream_id = request.POST.get('stream_id')
            new_name = request.POST.get('new_name', '').strip().title()

            if not new_name:
                messages.error(request, "Stream name cannot be empty.")
                return redirect('manage_classes')

            try:
                stream = Stream.all_objects.get(id=stream_id, school=school)
            except Stream.DoesNotExist:
                messages.error(request, "Stream not found.")
                return redirect('manage_classes')

            old_name = stream.name
            stream.name = new_name
            stream.save()
            messages.success(request, f"Stream renamed from '{old_name}' to '{new_name}'.")
            return redirect('manage_classes')

        # ── Delete a stream ───────────────────────────────────────────────────
        elif action == 'delete_stream':
            stream_id = request.POST.get('stream_id')
            try:
                stream = Stream.all_objects.get(id=stream_id, school=school)
            except Stream.DoesNotExist:
                messages.error(request, "Stream not found.")
                return redirect('manage_classes')

            from ..models import Student
            student_count = Student.all_objects.filter(
                school=school,
                class_name=stream.grade.name,
                stream=stream.name,
                is_active=True,
            ).count()

            if student_count > 0:
                messages.error(
                    request,
                    f"Cannot delete '{stream.name}' — {student_count} student(s) are still enrolled in it. "
                    f"Move or remove them first."
                )
                return redirect('manage_classes')

            grade_name = stream.grade.name
            stream_name = stream.name
            stream.delete()
            messages.success(request, f"Stream '{stream_name}' removed from {grade_name}.")
            return redirect('manage_classes')

        # ── Delete a grade ────────────────────────────────────────────────────
        elif action == 'delete_grade':
            grade_id = request.POST.get('grade_id')
            try:
                grade = Grade.all_objects.get(id=grade_id, school=school)
            except Grade.DoesNotExist:
                messages.error(request, "Grade not found.")
                return redirect('manage_classes')

            from ..models import Student
            student_count = Student.all_objects.filter(
                school=school,
                class_name=grade.name,
                is_active=True,
            ).count()

            if student_count > 0:
                messages.error(
                    request,
                    f"Cannot delete {grade.name} — {student_count} student(s) are enrolled in it. "
                    f"Move or remove them first."
                )
                return redirect('manage_classes')

            grade_name = grade.name
            grade.delete()
            messages.success(request, f"{grade_name} and all its streams have been deleted.")
            return redirect('manage_classes')

    # ── GET — build context ───────────────────────────────────────────────────
    from ..models import Student, Teacher

    grades = (
        Grade.all_objects
        .filter(school=school)
        .prefetch_related('streams')
        .order_by('order')
    )

    existing_grade_names = set(grades.values_list('name', flat=True))
    available_grades = [g for g in ALL_GRADES if g not in existing_grade_names]

    active_tab = request.GET.get('tab', 'manage')

    # ── Build grade rows for the table ────────────────────────────────────────
    grade_rows = []
    total_boys = 0
    total_girls = 0
    total_students = 0

    for grade in grades:
        students_qs = Student.all_objects.filter(
            school=school, class_name=grade.name, is_active=True
        )
        boys = students_qs.filter(gender='Male').count()
        girls = students_qs.filter(gender='Female').count()
        total = students_qs.count()

        # Class supervisor: teacher whose assigned_task mentions this grade
        supervisor = ''
        ct = Teacher.all_objects.filter(
            school=school,
            is_active=True,
            assigned_task__icontains=grade.name,
        ).filter(
            assigned_task__icontains='Class Teacher'
        ).filter(
            school_section=grade.school_section,
            sub_section=grade.sub_section,
        ).first()
        if ct:
            supervisor = ct.get_full_title()

        grade_rows.append({
            'id': grade.id,
            'name': grade.name,
            'boys': boys,
            'girls': girls,
            'total': total,
            'supervisor': supervisor,
            'stream_count': grade.streams.count(),
        })

        total_boys += boys
        total_girls += girls
        total_students += total

    return render(request, 'students/manage_classes.html', {
        'grades': grades,
        'available_grades': available_grades,
        'active_tab': active_tab,
        'grade_rows': grade_rows,
        'total_boys': total_boys,
        'total_girls': total_girls,
        'total_students': total_students,
    })


@login_required(login_url='login')
@school_admin_required
def manage_streams(request, grade_id):
    """
    School admin view to manage streams within a specific grade.
    Displays streams table with boy/girl counts, class teacher, and CRUD actions.
    """
    from ..models import Grade, Stream, Student, Teacher

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    try:
        grade = Grade.all_objects.get(id=grade_id, school=school)
    except Grade.DoesNotExist:
        messages.error(request, "Grade not found.")
        return redirect('manage_classes')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_stream':
            stream_name = request.POST.get('stream_name', '').strip().title()
            if not stream_name:
                messages.error(request, "Stream name cannot be empty.")
                return redirect('manage_streams', grade_id=grade.id)

            if Stream.all_objects.filter(school=school, grade=grade, name=stream_name).exists():
                messages.error(request, f"Stream '{stream_name}' already exists in {grade.name}.")
                return redirect('manage_streams', grade_id=grade.id)

            Stream.all_objects.create(
                school=school,
                grade=grade,
                name=stream_name,
                school_section=grade.school_section,
            )
            messages.success(request, f"Stream '{stream_name}' added to {grade.name}.")
            return redirect('manage_streams', grade_id=grade.id)

        elif action == 'rename_stream':
            stream_id = request.POST.get('stream_id')
            new_name = request.POST.get('new_name', '').strip().title()
            if not new_name:
                messages.error(request, "Stream name cannot be empty.")
                return redirect('manage_streams', grade_id=grade.id)
            try:
                stream = Stream.all_objects.get(id=stream_id, school=school)
            except Stream.DoesNotExist:
                messages.error(request, "Stream not found.")
                return redirect('manage_streams', grade_id=grade.id)
            old_name = stream.name
            stream.name = new_name
            stream.save()
            messages.success(request, f"Stream renamed from '{old_name}' to '{new_name}'.")
            return redirect('manage_streams', grade_id=grade.id)

        elif action == 'delete_stream':
            stream_id = request.POST.get('stream_id')
            try:
                stream = Stream.all_objects.get(id=stream_id, school=school)
            except Stream.DoesNotExist:
                messages.error(request, "Stream not found.")
                return redirect('manage_streams', grade_id=grade.id)
            student_count = Student.all_objects.filter(
                school=school, class_name=grade.name, stream=stream.name, is_active=True,
            ).count()
            if student_count > 0:
                messages.error(
                    request,
                    f"Cannot delete '{stream.name}' — {student_count} student(s) are still enrolled. Move them first.",
                )
                return redirect('manage_streams', grade_id=grade.id)
            stream_name = stream.name
            stream.delete()
            messages.success(request, f"Stream '{stream_name}' removed from {grade.name}.")
            return redirect('manage_streams', grade_id=grade.id)

    streams = Stream.all_objects.filter(school=school, grade=grade).order_by('name')

    stream_rows = []
    total_boys = 0
    total_girls = 0
    total_students = 0

    for stream in streams:
        students_qs = Student.all_objects.filter(
            school=school, class_name=grade.name, stream=stream.name, is_active=True,
        )
        boys = students_qs.filter(gender='Male').count()
        girls = students_qs.filter(gender='Female').count()
        total = students_qs.count()

        supervisor = ''
        ct = Teacher.all_objects.filter(
            school=school, is_active=True,
            assigned_task__icontains=grade.name,
        ).filter(
            assigned_task__icontains=stream.name,
        ).filter(
            assigned_task__icontains='Class Teacher',
        ).filter(
            school_section=grade.school_section,
            sub_section=grade.sub_section,
        ).first()
        if ct:
            supervisor = ct.get_full_title()

        stream_rows.append({
            'id': stream.id,
            'name': stream.name,
            'boys': boys,
            'girls': girls,
            'total': total,
            'supervisor': supervisor,
        })
        total_boys += boys
        total_girls += girls
        total_students += total

    return render(request, 'students/manage_streams.html', {
        'grade': grade,
        'stream_rows': stream_rows,
        'total_boys': total_boys,
        'total_girls': total_girls,
        'total_students': total_students,
    })


@login_required(login_url='login')
@school_admin_required
def api_class_list(request):
    """JSON endpoint: returns student list for a given grade+stream.
    For CRE/IRE/HRE subjects, filters by Student.religion tag.
    If no students are tagged yet, returns all (first-time behavior).
    Cached in Redis for SCORE_SHEET_CACHE_TTL.
    """
    from django.http import JsonResponse
    from django.core.cache import cache
    from ..models import Student, Stream, Subject
    from .constants import RELIGION_SUBJECTS, RELIGION_TAG

    school = get_request_school(request)
    if not school:
        return JsonResponse({'students': [], 'has_multiple_streams': False})

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    subject_id = request.GET.get('subject_id', '').strip()

    if not grade_name:
        return JsonResponse({'students': [], 'has_multiple_streams': False})

    # Build cache key including subject_id (religion filtering changes results)
    cache_key = f'score_sheet:class_list:{school.pk}:{grade_name}:{stream_name}:{subject_id}'
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    stream_count = Stream.all_objects.filter(school=school, grade__name=grade_name).count()
    has_multiple = stream_count > 1

    students = Student.all_objects.filter(
        school=school,
        class_name=grade_name,
        is_active=True,
    )
    if stream_name:
        students = students.filter(stream=stream_name)

    # --- Religion-aware filtering for CRE/IRE/HRE ---
    religion_tag = None
    if subject_id:
        try:
            subject_obj = Subject.all_objects.get(id=int(subject_id), school=school)
            subject_code = subject_obj.code
            if subject_code in RELIGION_SUBJECTS:
                religion_tag = RELIGION_TAG.get(subject_code, '')
        except (Subject.DoesNotExist, ValueError, TypeError):
            pass

    if religion_tag:
        tagged = students.filter(religion=religion_tag)
        if tagged.exists():
            students = tagged

    from django.db.models import CharField, Value
    from django.db.models.functions import Substr, Length
    from django.db.models import IntegerField
    from django.db.models.functions import Cast

    students = (
        students
        .annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
        .order_by('adm_int')
    )

    student_list = []
    for s in students:
        student_list.append({
            'id': s.id,
            'admission_no': s.admission_no or '',
            'name': s.name or '',
            'stream': s.stream or '',
            'gender': s.gender or '',
            'assessment_no': s.assessment_no or '',
            'guardian_name': s.guardian.name if s.guardian else '',
            'guardian_phone': s.guardian.phone if s.guardian else '',
        })

    result = {'students': student_list, 'has_multiple_streams': has_multiple}
    cache.set(cache_key, result, 300)  # 5 min TTL
    return JsonResponse(result)


@login_required(login_url='login')
@school_admin_required
def manage_subjects(request, grade_id, stream_name):
    """
    School admin view to manage subjects within a specific stream.
    Shows subjects table with teacher assignments, section-filtered teacher dropdowns.
    """
    from django.http import JsonResponse
    from ..models import Grade, Stream, Subject, SubjectAssignment, Student, Teacher

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    try:
        grade = Grade.all_objects.get(id=grade_id, school=school)
    except Grade.DoesNotExist:
        messages.error(request, "Grade not found.")
        return redirect('manage_classes')

    stream_name = stream_name.strip()
    if not Stream.all_objects.filter(school=school, grade=grade, name=stream_name).exists():
        messages.error(request, f"Stream '{stream_name}' not found in {grade.name}.")
        return redirect('manage_streams', grade_id=grade.id)

    grade_num = int(grade.name.replace('Grade ', ''))
    if grade_num <= 3:
        db_section = 'PRIMARY'
        db_sub_section = 'LOWER'
    elif grade_num <= 6:
        db_section = 'PRIMARY'
        db_sub_section = 'UPPER'
    else:
        db_section = 'JSS'
        db_sub_section = None

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_subject':
            subject_id = request.POST.get('subject_id')
            try:
                subject = Subject.all_objects.get(id=subject_id, school=school, grade=grade.name)
            except Subject.DoesNotExist:
                messages.error(request, "Subject not found.")
                return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

            if SubjectAssignment.all_objects.filter(
                school=school, subject=subject, class_name=grade.name, stream=stream_name,
            ).exists():
                messages.error(request, f"'{subject.name}' is already added to {grade.name} {stream_name}.")
                return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

            teacher_id = request.POST.get('teacher_id')
            teacher = None
            if teacher_id:
                try:
                    teacher = Teacher.all_objects.get(id=teacher_id, school=school)
                except Teacher.DoesNotExist:
                    pass

            SubjectAssignment.all_objects.create(
                school=school,
                subject=subject,
                teacher_profile=teacher,
                class_name=grade.name,
                stream=stream_name,
                school_section=db_section,
                sub_section=db_sub_section,
            )
            msg = f"'{subject.name}' added to {grade.name} {stream_name}"
            if teacher:
                msg += f" with {teacher.get_full_title()}"
            messages.success(request, msg + ".")
            return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

        elif action == 'assign_teacher':
            assignment_id = request.POST.get('assignment_id')
            teacher_id = request.POST.get('teacher_id')
            try:
                assignment = SubjectAssignment.all_objects.get(id=assignment_id, school=school)
                teacher = Teacher.all_objects.get(id=teacher_id, school=school)
            except (SubjectAssignment.DoesNotExist, Teacher.DoesNotExist):
                messages.error(request, "Assignment or teacher not found.")
                return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

            assignment.teacher_profile = teacher
            assignment.save()
            messages.success(request, f"{teacher.get_full_title()} assigned to {assignment.subject.name}.")
            return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

        elif action == 'unassign_teacher':
            assignment_id = request.POST.get('assignment_id')
            try:
                assignment = SubjectAssignment.all_objects.get(id=assignment_id, school=school)
            except SubjectAssignment.DoesNotExist:
                messages.error(request, "Assignment not found.")
                return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

            subj_name = assignment.subject.name
            assignment.teacher_profile = None
            assignment.save()
            messages.success(request, f"Teacher unassigned from {subj_name}.")
            return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

        elif action == 'delete_subject':
            assignment_id = request.POST.get('assignment_id')
            try:
                assignment = SubjectAssignment.all_objects.get(id=assignment_id, school=school)
            except SubjectAssignment.DoesNotExist:
                messages.error(request, "Subject assignment not found.")
                return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

            subj_name = assignment.subject.name
            assignment.delete()
            messages.success(request, f"'{subj_name}' removed from {grade.name} {stream_name}.")
            return redirect('manage_subjects', grade_id=grade.id, stream_name=stream_name)

    assignments = SubjectAssignment.all_objects.filter(
        school=school, class_name=grade.name, stream=stream_name, is_active=True,
    ).select_related('subject', 'teacher_profile').order_by('subject__code')

    assigned_subject_ids = set(a.subject_id for a in assignments)
    available_subjects = Subject.all_objects.filter(
        school=school, grade=grade.name, is_active=True,
    ).exclude(id__in=assigned_subject_ids).order_by('code')

    if db_sub_section:
        teachers = Teacher.all_objects.filter(
            school=school, is_active=True,
        ).filter(
            school_section__in=[db_section, 'BOTH'],
            sub_section__in=[db_sub_section, None],
        ).order_by('user__first_name')
    else:
        teachers = Teacher.all_objects.filter(
            school=school, is_active=True,
        ).filter(
            school_section__in=[db_section, 'BOTH'],
        ).order_by('user__first_name')

    all_students = Student.all_objects.filter(
        school=school, class_name=grade.name, stream=stream_name, is_active=True,
    )
    total_stream_count = all_students.count()

    RELIGION_SUBJECTS = ['908', '909', '910', 'CRE', 'IRE', 'HRE']
    RELIGION_TAG = {'908': 'CRE', '909': 'IRE', '910': 'HRE', 'CRE': 'CRE', 'IRE': 'IRE', 'HRE': 'HRE'}

    subject_rows = []
    for a in assignments:
        subj_code = a.subject.code if a.subject else ''
        if subj_code in RELIGION_SUBJECTS:
            tag = RELIGION_TAG.get(subj_code, '')
            sc = all_students.filter(religion=tag).count() if tag else total_stream_count
        else:
            sc = total_stream_count

        subject_rows.append({
            'id': a.id,
            'subject_id': a.subject_id,
            'subject_name': a.subject.name if a.subject else '',
            'subject_code': subj_code,
            'teacher_id': a.teacher_profile_id,
            'teacher_name': a.teacher_profile.get_full_title() if a.teacher_profile else '',
            'student_count': sc,
        })

    return render(request, 'students/manage_subjects.html', {
        'grade': grade,
        'stream_name': stream_name,
        'subject_rows': subject_rows,
        'available_subjects': available_subjects,
        'teachers': teachers,
        'total_subjects': len(subject_rows),
    })


@login_required(login_url='login')
@school_admin_required
def class_list_page(request):
    """
    Full class list page with school header, student table, and print/download options.
    Accepts ?grade=Grade+8&stream=Main as GET parameters.
    """
    from django.db.models import IntegerField
    from django.db.models.functions import Cast, Substr, Length
    from ..models import Student

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()

    if not grade_name:
        messages.error(request, "No grade specified.")
        return redirect('manage_classes')

    students = Student.all_objects.filter(
        school=school, class_name=grade_name, is_active=True,
    )
    if stream_name:
        students = students.filter(stream=stream_name)

    students = (
        students
        .annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
        .order_by('adm_int')
    )

    student_list = []
    for idx, s in enumerate(students, 1):
        student_list.append({
            'id': s.id,
            'admission_no': s.admission_no or '',
            'name': s.name or '',
            'stream': s.stream or '',
            'gender': s.gender or '',
            'assessment_no': s.assessment_no or '',
            'guardian_name': s.guardian.name if s.guardian else '',
            'guardian_phone': s.guardian.phone if s.guardian else '',
            'religion': s.religion or '',
        })

    return render(request, 'students/class_list_page.html', {
        'school': school,
        'grade_name': grade_name,
        'stream_name': stream_name,
        'students': student_list,
        'total_count': len(student_list),
        'boys_count': sum(1 for s in student_list if s['gender'] == 'Male'),
        'girls_count': sum(1 for s in student_list if s['gender'] == 'Female'),
    })


# ── Subject categories for Add New Class form ────────────────────────────────
SUBJECT_CATEGORIES = {
    'Mathematics': ['903', 'MAT'],
    'Languages': ['901', 'ENG', '902', 'KIS', '904', 'KSL'],
    'Sciences': ['905', 'SCI'],
    'Humanities': ['907', 'SOC', '908', 'CRE', '909', 'IRE', '910', 'HRE'],
    'Technicals': ['912', 'PRE', 'AGR'],
    'Creative Arts': ['911', 'CAS'],
}


@login_required(login_url='login')
@school_admin_required
def add_new_class(request):
    """
    Class creation form: Grade + Streams + Subject selection.
    - If grade doesn't exist: creates grade, streams, and subject assignments.
    - If grade already exists: adds new streams to it and assigns subjects to new streams only.
    """
    from ..models import Grade, Stream, Subject, SubjectAssignment

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    if request.method == 'POST':
        grade_name = request.POST.get('grade_name', '').strip()
        streams_raw = request.POST.get('streams', '').strip()
        selected_codes = request.POST.getlist('subjects')

        if not grade_name:
            messages.error(request, "Please select a grade.")
            return redirect('add_new_class')

        if not streams_raw:
            messages.error(request, "Please enter at least one stream name.")
            return redirect('add_new_class')

        grade_num = int(grade_name.replace('Grade ', ''))
        if grade_num <= 3:
            db_section = 'PRIMARY'
            sub_section = 'LOWER'
        elif grade_num <= 6:
            db_section = 'PRIMARY'
            sub_section = 'UPPER'
        else:
            db_section = 'JSS'
            sub_section = None

        existing_grade = Grade.all_objects.filter(school=school, name=grade_name).first()

        if existing_grade:
            # Grade exists — add new streams only
            existing_stream_names = set(
                Stream.all_objects.filter(school=school, grade=existing_grade)
                .values_list('name', flat=True)
            )
            stream_names = [s.strip().title() for s in streams_raw.split(',') if s.strip()]
            new_stream_names = [s for s in stream_names if s not in existing_stream_names]
            skipped = [s for s in stream_names if s in existing_stream_names]

            if not new_stream_names:
                msg = "All entered stream(s) already exist in " + grade_name + "."
                if skipped:
                    msg += " Skipped: " + ", ".join(skipped)
                messages.error(request, msg)
                return redirect('add_new_class')

            created_streams = []
            for sname in new_stream_names:
                stream = Stream.all_objects.create(
                    school=school,
                    grade=existing_grade,
                    name=sname,
                    school_section=existing_grade.school_section,
                )
                created_streams.append(stream)

            subjects_created = 0
            for code in selected_codes:
                subject = Subject.all_objects.filter(
                    school=school, code=code, grade=grade_name, school_section=db_section
                ).first()
                if not subject:
                    continue
                for stream in created_streams:
                    SubjectAssignment.all_objects.get_or_create(
                        school=school,
                        subject=subject,
                        class_name=grade_name,
                        stream=stream.name,
                        defaults={
                            'school_section': db_section,
                            'sub_section': sub_section,
                        },
                    )
                    subjects_created += 1

            stream_label = ', '.join(new_stream_names)
            msg = f"{len(new_stream_names)} stream(s) ({stream_label}) added to {grade_name}"
            if skipped:
                msg += f". Skipped existing: {', '.join(skipped)}"
            if selected_codes:
                msg += f" with {len(selected_codes)} subject(s) assigned."
            else:
                msg += "."
            messages.success(request, msg)
            return redirect('manage_classes')

        else:
            # Grade doesn't exist — create grade + streams + subjects
            grade = Grade.all_objects.create(
                school=school,
                name=grade_name,
                school_section=db_section,
                sub_section=sub_section,
                order=GRADE_ORDER.get(grade_name, 99),
            )

            stream_names = [s.strip().title() for s in streams_raw.split(',') if s.strip()]
            created_streams = []
            for sname in stream_names:
                stream = Stream.all_objects.create(
                    school=school,
                    grade=grade,
                    name=sname,
                    school_section=db_section,
                )
                created_streams.append(stream)

            subjects_created = 0
            for code in selected_codes:
                subject = Subject.all_objects.filter(
                    school=school, code=code, grade=grade_name, school_section=db_section
                ).first()
                if not subject:
                    continue
                for stream in created_streams:
                    SubjectAssignment.all_objects.get_or_create(
                        school=school,
                        subject=subject,
                        class_name=grade_name,
                        stream=stream.name,
                        defaults={
                            'school_section': db_section,
                            'sub_section': sub_section,
                        },
                    )
                    subjects_created += 1

            stream_label = ', '.join(stream_names)
            messages.success(
                request,
                f"{grade_name} created with {len(created_streams)} stream(s) ({stream_label}) "
                f"and {len(selected_codes)} subject(s) assigned."
            )
            return redirect('manage_classes')

    # GET — load available subjects grouped by category
    all_subjects = Subject.all_objects.filter(school=school, is_active=True)
    categories = {}
    for cat_name, codes in SUBJECT_CATEGORIES.items():
        cat_subjects = all_subjects.filter(code__in=codes)
        if cat_subjects.exists():
            categories[cat_name] = cat_subjects.order_by('code')

    known_codes = set()
    for codes in SUBJECT_CATEGORIES.values():
        known_codes.update(codes)
    uncategorized = all_subjects.exclude(code__in=known_codes)
    if uncategorized.exists():
        categories['Other Subjects'] = uncategorized.order_by('code')

    return render(request, 'students/add_new_class.html', {
        'grades': ALL_GRADES,
    })


@login_required(login_url='login')
@school_admin_required
def api_grade_subjects(request):
    """
    AJAX endpoint: returns subjects grouped by category for a given grade.
    Usage: /school-admin/api/grade-subjects/?grade=Grade+7
    """
    import json
    from django.http import JsonResponse
    from ..models import Subject

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'No school context'}, status=400)

    grade_name = request.GET.get('grade', '').strip()
    if not grade_name:
        return JsonResponse({'categories': {}})

    grade_num = int(grade_name.replace('Grade ', ''))
    if grade_num <= 3:
        db_section = 'PRIMARY'
        sub_section = 'LOWER'
    elif grade_num <= 6:
        db_section = 'PRIMARY'
        sub_section = 'UPPER'
    else:
        db_section = 'JSS'
        sub_section = None

    subjects = Subject.all_objects.filter(
        school=school, school_section=db_section, is_active=True
    )
    if sub_section:
        subjects = subjects.filter(sub_section=sub_section)
    else:
        subjects = subjects.filter(sub_section__isnull=True)

    # Deduplicate by code (same code may exist for multiple grades)
    seen_codes = set()
    unique_subjects = []
    for s in subjects.order_by('code'):
        if s.code not in seen_codes:
            seen_codes.add(s.code)
            unique_subjects.append({'code': s.code, 'name': s.name})

    # Group by category
    categories = {}
    for cat_name, codes in SUBJECT_CATEGORIES.items():
        cat_subjects = [s for s in unique_subjects if s['code'] in codes]
        if cat_subjects:
            categories[cat_name] = cat_subjects

    # Uncategorized
    known_codes = set()
    for codes in SUBJECT_CATEGORIES.values():
        known_codes.update(codes)
    uncategorized = [s for s in unique_subjects if s['code'] not in known_codes]
    if uncategorized:
        categories['Other Subjects'] = uncategorized

    return JsonResponse({'categories': categories})


@login_required(login_url='login')
@school_admin_required
def api_check_grade_streams(request):
    """
    AJAX endpoint: checks if a grade already exists and returns its existing streams.
    Usage: /school-admin/api/check-grade-streams/?grade=Grade+1
    Returns: {exists: bool, grade_name: str, existing_streams: [str]}
    """
    from django.http import JsonResponse
    from ..models import Grade, Stream

    school = get_request_school(request)
    if not school:
        return JsonResponse({'exists': False, 'existing_streams': []})

    grade_name = request.GET.get('grade', '').strip()
    if not grade_name:
        return JsonResponse({'exists': False, 'existing_streams': []})

    grade = Grade.all_objects.filter(school=school, name=grade_name).first()
    if not grade:
        return JsonResponse({'exists': False, 'grade_name': grade_name, 'existing_streams': []})

    streams = list(
        Stream.all_objects.filter(school=school, grade=grade)
        .values_list('name', flat=True)
        .order_by('name')
    )
    return JsonResponse({
        'exists': True,
        'grade_name': grade_name,
        'existing_streams': streams,
    })


# ── Combine Streams ───────────────────────────────────────────────────────────
@login_required(login_url='login')
@school_admin_required
def combine_streams(request, grade_id):
    """
    POST: Combine selected streams into one new stream.
    Saves each student's current stream to previous_stream before merging.
    Archives old SubjectAssignments, creates new ones for the combined stream,
    and updates Teacher.assigned_task for class teachers.
    """
    from django.http import JsonResponse
    from django.db import models, transaction
    from ..models import Grade, Stream, Student, SubjectAssignment, Teacher

    if request.method != 'POST':
        return redirect('manage_streams', grade_id=grade_id)

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    try:
        grade = Grade.all_objects.get(id=grade_id, school=school)
    except Grade.DoesNotExist:
        messages.error(request, "Grade not found.")
        return redirect('manage_classes')

    stream_ids = request.POST.getlist('stream_ids')
    new_name = request.POST.get('new_stream_name', '').strip().title()

    if not new_name:
        messages.error(request, "Please provide a name for the merged stream.")
        return redirect('manage_streams', grade_id=grade.id)

    if len(stream_ids) < 2:
        messages.error(request, "Select at least 2 streams to combine.")
        return redirect('manage_streams', grade_id=grade.id)

    # Get the Stream records
    streams_to_combine = Stream.all_objects.filter(
        id__in=stream_ids, school=school, grade=grade
    )
    if streams_to_combine.count() != len(stream_ids):
        messages.error(request, "One or more selected streams not found.")
        return redirect('manage_streams', grade_id=grade.id)

    stream_names = list(streams_to_combine.values_list('name', flat=True))

    # Check if new name conflicts with an existing stream not being merged
    other_streams = Stream.all_objects.filter(
        school=school, grade=grade
    ).exclude(id__in=stream_ids)
    if other_streams.filter(name=new_name).exists():
        messages.error(
            request,
            f"Cannot rename to '{new_name}' — a stream with that name already exists "
            f"in {grade.name} and is not part of the merge.",
        )
        return redirect('manage_streams', grade_id=grade.id)

    with transaction.atomic():
        # 1. Save previous_stream for all affected students, then update stream
        students = Student.all_objects.filter(
            school=school, class_name=grade.name, stream__in=stream_names, is_active=True,
        )
        # Audit: capture old streams before update
        _old_streams = {s.id: s.stream for s in students}
        updated = students.update(previous_stream=models.F('stream'), stream=new_name)
        # Log the stream changes
        from students.models import SecurityAuditLog
        for student_id, old_stream in _old_streams.items():
            SecurityAuditLog.objects.create(
                actor=request.user,
                client_ip=request.META.get('REMOTE_ADDR'),
                action='update',
                target_model='students.student',
                target_id=str(student_id),
                target_fields=['stream'],
                old_values={'stream': old_stream},
                new_values={'stream': new_name},
                school_id_snapshot=school.pk,
            )

        # 2. Collect unique subjects BEFORE archiving (queryset re-evaluates after update)
        old_assignments = SubjectAssignment.all_objects.filter(
            school=school, class_name=grade.name, stream__in=stream_names, is_active=True,
        )
        subjects_to_create = list(
            old_assignments.values('subject_id', 'school_section', 'sub_section').distinct()
        )

        # 3. Archive old SubjectAssignments (set is_active=False)
        archived_count = old_assignments.update(is_active=False)

        # 4. Create new SubjectAssignments for the combined stream
        new_assignments = []
        for subj_data in subjects_to_create:
            new_assignments.append(SubjectAssignment(
                school=school,
                subject_id=subj_data['subject_id'],
                class_name=grade.name,
                stream=new_name,
                school_section=subj_data['school_section'],
                sub_section=subj_data['sub_section'],
                teacher_profile=None,
                is_active=True,
            ))
        if new_assignments:
            SubjectAssignment.all_objects.bulk_create(new_assignments, ignore_conflicts=True)

        # 4. Update class teachers: demote to "Teacher", then promote one per subject
        # Find class teachers of the old streams
        old_ct_pattern = models.Q()
        for sname in stream_names:
            old_ct_pattern |= models.Q(assigned_task__icontains=f"{grade.name} {sname}")
        class_teachers = Teacher.all_objects.filter(
            school=school, is_active=True,
            assigned_task__startswith='Class Teacher',
        ).filter(old_ct_pattern)

        # Demote all old class teachers to "Teacher"
        class_teachers.update(assigned_task='Teacher')

        # Update Teacher.classes denormalized field for affected teachers
        for teacher in class_teachers:
            if teacher.classes:
                old_classes = [c.strip() for c in teacher.classes.split(',') if c.strip()]
                new_classes = []
                for cls in old_classes:
                    # Check if this class references any of the old streams
                    matched = False
                    for sname in stream_names:
                        if grade.name in cls and sname in cls:
                            matched = True
                            break
                    if not matched:
                        new_classes.append(cls)
                # Add the new combined class
                new_classes.append(f"{grade.name} {new_name}")
                teacher.classes = ', '.join(new_classes)
                teacher.save(update_fields=['classes'])

        # 5. Delete old Stream model records
        streams_to_combine.delete()

        # 6. Create the new Stream record
        Stream.all_objects.create(
            school=school,
            grade=grade,
            name=new_name,
            school_section=grade.school_section,
        )

    messages.success(
        request,
        f"Combined {len(stream_names)} streams ({', '.join(stream_names)}) → '{new_name}'. "
        f"{updated} students updated, {archived_count} subject assignments archived. "
        f"New subject assignments created — go to Manage Subjects to assign teachers.",
    )
    return redirect('manage_streams', grade_id=grade.id)


# ── Split Streams ─────────────────────────────────────────────────────────────
@login_required(login_url='login')
@school_admin_required
def split_streams(request, grade_id):
    """
    POST: Split a single stream into multiple streams with smart balancing.
    The old stream record is preserved in the database for future combine operations.
    SubjectAssignments are auto-created for new streams (no teachers assigned).
    Students are distributed by gender + performance balance.
    """
    from django.http import JsonResponse
    from django.db import transaction
    from ..models import Grade, Stream, Student, SubjectAssignment, ExamSummary, Teacher, current_year

    if request.method != 'POST':
        return redirect('manage_streams', grade_id=grade_id)

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    try:
        grade = Grade.all_objects.get(id=grade_id, school=school)
    except Grade.DoesNotExist:
        messages.error(request, "Grade not found.")
        return redirect('manage_classes')

    target_stream = request.POST.get('target_stream', '').strip()
    split_names_raw = request.POST.getlist('split_names')
    split_names = [n.strip() for n in split_names_raw if n.strip()]

    if not target_stream:
        messages.error(request, "No stream selected to split.")
        return redirect('manage_streams', grade_id=grade.id)

    if not split_names or len(split_names) < 2:
        messages.error(request, "Please provide names for at least 2 new streams.")
        return redirect('manage_streams', grade_id=grade.id)

    # Verify the target stream exists
    stream_record = Stream.all_objects.filter(
        school=school, grade=grade, name=target_stream
    ).first()
    if not stream_record:
        messages.error(request, f"Stream '{target_stream}' not found.")
        return redirect('manage_streams', grade_id=grade.id)

    if len(set(split_names)) != len(split_names):
        messages.error(request, "Stream names must be unique.")
        return redirect('manage_streams', grade_id=grade.id)

    # Check name conflicts (allow the target_stream name itself — it stays)
    existing_names = set(
        Stream.all_objects.filter(school=school, grade=grade).values_list('name', flat=True)
    )
    for name in split_names:
        if name in existing_names and name != target_stream:
            messages.error(request, f"Stream '{name}' already exists in {grade.name}.")
            return redirect('manage_streams', grade_id=grade.id)

    # Fetch all students from the source stream
    students = Student.all_objects.filter(
        school=school, class_name=grade.name, stream=target_stream, is_active=True,
    )
    student_list = list(students.values('id', 'gender', 'name'))

    # Get latest performance data for smart balancing
    latest_term = 'Term 3'
    latest_year = current_year()
    summaries = ExamSummary.all_objects.filter(
        school=school,
        student__class_name=grade.name,
        term=latest_term,
        year=latest_year,
    ).values('student_id', 'total_marks')
    perf_map = {s['student_id']: s['total_marks'] for s in summaries}

    for s in student_list:
        s['total_marks'] = perf_map.get(s['id'], 0)
        s['gender'] = (s.get('gender') or 'Not Specified')

    # ── Smart Balance Algorithm ───────────────────────────────────────────
    # Split by gender first, then balance performance within each gender
    boys = [s for s in student_list if s['gender'] == 'Male']
    girls = [s for s in student_list if s['gender'] == 'Female']
    other = [s for s in student_list if s['gender'] not in ('Male', 'Female')]

    # Sort each group by performance descending
    boys.sort(key=lambda x: (-x['total_marks'], x['name']))
    girls.sort(key=lambda x: (-x['total_marks'], x['name']))
    other.sort(key=lambda x: (-x['total_marks'], x['name']))

    n = len(split_names)
    groups = {name: [] for name in split_names}
    group_boys = {name: 0 for name in split_names}
    group_girls = {name: 0 for name in split_names}
    group_perf = {name: 0 for name in split_names}

    # Distribute boys round-robin (snake: 1→2→3→4→4→3→2→1) for performance balance
    def snake_distribute(items, groups_dict, gender_key):
        order = list(range(n))
        reverse = False
        idx = 0
        while idx < len(items):
            for pos in order:
                if idx >= len(items):
                    break
                stream_idx = pos if not reverse else order[-(pos + 1)]
                sname = split_names[stream_idx]
                groups_dict[sname].append(items[idx]['id'])
                group_perf[sname] += items[idx]['total_marks']
                if gender_key == 'Male':
                    group_boys[sname] += 1
                else:
                    group_girls[sname] += 1
                idx += 1
            reverse = not reverse

    snake_distribute(boys, groups, 'Male')
    snake_distribute(girls, groups, 'Female')
    # Distribute 'other' gender students evenly
    for i, s in enumerate(other):
        sname = split_names[i % n]
        groups[sname].append(s['id'])
        group_perf[sname] += s['total_marks']

    # ── Apply Changes ─────────────────────────────────────────────────────
    with transaction.atomic():
        # 1. Collect subjects from the source stream BEFORE any changes
        source_assignments = SubjectAssignment.all_objects.filter(
            school=school, class_name=grade.name, stream=target_stream, is_active=True,
        )
        subjects_data = list(
            source_assignments.values('subject_id', 'school_section', 'sub_section').distinct()
        )

        # 2. Archive the source stream's SubjectAssignments (don't delete — preserve for future combine)
        source_assignments.update(is_active=False)

        # 3. Create new Stream records for each split name
        for name in split_names:
            Stream.all_objects.get_or_create(
                school=school, grade=grade, name=name,
                defaults={'school_section': grade.school_section},
            )

            # 4. Auto-create SubjectAssignments for this new stream (no teacher)
            new_sas = []
            for subj in subjects_data:
                new_sas.append(SubjectAssignment(
                    school=school,
                    subject_id=subj['subject_id'],
                    class_name=grade.name,
                    stream=name,
                    school_section=subj['school_section'],
                    sub_section=subj['sub_section'],
                    teacher_profile=None,
                    is_active=True,
                ))
            if new_sas:
                SubjectAssignment.all_objects.bulk_create(new_sas, ignore_conflicts=True)

        # 5. Move students to their new streams (preserve previous_stream for future restore)
        for name in split_names:
            sids = groups[name]
            if sids:
                # Audit: log stream changes for each student
                from students.models import SecurityAuditLog
                for sid in sids:
                    SecurityAuditLog.objects.create(
                        actor=request.user,
                        client_ip=request.META.get('REMOTE_ADDR'),
                        action='update',
                        target_model='students.student',
                        target_id=str(sid),
                        target_fields=['stream'],
                        old_values={'stream': target_stream},
                        new_values={'stream': name},
                        school_id_snapshot=school.pk,
                    )
                Student.all_objects.filter(id__in=sids).update(
                    stream=name, previous_stream=target_stream,
                )

        # 6. Demote class teacher of the source stream (if any)
        Teacher.all_objects.filter(
            school=school, is_active=True,
            assigned_task__startswith='Class Teacher',
            assigned_task__icontains=target_stream,
        ).update(assigned_task='Teacher')

    # Build summary
    parts = []
    for name in split_names:
        cnt = len(groups[name])
        avg = group_perf[name] / cnt if cnt else 0
        parts.append(f"{name}: {cnt} students ({group_boys[name]}B/{group_girls[name]}G, avg {avg:.0f} marks)")

    messages.success(
        request,
        f"Split '{target_stream}' → {len(split_names)} streams. "
        + "; ".join(parts)
        + ". SubjectAssignments created (no teachers). Go to Manage Subjects to assign teachers.",
    )
    return redirect('manage_streams', grade_id=grade.id)


# ── Split Preview API ─────────────────────────────────────────────────────────
@login_required(login_url='login')
@school_admin_required
def api_split_preview(request, grade_id):
    """
    GET: Preview smart-balance distribution for a stream split.
    Returns JSON with proposed groups, gender counts, and average performance.
    Uses snake-draft distribution for gender + performance balance.
    """
    from django.http import JsonResponse
    from ..models import Grade, Student, ExamSummary, current_year

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'No school context'}, status=400)

    try:
        grade = Grade.all_objects.get(id=grade_id, school=school)
    except Grade.DoesNotExist:
        return JsonResponse({'error': 'Grade not found'}, status=404)

    stream_name = request.GET.get('stream', '').strip()
    num_splits = int(request.GET.get('num_splits', 2))
    split_names_raw = request.GET.get('names', '').strip()
    split_names = [n.strip() for n in split_names_raw.split(',') if n.strip()] if split_names_raw else []

    if not stream_name or num_splits < 2:
        return JsonResponse({'error': 'Invalid parameters'}, status=400)

    # Use provided names or generate defaults
    if len(split_names) != num_splits:
        defaults = ['A', 'B', 'C', 'D']
        split_names = defaults[:num_splits]

    students = Student.all_objects.filter(
        school=school, class_name=grade.name, stream=stream_name, is_active=True,
    )

    # Get latest performance data
    latest_term = 'Term 3'
    latest_year = current_year()
    summaries = ExamSummary.all_objects.filter(
        school=school,
        student__class_name=grade.name,
        term=latest_term,
        year=latest_year,
    ).values('student_id', 'total_marks')
    perf_map = {s['student_id']: s['total_marks'] for s in summaries}

    student_data = []
    for s in students.values('id', 'name', 'gender'):
        student_data.append({
            'id': s['id'],
            'name': s['name'],
            'gender': (s.get('gender') or 'Not Specified'),
            'total_marks': perf_map.get(s['id'], 0),
        })

    # ── Smart Balance (same algorithm as the view) ────────────────────────
    boys = [s for s in student_data if s['gender'] == 'Male']
    girls = [s for s in student_data if s['gender'] == 'Female']
    other = [s for s in student_data if s['gender'] not in ('Male', 'Female')]

    boys.sort(key=lambda x: (-x['total_marks'], x['name']))
    girls.sort(key=lambda x: (-x['total_marks'], x['name']))
    other.sort(key=lambda x: (-x['total_marks'], x['name']))

    n = num_splits
    groups = {name: [] for name in split_names}
    group_boys = {name: 0 for name in split_names}
    group_girls = {name: 0 for name in split_names}
    group_perf = {name: 0 for name in split_names}

    def snake_distribute(items, groups_dict, gender_key):
        order = list(range(n))
        reverse = False
        idx = 0
        while idx < len(items):
            for pos in order:
                if idx >= len(items):
                    break
                stream_idx = pos if not reverse else order[-(pos + 1)]
                sname = split_names[stream_idx]
                groups_dict[sname].append(items[idx])
                group_perf[sname] += items[idx]['total_marks']
                if gender_key == 'Male':
                    group_boys[sname] += 1
                else:
                    group_girls[sname] += 1
                idx += 1
            reverse = not reverse

    snake_distribute(boys, groups, 'Male')
    snake_distribute(girls, groups, 'Female')
    for i, s in enumerate(other):
        sname = split_names[i % n]
        groups[sname].append(s)
        group_perf[sname] += s['total_marks']

    result = {
        'total_students': len(student_data),
        'total_boys': len(boys),
        'total_girls': len(girls),
        'groups': {},
    }
    for name in split_names:
        cnt = len(groups[name])
        avg = group_perf[name] / cnt if cnt else 0
        result['groups'][name] = {
            'count': cnt,
            'boys': group_boys[name],
            'girls': group_girls[name],
            'avg_marks': round(avg, 1),
            'students': [
                {'id': s['id'], 'name': s['name'], 'gender': s['gender'], 'marks': s['total_marks']}
                for s in groups[name]
            ],
        }

    return JsonResponse(result)
