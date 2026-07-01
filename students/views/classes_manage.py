"""
Class and stream management views for school administrators.

Provides CRUD operations for grades (Grade 1-12) and their streams,
with automatic single-stream naming and enrollment checks.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from ..security import get_request_school, get_request_school_section, school_admin_required


# Section-to-grade mapping
LOWER_PRIMARY_GRADES = [f'Grade {i}' for i in range(1, 4)]
PRIMARY_GRADES = [f'Grade {i}' for i in range(4, 7)]
JSS_GRADES = [f'Grade {i}' for i in range(7, 10)]
ALL_GRADES = [f'Grade {i}' for i in range(1, 13)]
GRADE_ORDER = {f'Grade {i}': i for i in range(1, 13)}

# Grade number → sub_section mapping
GRADE_SUB_SECTION = {}
for _i in range(1, 4):
    GRADE_SUB_SECTION[f'Grade {_i}'] = 'LOWER'
for _i in range(4, 7):
    GRADE_SUB_SECTION[f'Grade {_i}'] = 'UPPER'


def _resolve_db_section(section):
    """Map workspace section to the DB school_section value used by Grade/Stream."""
    if section in ('LOWER_PRIMARY', 'PRIMARY'):
        return 'PRIMARY'
    return 'JSS'


@login_required(login_url='login')
@school_admin_required
def manage_classes(request):
    """
    School admin view to manage grades and streams.
    Admin can add grades and name streams per grade.
    Single-stream grades auto-name to 'Main'.
    Grades are filtered by workspace section (Primary or JSS).
    """
    from ..models import Grade, Stream

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    section = get_request_school_section(request)

    # Determine which grades are allowed in this workspace
    if section == 'LOWER_PRIMARY':
        allowed_grades = LOWER_PRIMARY_GRADES
    elif section == 'PRIMARY':
        allowed_grades = PRIMARY_GRADES
    elif section == 'JSS':
        allowed_grades = JSS_GRADES
    else:
        allowed_grades = ALL_GRADES

    if request.method == 'POST':
        action = request.POST.get('action')

        # ── Add a new grade ───────────────────────────────────────────────────
        if action == 'add_grade':
            grade_name = request.POST.get('grade_name', '').strip()
            if not grade_name:
                messages.error(request, "Please select a grade.")
                return redirect('manage_classes')

            if grade_name not in allowed_grades:
                messages.error(request, f"{grade_name} is not available in the {section or 'current'} workspace.")
                return redirect('manage_classes')

            if Grade.all_objects.filter(school=school, name=grade_name).exists():
                messages.error(request, f"{grade_name} already exists for this school.")
                return redirect('manage_classes')

            grade = Grade.all_objects.create(
                school=school,
                name=grade_name,
                school_section=_resolve_db_section(section),
                sub_section=GRADE_SUB_SECTION.get(grade_name),
                order=GRADE_ORDER.get(grade_name, 99),
            )
            Stream.all_objects.create(
                school=school,
                grade=grade,
                name='Main',
                school_section=_resolve_db_section(section),
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
    # Use all_objects but filter by section to show correct grades
    grades = (
        Grade.all_objects
        .filter(school=school, school_section=_resolve_db_section(section))
        .prefetch_related('streams')
        .order_by('order')
    )

    existing_grade_names = set(grades.values_list('name', flat=True))
    available_grades = [g for g in allowed_grades if g not in existing_grade_names]

    return render(request, 'students/manage_classes.html', {
        'grades': grades,
        'available_grades': available_grades,
    })
