import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from students.models import Mark, Exam, Student
from django.db.models import Avg

school = Student.all_objects.first().school
curr = Exam.all_objects.filter(school=school, id=11).first()
prev = Exam.all_objects.filter(school=school, id=9).first()

cm = Mark.all_objects.filter(student__school=school, term=curr.term, year=curr.year, exam_type=curr.name, student__class_name='Grade 6')
pm = Mark.all_objects.filter(student__school=school, term=prev.term, year=prev.year, exam_type=prev.name, student__class_name='Grade 6')

print(f"Current: {curr.name} {curr.term} {curr.year}")
print(f"Previous: {prev.name} {prev.term} {prev.year}")
print()

curr_subj = cm.values('subject__name').annotate(a=Avg('points')).order_by('-a')
for r in curr_subj:
    name = r['subject__name']
    curr_pts = r['a']
    prev_mark = pm.filter(subject__name=name)
    if prev_mark.exists():
        prev_pts = prev_mark.aggregate(a=Avg('points'))['a']
        change = round(curr_pts - prev_pts, 4)
    else:
        prev_pts = 'N/A'
        change = 'N/A'
    print(f"  {name}: curr={curr_pts:.4f} prev={prev_pts} change={change}")
