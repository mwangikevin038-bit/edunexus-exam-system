"""
Subject Merge Computation Engine
================================
Handles dynamic aggregation of individual subject marks into merged exam papers
when exam_report_mode='INTEGRATED_KNEC'.

Business Rules:
- Merged score = simple average (AVG) of component subject scores
- If ANY component is NULL (data not yet entered) -> result = 'INC' (incomplete)
- If a component is explicitly absent (is_absent=TRUE) -> treat as 0, continue
- RE variant (CRE/IRE/HRE) resolved per student's religion field
- Grading threshold applied to final combined score
"""

from django.db.models import Q, F, CharField, Value, Case, When, IntegerField
from django.db.models.functions import Coalesce
from django.db import connection


def get_merge_groups(sub_section):
    """
    Return all merge groups for a sub_section as a dict:
    {
        'INT_SCI': {
            'name': 'Integrated Science',
            'components': [
                {'code': 'SCI', 'name': 'Science and Technology', 'order': 1},
                {'code': 'AGR', 'name': 'Agriculture and Nutrition', 'order': 2},
            ]
        },
        ...
    }
    """
    from students.models import SubjectMergeGroup

    groups = {}
    rows = SubjectMergeGroup.objects.filter(
        sub_section=sub_section
    ).order_by('merge_group_code', 'display_order')

    for row in rows:
        if row.merge_group_code not in groups:
            groups[row.merge_group_code] = {
                'name': row.merge_group_name,
                'components': [],
            }
        groups[row.merge_group_code]['components'].append({
            'code': row.component_code,
            'name': row.component_name,
            'order': row.display_order,
        })

    return groups


def compute_merged_score_raw_sql(school_id, sub_section, exam_name, term, year, class_name, stream):
    """
    Execute a single optimized PostgreSQL query that returns the merged results
    sheet matrix for a class/stream.

    Returns a list of dicts, one per student:
    [
        {
            'student_id': 123,
            'admission_no': '001',
            'name': 'John Doe',
            'merged_papers': {
                'INT_SCI': {
                    'name': 'Integrated Science',
                    'score': 72,          # average or 'INC'
                    'components': {
                        'SCI': {'score': 80, 'is_absent': False},
                        'AGR': {'score': 64, 'is_absent': False},
                    }
                },
                'ENG_LIT': { ... },
                ...
            }
        },
        ...
    ]
    """
    merge_groups = get_merge_groups(sub_section)
    if not merge_groups:
        return []

    # Get all students in the class/stream
    from students.models import Student
    students = Student.objects.filter(
        school_id=school_id,
        class_name=class_name,
        stream=stream,
    ).order_by('admission_no')

    # Get all relevant subjects for this sub_section
    from students.models import Subject
    subjects = Subject.objects.filter(
        school_id=school_id,
        school_section='PRIMARY',
        sub_section=sub_section,
    )
    subject_map = {s.code: s for s in subjects}

    # Get all marks for this class/stream/exam
    from students.models import Mark
    marks = Mark.objects.filter(
        school_id=school_id,
        school_section='PRIMARY',
        sub_section=sub_section,
        term=term,
        exam_type=exam_name,
        year=year,
        student__class_name=class_name,
        student__stream=stream,
    ).select_related('subject', 'student')

    # Build mark lookup: {student_id: {subject_code: mark_record}}
    marks_by_student = {}
    for mark in marks:
        sid = mark.student_id
        code = mark.subject.code if mark.subject else None
        if sid not in marks_by_student:
            marks_by_student[sid] = {}
        if code:
            marks_by_student[sid][code] = mark

    results = []
    for student in students:
        student_marks = marks_by_student.get(student.id, {})
        merged_papers = {}

        for group_code, group_info in merge_groups.items():
            components = group_info['components']
            component_scores = {}
            all_present = True
            any_absent = False

            for comp in components:
                code = comp['code']
                mark = student_marks.get(code)

                if mark is None:
                    # NULL mark = data entry in progress
                    all_present = False
                    component_scores[code] = {'score': None, 'is_absent': False}
                elif mark.is_absent:
                    # Explicit absence = treat as 0
                    component_scores[code] = {'score': 0, 'is_absent': True}
                    any_absent = True
                else:
                    # Use raw_score if available, else score
                    score_val = mark.raw_score if mark.raw_score is not None else mark.score
                    component_scores[code] = {'score': score_val, 'is_absent': False}

            if not all_present:
                # Missing data -> INC
                merged_score = 'INC'
            else:
                # Calculate simple average
                scores = [c['score'] for c in component_scores.values()]
                merged_score = round(sum(scores) / len(scores)) if scores else 0

            merged_papers[group_code] = {
                'name': group_info['name'],
                'score': merged_score,
                'components': component_scores,
            }

        results.append({
            'student_id': student.id,
            'admission_no': student.admission_no,
            'name': student.name,
            'merged_papers': merged_papers,
        })

    return results


def get_merged_results_sheet_data(school_id, sub_section, exam_name, term, year, class_name, stream):
    """
    High-level function that returns the full results sheet matrix data.
    Used by views to render both Report Cards and Results Sheet.

    Returns:
    {
        'students': [...],  # from compute_merged_score_raw_sql
        'merge_groups': {
            'INT_SCI': {
                'name': 'Integrated Science',
                'components': ['SCI', 'AGR'],
            },
            ...
        },
        'exam_report_mode': 'INTEGRATED_KNEC',
    }
    """
    merge_groups = get_merge_groups(sub_section)
    students = compute_merged_score_raw_sql(
        school_id, sub_section, exam_name, term, year, class_name, stream
    )

    return {
        'students': students,
        'merge_groups': {
            code: {
                'name': info['name'],
                'components': [c['code'] for c in info['components']],
            }
            for code, info in merge_groups.items()
        },
        'exam_report_mode': 'INTEGRATED_KNEC',
    }


def compute_unmerged_results_sheet_data(school_id, sub_section, exam_name, term, year, class_name, stream):
    """
    Returns the standard unmerged results sheet (individual subjects).
    Used when exam_report_mode='UNMERGED'.
    """
    from students.models import Student, Subject, Mark

    students = Student.objects.filter(
        school_id=school_id,
        class_name=class_name,
        stream=stream,
    ).order_by('admission_no')

    subjects = Subject.objects.filter(
        school_id=school_id,
        school_section='PRIMARY',
        sub_section=sub_section,
        is_active=True,
    ).order_by('code')

    marks = Mark.objects.filter(
        school_id=school_id,
        school_section='PRIMARY',
        sub_section=sub_section,
        term=term,
        exam_type=exam_name,
        year=year,
        student__class_name=class_name,
        student__stream=stream,
    ).select_related('subject', 'student')

    marks_by_student = {}
    for mark in marks:
        sid = mark.student_id
        code = mark.subject.code if mark.subject else None
        if sid not in marks_by_student:
            marks_by_student[sid] = {}
        if code:
            marks_by_student[sid][code] = mark

    student_data = []
    for student in students:
        student_marks = marks_by_student.get(student.id, {})
        subject_scores = {}
        for subj in subjects:
            mark = student_marks.get(subj.code)
            if mark is None:
                subject_scores[subj.code] = None
            elif mark.is_absent:
                subject_scores[subj.code] = 0
            else:
                subject_scores[subj.code] = mark.raw_score if mark.raw_score is not None else mark.score

        student_data.append({
            'student_id': student.id,
            'admission_no': student.admission_no,
            'name': student.name,
            'subjects': subject_scores,
        })

    return {
        'students': student_data,
        'subjects': [{'code': s.code, 'name': s.name} for s in subjects],
        'exam_report_mode': 'UNMERGED',
    }
