import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from students.models import Student, SubjectAssignment, Teacher
from django.contrib.auth.models import User
from students.school_scope import set_current_school_section, reset_current_school_section, set_current_school, reset_current_school

# Check students
students = Student.all_objects.filter(school__code='lungalunga', class_name='Grade 3')
print(f"Grade 3 students: {students.count()}")
for s in students[:3]:
    print(f"  {s.name} sec={s.school_section} sub={s.sub_section} stream={s.stream}")

# Check what SchoolScopedManager returns for LOWER_PRIMARY
from students.models import School
school = School.objects.get(code='lungalunga')
school_token = set_current_school(school)
token = set_current_school_section('LOWER_PRIMARY')
scoped = Student.objects.filter(school__code='lungalunga', class_name='Grade 3')
print(f"\nStudents via SchoolScopedManager (LOWER_PRIMARY): {scoped.count()}")
for s in scoped[:3]:
    print(f"  {s.name} sec={s.school_section} sub={s.sub_section} stream={s.stream}")

# Check streams
from students.models import Stream, Grade
grades = Grade.all_objects.filter(school__code='lungalunga', school_section='PRIMARY', sub_section='LOWER')
print(f"\nLower Primary grades: {list(grades.values_list('name', flat=True))}")
streams = Stream.all_objects.filter(school__code='lungalunga').filter(grade__name='Grade 3')
print(f"Grade 3 streams: {list(streams.values_list('name', 'school_section', 'grade__name'))}")

# Check teacher assignments and what contexts the teacher would see
u = User.objects.get(username='0103589521@lungalunga')
t = Teacher.all_objects.get(user=u)
assgns = SubjectAssignment.all_objects.filter(teacher_profile=t)
print(f"\nTeacher assignments: {assgns.count()}")
for a in assgns:
    print(f"  {a.subject.code} {a.class_name} {a.stream} sec={a.school_section} sub={a.sub_section}")

scoped_assgns = SubjectAssignment.objects.filter(teacher_profile=t)
print(f"\nScoped assignments (LOWER_PRIMARY): {scoped_assgns.count()}")
for a in scoped_assgns:
    print(f"  {a.subject.code} {a.class_name} {a.stream}")

# Check allowed class+stream pairs
allowed = scoped_assgns.values_list('class_name', 'stream').distinct()
print(f"\nAllowed class+stream pairs: {list(allowed)}")

# Check students matching
filters = None
for cls, stm in allowed:
    if filters is None:
        from django.db.models import Q
        filters = Q(class_name=cls, stream=stm)
    else:
        filters |= Q(class_name=cls, stream=stm)

if filters:
    result = Student.objects.filter(school__code='lungalunga').filter(filters)
    print(f"Students matching allowed pairs: {result.count()}")
    for s in result[:5]:
        print(f"  {s.name} {s.class_name} {s.stream}")

reset_current_school_section(token)
reset_current_school(school_token)

# Count students per grade
from django.db.models import Count
counts = Student.all_objects.filter(school__code='lungalunga').values('class_name').annotate(c=Count('id')).order_by('class_name')
print("\nAll students by grade (lungalunga):")
for row in counts:
    print(f"  {row['class_name']}: {row['c']}")

