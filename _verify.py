from students.models import Student

for grade, stream in [('Grade 8', 'Main'), ('Grade 9', 'Main')]:
    print(f'=== {grade} {stream} ===')
    nums = sorted({int(n) for n in Student.all_objects.filter(class_name=grade, stream=stream).values_list('admission_no', flat=True) if n and str(n).isdigit()})
    print(f'  {len(nums)} students, range {nums[0]}-{nums[-1]}')

print()
print('=== Mohamed Hassan (Grade 8) — for manual fix ===')
for s in Student.all_objects.filter(class_name='Grade 8', stream='Main', name__iexact='mohamed hassan').order_by('admission_no'):
    print(f'  id={s.id}  adm={s.admission_no}  name={s.name!r}  assess={s.assessment_no}')
print('You need to manually set these to 226 and 311.')
