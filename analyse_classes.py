import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
import django
django.setup()
from students.models import Student, School
school = School.objects.get(code='lungalunga')

for grade, stream in [('Grade 7', 'Yellow'), ('Grade 8', 'Main'), ('Grade 9', 'Main')]:
    qs = Student.all_objects.filter(
        school=school, class_name=grade, stream=stream,
    ).order_by('admission_no')
    print(f'\n=== {grade} {stream} ({qs.count()} students) ===')
    print(f'  adm range: {qs.first().admission_no} ... {qs.last().admission_no}')
    # Show first 10 + last 10
    items = list(qs.values('admission_no', 'name', 'assessment_no'))
    print('  First 5:')
    for s in items[:5]:
        print(f'    adm={s["admission_no"]:>4}  {s["name"]:<30}  assess={s["assessment_no"]}')
    print('  Last 5:')
    for s in items[-5:]:
        print(f'    adm={s["admission_no"]:>4}  {s["name"]:<30}  assess={s["assessment_no"]}')
