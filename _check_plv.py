import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
sys.path.insert(0, r'C:\Exam System')
django.setup()
from students.models import ExamSummary, Exam, Mark, Student, GradingConfig
from students.views.constants import ORDERED_LEVELS
from collections import Counter

school_id = 3
exam = Exam.all_objects.filter(school_id=school_id, id=11).first()

PRIMARY_LEVELS = ['EE', 'ME', 'AE', 'BE']
PLV_LABELS = {
    'EE1': 'Exceeding Expectations', 'EE2': 'Exceeding Expectations',
    'ME1': 'Meeting Expectations', 'ME2': 'Meeting Expectations',
    'AE1': 'Approaching Expectations', 'AE2': 'Approaching Expectations',
    'BE1': 'Below Expectations', 'BE2': 'Below Expectations',
    'EE': 'Exceeding Expectations', 'ME': 'Meeting Expectations',
    'AE': 'Approaching Expectations', 'BE': 'Below Expectations',
}

for grade in ['Grade 1', 'Grade 2', 'Grade 3', 'Grade 4', 'Grade 5', 'Grade 6', 'Grade 7', 'Grade 8', 'Grade 9']:
    summaries = ExamSummary.all_objects.filter(
        school_id=school_id, exam_name=exam.name, term=exam.term, year=exam.year,
        student__class_name=grade,
    )
    student_ids = summaries.values_list('student_id', flat=True).distinct()
    all_marks = Mark.all_objects.filter(
        student__school_id=school_id, student_id__in=student_ids,
        term=exam.term, year=exam.year, exam_type=exam.name,
    )

    # Detect section
    detected = summaries.values_list('school_section', flat=True).distinct()
    sec = list(detected)[0] if detected else 'PRIMARY'
    levels = PRIMARY_LEVELS if sec == 'PRIMARY' else ORDERED_LEVELS

    # Compute breakdown like the view does
    streams = list(summaries.values_list('student__stream', flat=True).distinct().order_by('student__stream'))
    total_pts = sum(m.points for m in all_marks)
    count = all_marks.count() if all_marks.count() > 0 else 1
    mean_pts = total_pts / count
    mean_marks = sum(m.score for m in all_marks) / count

    plv_counts = {}
    for s in summaries:
        p = s.overall_plv
        if p in levels:
            plv_counts[p] = plv_counts.get(p, 0) + 1

    # Fallback PLV
    best_lvl = '-'
    best_count = 0
    for lvl in levels:
        if plv_counts.get(lvl, 0) > best_count:
            best_count = plv_counts[lvl]
            best_lvl = lvl

    print(f'{grade} ({sec}): students={summaries.values("student_id").distinct().count()}, mean_pts={mean_pts:.4f}, mean_marks={mean_marks:.1f}')
    print(f'  PLV: {plv_counts}')
    print(f'  Performance Level (fallback): {best_lvl} ({PLV_LABELS.get(best_lvl, best_lvl)})')
