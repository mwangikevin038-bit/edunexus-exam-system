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
    """
    from django.http import JsonResponse
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
        # else: no students tagged yet → show all (first-time)

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

    return JsonResponse({'students': student_list, 'has_multiple_streams': has_multiple})


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
        school=school, class_name=grade.name, stream=stream_name,
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
