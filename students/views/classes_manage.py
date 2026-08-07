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
ALL_GRADES = [f'Grade {i}' for i in range(1, 13)]
GRADE_ORDER = {f'Grade {i}': i for i in range(1, 13)}


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
    """JSON endpoint: returns student list for a given grade+stream."""
    from django.http import JsonResponse
    from ..models import Student, Stream

    school = get_request_school(request)
    if not school:
        return JsonResponse({'students': [], 'has_multiple_streams': False})

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()

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

    student_count = Student.all_objects.filter(
        school=school, class_name=grade.name, stream=stream_name, is_active=True,
    ).count()

    subject_rows = []
    for a in assignments:
        subject_rows.append({
            'id': a.id,
            'subject_id': a.subject_id,
            'subject_name': a.subject.name if a.subject else '',
            'subject_code': a.subject.code if a.subject else '',
            'teacher_id': a.teacher_profile_id,
            'teacher_name': a.teacher_profile.get_full_title() if a.teacher_profile else '',
        })

    return render(request, 'students/manage_subjects.html', {
        'grade': grade,
        'stream_name': stream_name,
        'subject_rows': subject_rows,
        'available_subjects': available_subjects,
        'teachers': teachers,
        'student_count': student_count,
        'total_subjects': len(subject_rows),
    })
