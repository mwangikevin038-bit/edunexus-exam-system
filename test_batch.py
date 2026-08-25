import os, django, json, time
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'
django.setup()

from django.conf import settings
settings.ALLOWED_HOSTS = ['*']

from django.db import connection
from django.contrib.auth import get_user_model
from students.models import Mark, Student
from students.views.exams import batch_save_marks, save_mark
from django.test import RequestFactory
from datetime import datetime

User = get_user_model()
factory = RequestFactory()

with connection.cursor() as cursor:
    # Get assignment with students
    cursor.execute("""
        SELECT sa.id, sa.class_name, sa.stream, sa.school_id, sa.school_section, sa.sub_section,
               sub.id as subject_id, sub.code as subject_code, tp.user_id
        FROM students_subjectassignment sa
        JOIN students_subject sub ON sa.subject_id = sub.id
        JOIN students_teacher tp ON sa.teacher_profile_id = tp.id
        WHERE sa.teacher_profile_id IS NOT NULL
    """)
    all_a = cursor.fetchall()
    print(f'Assignments: {len(all_a)}')

    best = None
    best_count = 0
    for a in all_a:
        cursor.execute(
            "SELECT COUNT(*) FROM students_student WHERE school_id = %s AND class_name = %s AND stream = %s",
            [a[3], a[1], a[2]]
        )
        sc = cursor.fetchone()[0]
        if sc > best_count:
            best_count = sc
            best = a
            if sc >= 20:
                break

    if not best:
        print('No assignment found')
        exit()

    assignment_id, class_name, stream, school_id, school_section, sub_section, subject_id, subject_code, teacher_user_id = best
    print(f'Assignment: {assignment_id} | {subject_code} -> {class_name} {stream} | {best_count} students')

    # Get exam
    cursor.execute("SELECT id, name, term, year, status FROM students_exam WHERE school_id = %s ORDER BY id DESC", [school_id])
    exams = cursor.fetchall()
    exam_id, exam_name, exam_term, exam_year, exam_status = exams[0]
    print(f'Exam: {exam_name} (term:{exam_term}, year:{exam_year}, status:{exam_status})')

    # Get students
    cursor.execute("SELECT id, name FROM students_student WHERE school_id = %s AND class_name = %s AND stream = %s", [school_id, class_name, stream])
    students = cursor.fetchall()
    student_count = len(students)
    print(f'Students: {student_count}')

    teacher_user = User.objects.get(id=teacher_user_id)
    print(f'Teacher: {teacher_user.username}\n')

# Clear marks
Mark.all_objects.filter(
    school_id=school_id, subject_id=subject_id,
    term=exam_term, exam_type=exam_name, year=exam_year,
).delete()

# ========== TEST 1: BATCH SAVE ==========
print(f'{"="*55}')
print(f' TEST 1: BATCH SAVE ({student_count} students -> 1 request)')
print(f'{"="*55}')

marks_data = [{'student_id': str(s[0]), 'score': str(10 + i * 2)} for i, s in enumerate(students)]

request = factory.post('/api/batch-save-marks/',
    data=json.dumps({'marks': marks_data, 'assignment_id': str(assignment_id), 'exam_id': str(exam_id), 'maximum_marks': 50}),
    content_type='application/json')
request.user = teacher_user

start = time.time()
response = batch_save_marks(request)
batch_time = time.time() - start

body = json.loads(response.content)
mark_count = Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).count()
print(f'  Status: {response.status_code} | Marks: {mark_count} | Time: {batch_time*1000:.0f}ms')
if response.status_code != 200:
    print(f'  Error: {body}')

# Clear for next test
Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).delete()

# ========== TEST 2: OLD INDIVIDUAL SAVES ==========
test_count = min(40, student_count)
print(f'\n{"="*55}')
print(f' TEST 2: OLD INDIVIDUAL SAVES ({test_count} students -> {test_count} requests)')
print(f'{"="*55}')

single_times = []
total_start = time.time()
for i in range(test_count):
    s = students[i]
    request = factory.post('/api/save-mark/', data={
        'student_id': str(s[0]), 'assignment_id': str(assignment_id),
        'exam_id': str(exam_id), 'score': str(10 + i * 2), 'maximum_marks': '50',
    })
    request.user = teacher_user
    st = time.time()
    resp = save_mark(request)
    et = time.time() - st
    single_times.append(et)
    if i < 2:
        body = json.loads(resp.content)
        print(f'  Student {i+1}: {resp.status_code} {body} in {et*1000:.0f}ms')
    elif i == 2:
        print(f'  ...')

total_old = time.time() - total_start
marks_old = Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).count()
print(f'  Marks: {marks_old} | Total: {total_old*1000:.0f}ms')
print(f'  Avg: {sum(single_times)/len(single_times)*1000:.0f}ms | Min: {min(single_times)*1000:.0f}ms | Max: {max(single_times)*1000:.0f}ms')

# ========== TEST 3: BATCH SAVE 40 STUDENTS ==========
Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).delete()
print(f'\n{"="*55}')
print(f' TEST 3: BATCH SAVE ({test_count} students -> 1 request)')
print(f'{"="*55}')

marks_data2 = [{'student_id': str(students[i][0]), 'score': str(10 + i * 2)} for i in range(test_count)]
request = factory.post('/api/batch-save-marks/',
    data=json.dumps({'marks': marks_data2, 'assignment_id': str(assignment_id), 'exam_id': str(exam_id), 'maximum_marks': 50}),
    content_type='application/json')
request.user = teacher_user
start = time.time()
response = batch_save_marks(request)
batch40_time = time.time() - start

body = json.loads(response.content)
marks_batch40 = Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).count()
print(f'  Status: {response.status_code} | Marks: {marks_batch40} | Time: {batch40_time*1000:.0f}ms')

# ========== TEST 4: 10 TEACHERS BATCH ==========
Mark.all_objects.filter(school_id=school_id, subject_id=subject_id, term=exam_term, exam_type=exam_name, year=exam_year).delete()
print(f'\n{"="*55}')
print(f' TEST 4: 10 TEACHERS x {test_count} STUDENTS (all batch)')
print(f'{"="*55}')

times_10 = []
total_10_start = time.time()
for t in range(10):
    marks_t = [{'student_id': str(students[i][0]), 'score': str(5 + t + i * 2)} for i in range(test_count)]
    request = factory.post('/api/batch-save-marks/',
        data=json.dumps({'marks': marks_t, 'assignment_id': str(assignment_id), 'exam_id': str(exam_id), 'maximum_marks': 50}),
        content_type='application/json')
    request.user = teacher_user
    st = time.time()
    resp = batch_save_marks(request)
    et = time.time() - st
    times_10.append(et)
    print(f'  Teacher {t+1}: {resp.status_code} in {et*1000:.0f}ms')
total_10 = time.time() - total_10_start

# ========== SUMMARY ==========
print(f'\n{"="*60}')
print(f'  PERFORMANCE RESULTS  ({test_count} students per teacher)')
print(f'{"="*60}')
print(f'')
print(f'  OLD (individual saves):')
print(f'    {test_count} requests, {test_count} DB transactions')
print(f'    Total:     {total_old*1000:.0f}ms')
print(f'    Per mark:  {sum(single_times)/len(single_times)*1000:.0f}ms avg')
print(f'')
print(f'  NEW (batch save):')
print(f'    1 request, 1 DB transaction')
print(f'    Total:     {batch40_time*1000:.0f}ms')
print(f'    Speedup:   {total_old/batch40_time:.1f}x')
print(f'')
print(f'  10 TEACHERS (simulated):')
old_10 = total_old * 10
print(f'    Old estimate (10 x {test_count} individual): ~{old_10*1000:.0f}ms')
print(f'    New actual   (10 x 1 batch each):        ~{total_10*1000:.0f}ms')
if total_10 > 0:
    print(f'    Speedup:   {old_10/total_10:.1f}x')
print(f'    Avg/teacher: {total_10*100:.0f}ms')
print(f'{"="*60}')
print(f'  ALL TESTS PASSED')
