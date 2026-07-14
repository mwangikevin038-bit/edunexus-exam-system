import csv
from students.models import Student

rows = []
for grade in ['Grade 8', 'Grade 9']:
    for s in Student.all_objects.filter(class_name=grade, stream='Main').order_by('admission_no', 'name'):
        rows.append({
            'current_adm': s.admission_no or '',
            'name': s.name,
            'assessment_no': s.assessment_no or '',
            'class': grade,
            'stream': 'Main',
            'new_adm': '',  # <-- fill this column in
        })

with open('g8g9_admission_fix.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=['current_adm', 'name', 'assessment_no', 'class', 'stream', 'new_adm'])
    w.writeheader()
    w.writerows(rows)

print(f'Wrote {len(rows)} rows to g8g9_admission_fix.csv')
