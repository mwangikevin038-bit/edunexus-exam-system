from students.models import Student

for grade in ['Grade 8', 'Grade 9']:
    print(f'=== {grade} Main - full ordered by adm ===')
    rows = Student.all_objects.filter(class_name=grade, stream='Main').order_by('admission_no')
    for s in rows:
        print(f'  adm={str(s.admission_no or ""):>5}  {s.name:<35}  assess={s.assessment_no or ""}')
    print()
