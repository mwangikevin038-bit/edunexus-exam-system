import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from students.models import Student, ExamSummary, Exam
from django.db.models import Count

school = Student.all_objects.first().school

print("=== Summaries for Exam ID=11 (End of Term Assessment, PRIMARY, Term 2 2026) ===")
exam = Exam.all_objects.filter(school=school, id=11).first()
print(f"Exam: {exam.name} | {exam.term} {exam.year} | section={exam.school_section}")
summaries = ExamSummary.all_objects.filter(school=school, exam_name=exam.name, term=exam.term, year=exam.year)
print(f"Total: {summaries.count()}")
print("\nBy class_name:")
for r in summaries.values('student__class_name').annotate(c=Count('student_id', distinct=True)).order_by('student__class_name'):
    print(f"  {r['student__class_name']}: {r['c']}")
print("\nBy class_name + stream:")
for r in summaries.values('student__class_name', 'student__stream').annotate(c=Count('student_id', distinct=True)).order_by('student__class_name', 'student__stream'):
    print(f"  {r['student__class_name']} {r['student__stream']}: {r['c']}")
