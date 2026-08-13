import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from students.models import Exam, AssessmentLock
from django.db.models import Count

print('=== ALL UNIQUE EXAM NAMES ===')
for row in Exam.all_objects.values('name').annotate(c=Count('id')).order_by('name'):
    print(f'  "{row["name"]}": {row["c"]} exams')

print()
print('=== ALL UNIQUE ASSESSMENT LOCK EXAM TYPES ===')
for row in AssessmentLock.all_objects.values('exam_type').annotate(c=Count('id')).order_by('exam_type'):
    print(f'  "{row["exam_type"]}": {row["c"]} locks')

print()
print('=== EXAM MODEL CHOICES ===')
from students.models import Exam
for val, label in Exam.EXAM_CHOICES:
    print(f'  "{val}"')
