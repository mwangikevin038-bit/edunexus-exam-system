import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from students.models import Mark, Student, Subject, Exam, MarkSubmission
from students.school_scope import set_current_school, set_current_school_section
from collections import defaultdict, Counter

school = Student.all_objects.values_list('school', flat=True).first()
set_current_school(school)
set_current_school_section('BOTH')

exam = Exam.all_objects.filter(name__icontains='End of Term', term='Term 2', year=2026).first()
print(f'Exam: {exam.name} | {exam.term} {exam.year}')

# ====== GRADE 7 FULL ANALYSIS ======
print('\n' + '='*80)
print('GRADE 7 FULL ANALYSIS')
print('='*80)

# All Grade 7 students
all_g7 = Student.all_objects.filter(class_name='Grade 7', is_active=True).order_by('stream', 'admission_no')
print(f'\nTotal active Grade 7 students: {all_g7.count()}')

# Students per stream
stream_counts = Counter(s.stream for s in all_g7)
print(f'Students per stream: {dict(stream_counts)}')

# All Grade 7 marks for this exam
all_g7_marks = Mark.all_objects.filter(
    student__class_name='Grade 7',
    term=exam.term, exam_type=exam.name, year=exam.year,
).select_related('student', 'subject')

# Group marks by student
marks_by_student = defaultdict(list)
for m in all_g7_marks:
    marks_by_student[m.student_id].append(m)

# Group marks by stream
marks_by_stream = defaultdict(lambda: defaultdict(list))
for m in all_g7_marks:
    marks_by_stream[m.student.stream][m.subject.code].append(m)

# Show subject coverage per stream
print(f'\n--- Subject coverage per stream ---')
for stream_name in sorted(marks_by_stream.keys()):
    subjects = marks_by_stream[stream_name]
    print(f'\n  Stream: {stream_name}')
    for subj_code in sorted(subjects.keys()):
        marks_list = subjects[subj_code]
        print(f'    {subj_code}: {len(marks_list)} marks')

# ====== FIND ANOMALIES ======
print('\n' + '='*80)
print('ANOMALY DETECTION')
print('='*80)

# 1. Students with marks in MULTIPLE streams
print('\n--- Students with marks in multiple streams ---')
student_stream_marks = defaultdict(lambda: defaultdict(set))
for m in all_g7_marks:
    student_stream_marks[m.student.admission_no][m.student.stream].add(m.subject.code)

multi_stream_students = {adno: streams for adno, streams in student_stream_marks.items() if len(streams) > 1}
if multi_stream_students:
    for adno, streams in sorted(multi_stream_students.items()):
        student = Student.all_objects.filter(admission_no=adno, class_name='Grade 7').first()
        print(f'  {adno} | {student.name} | enrolled_stream={student.stream} | marks_in={list(streams.keys())}')
    else:
        pass
else:
    print('  None found')

# 2. Students enrolled in a stream but marks in a DIFFERENT stream
print('\n--- Students with stream mismatch (enrolled vs marks) ---')
mismatch_students = []
for adno, streams in student_stream_marks.items():
    student = Student.all_objects.filter(admission_no=adno, class_name='Grade 7').first()
    if student and student.stream not in streams:
        mismatch_students.append((adno, student.name, student.stream, list(streams.keys())))
    # Also check if student appears in multiple DB records
    all_records = Student.all_objects.filter(admission_no=adno, class_name='Grade 7', is_active=True)
    if all_records.count() > 1:
        for rec in all_records:
            print(f'  DUPLICATE STUDENT: {adno} | {rec.name} | stream={rec.stream} | id={rec.id}')

if mismatch_students:
    for adno, name, enrolled, mark_streams in mismatch_students:
        print(f'  {adno} | {name} | enrolled={enrolled} | marks_in={mark_streams}')
else:
    print('  None found')

# 3. Students with marks but wrong stream subject assignment
print('\n--- Students with marks for subjects not in their stream ---')
# Check MarkSubmission to see which subjects are assigned to which stream
subs = MarkSubmission.objects.filter(
    class_name='Grade 7',
    exam_name=exam.name, term=exam.term, year=exam.year,
).select_related('subject').order_by('stream', 'subject__code')

stream_subjects = defaultdict(set)
for s in subs:
    stream_subjects[s.stream].add(s.subject.code)

print(f'  Subjects per stream (from MarkSubmission):')
for stream_name in sorted(stream_subjects.keys()):
    print(f'    {stream_name}: {sorted(stream_subjects[stream_name])}')

# 4. Students with duplicate marks
print('\n--- Students with duplicate marks (same subject, same stream) ---')
mark_keys = defaultdict(list)
for m in all_g7_marks:
    key = (m.student_id, m.subject_id, m.student.stream)
    mark_keys[key].append(m)

dupes = {k: v for k, v in mark_keys.items() if len(v) > 1}
if dupes:
    for (student_id, subject_id, stream), marks_list in sorted(dupes.items()):
        student = Student.all_objects.filter(id=student_id).first()
        subject = Subject.objects.filter(id=subject_id).first()
        print(f'  {student.admission_no} | {student.name} | {stream} | {subject.code} | {len(marks_list)} marks')
        for m in marks_list:
            score_str = 'AB' if m.is_absent else str(m.score)
            print(f'    id={m.id} score={score_str} raw={m.raw_score} max={m.maximum_marks} plv={m.performance_level}')
else:
    print('  None found')

# 5. Students in a stream but with NO marks at all
print('\n--- Students with NO marks this exam ---')
for stream_name in sorted(stream_counts.keys()):
    stream_students = all_g7.filter(stream=stream_name)
    no_mark_students = []
    for s in stream_students:
        if s.id not in marks_by_student:
            no_mark_students.append(s)
    if no_mark_students:
        print(f'\n  Stream {stream_name} ({len(no_mark_students)} students with no marks):')
        for s in no_mark_students:
            print(f'    {s.admission_no} | {s.name}')

# 6. Students with marks but NOT enrolled in any Grade 7 stream
print('\n--- Students with marks but not in any active Grade 7 stream ---')
marked_student_ids = set(m.student_id for m in all_g7_marks)
all_g7_ids = set(all_g7.values_list('id', flat=True))
phantom_ids = marked_student_ids - all_g7_ids
if phantom_ids:
    for sid in phantom_ids:
        marks = [m for m in all_g7_marks if m.student_id == sid]
        student = Student.all_objects.filter(id=sid).first()
        if student:
            print(f'  {student.admission_no} | {student.name} | class={student.class_name} | stream={student.stream} | active={student.is_active}')
            for m in marks[:3]:
                print(f'    Mark: {m.subject.code} | stream_context={m.student.stream}')
else:
    print('  None found')

# 7. Show detailed mark distribution for each stream
print('\n' + '='*80)
print('DETAILED MARK DISTRIBUTION PER STREAM')
print('='*80)

for stream_name in sorted(marks_by_stream.keys()):
    subjects = marks_by_stream[stream_name]
    print(f'\n--- Stream: {stream_name} ---')
    # Get all students in this stream
    stream_students = all_g7.filter(stream=stream_name).order_by('admission_no')
    
    for student in stream_students:
        student_marks = [m for m in all_g7_marks if m.student_id == student.id]
        if student_marks:
            subj_scores = []
            for m in student_marks:
                score_str = 'AB' if m.is_absent else str(m.score)
                subj_scores.append(f'{m.subject.code}={score_str}')
            print(f'  {student.admission_no} | {student.name} | {", ".join(subj_scores)}')
        else:
            print(f'  {student.admission_no} | {student.name} | NO MARKS')
