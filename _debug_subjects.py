import os, sys, json, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'
sys.path.insert(0, r'C:\Exam System')
django.setup()
from students.models import *
from students.views.constants import ORDERED_LEVELS
from collections import Counter

school = School.objects.first()
summaries = ExamSummary.all_objects.filter(school=school, exam_name='End of Term Assessment', term='Term 3', year=2025, student__class_name='Grade 7')
student_ids = list(summaries.values_list('student_id', flat=True).distinct())
all_marks = Mark.all_objects.filter(school=school, exam_type='End of Term Assessment', term='Term 3', year=2025, student_id__in=student_ids).select_related('subject','student')

subject_perf = {}
for mark in all_marks:
    if mark.subject_id and mark.subject:
        subj_name = mark.subject.name
        if subj_name not in subject_perf:
            subject_perf[subj_name] = {'total_points': 0, 'count': 0}
        subject_perf[subj_name]['total_points'] += mark.points
        subject_perf[subj_name]['count'] += 1

print('=== SUBJECT PERF ===')
for k, v in sorted(subject_perf.items()):
    print(f'  {k}: count={v["count"]}')

grading_config = GradingConfig.objects.filter(school=school, section='JSS').first()
print(f'grading_config: {grading_config}')

ORDERED_JSS = ['EE1','EE2','ME1','ME2','AE1','AE2','BE1','BE2']
streams = list(summaries.values_list('student__stream', flat=True).distinct())
print(f'streams: {streams}')

subject_breakdowns = {}
for subj_name, data in sorted(subject_perf.items()):
    if data['count'] == 0:
        continue
    subj_rows = []
    for s in streams:
        s_ids_list = summaries.filter(student__stream=s).values_list('student_id', flat=True).distinct()
        subj_marks = all_marks.filter(student_id__in=s_ids_list, subject__name=subj_name)
        row = {'form': f'Grade 7 {s}', 'X': 0, 'Y': 0, 'entries': len(s_ids_list)}
        for lvl in ORDERED_JSS:
            row[lvl] = 0
        total_pts = 0
        count = 0
        for m in subj_marks:
            total_pts += m.points
            count += 1
            plv_key = '-'
            if grading_config and grading_config.subject_scale:
                for level_def in grading_config.subject_scale:
                    converted = (m.score / m.maximum_marks * 100) if m.maximum_marks else 0
                    if level_def.get('min_marks', 0) <= converted <= level_def.get('max_marks', 0):
                        plv_key = level_def.get('level', '-')
                        break
            if plv_key in row:
                row[plv_key] += 1
        mean_pts = total_pts / count if count else 0
        row['mean_marks'] = round(sum(m.score for m in subj_marks) / count, 1) if count else 0
        row['mean_points'] = round(mean_pts, 4)
        row['entries'] = count
        subj_rows.append(row)
    subj_total = {
        'form': 'Grade 7', 'X': 0, 'Y': 0,
        'entries': sum(r['entries'] for r in subj_rows),
        'mean_marks': round(sum(r['mean_marks'] for r in subj_rows) / len(subj_rows), 1) if subj_rows else 0,
        'mean_points': round(sum(r['mean_points'] for r in subj_rows) / len(subj_rows), 4) if subj_rows else 0,
    }
    for lvl in ORDERED_JSS:
        subj_total[lvl] = sum(r.get(lvl, 0) for r in subj_rows)
    subject_breakdowns[subj_name] = {'rows': subj_rows, 'total': subj_total}

print(f'\n=== SUBJECT BREAKDOWNS ({len(subject_breakdowns)} subjects) ===')
for k in subject_breakdowns:
    print(f'  key: "{k}"')
print(f'\nJSON keys: {json.dumps(list(subject_breakdowns.keys()))}')
