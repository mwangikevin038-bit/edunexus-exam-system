import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
import django
django.setup()
from students.models import Student, School

school = School.objects.get(code='lungalunga')

# Revert: keep the DB name "Hanafi Hassani" (don't change it)
s = Student.all_objects.filter(
    school=school, class_name='Grade 7', stream='Yellow',
    assessment_no='B002157472',
).first()
print(f'Before: id={s.id} adm={s.admission_no} name={s.name!r} guardian_id={s.guardian_id}')
Student.all_objects.filter(pk=s.pk).update(name='Hanafi Hassani')
s.refresh_from_db()
print(f'After:  id={s.id} adm={s.admission_no} name={s.name!r} guardian_id={s.guardian_id}')

