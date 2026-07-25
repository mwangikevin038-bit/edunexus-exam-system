import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()
from students.models import Student, Teacher
from django.db.models import Q

student = Student.all_objects.get(id=99)
print(f'Student: {student.name}, class="{student.class_name}", stream="{student.stream}", school="{student.school.name}"')

ct = Teacher.all_objects.filter(
    school=student.school,
    assigned_task__icontains=student.class_name,
).filter(
    Q(assigned_task__icontains=student.stream),
).select_related('user').first()

print(f'Teacher: {ct.get_full_title() if ct else "NONE"}')
if ct:
    print(f'Task: "{ct.assigned_task}"')
