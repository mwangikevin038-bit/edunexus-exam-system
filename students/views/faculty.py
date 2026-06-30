"""
Faculty and learner profile views for the EduNexus student management system.

Handles class teacher master comments, school headteacher remarks,
teacher onboarding/assignment management, and longitudinal learner profiles.
"""

import datetime
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.core.mail import EmailMessage

from .constants import (
    ASSESSMENT_MAP,
    OPPOSITE_RELIGION_SUBJECT,
    PRIMARY_SUBJECT_NAMES,
    PRIMARY_SUBJECT_SHORT_MAP,
    SUBJECT_CHOICES,
    SUBJECT_SHORT_MAP,
)

PRIMARY_SUBJECT_CHOICES = [
    ('ENG', 'English'),
    ('KIS', 'Kiswahili'),
    ('MAT', 'Mathematics'),
    ('SCI', 'Science and Technology'),
    ('SOC', 'Social Studies'),
    ('CRE', 'Christian Religious Education'),
    ('IRE', 'Islamic Religious Education'),
    ('AGR', 'Agriculture and Nutrition'),
    ('CAS', 'Creative Arts'),
]
from .exams import _get_primary_performance
from ..forms import StudentEditForm
from .helpers import (
    calculate_primary_plv,
    calculate_report_plv,
    generate_default_password,
    get_performance_level,
    user_can_edit_learner_profile,
    user_can_view_learner_profile,
)
from ..models import (
    ClassTeacherMasterComment,
    Guardian,
    Mark,
    SchoolHeadteacherComment,
    Student,
    Subject,
    SubjectAssignment,
    Teacher,
)
from ..security import (
    get_request_school,
    get_request_school_section,
    get_school_object_or_403,
    school_admin_required,
    user_has_main_school_admin_override,
)


# ==============================================================================
# SECTION 8 — COMMENTS VIEW
# ==============================================================================

@login_required(login_url='login')
def manage_master_comments(request):
    """
    Renders and saves the PLV-based master comment boxes for a class teacher.
    Primary gets 4 boxes (EE, ME, AE, BE); JSS gets 8 boxes (EE1-BE2).
    Returns a modal fragment for AJAX requests or a full page otherwise.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('report_card_select')

    year       = request.GET.get('year')
    term       = request.GET.get('term')
    grade      = request.GET.get('grade')
    stream     = request.GET.get('stream')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    section = get_request_school_section(request) or 'JSS'
    is_primary = section == 'PRIMARY'

    comment_obj, _ = ClassTeacherMasterComment.objects.get_or_create(
        school=school, year=year, term=term, grade=grade, stream=stream, exam_type=db_assessment,
        defaults={'school_section': section},
    )
    if comment_obj.school_section != section:
        comment_obj.school_section = section
        comment_obj.save(update_fields=['school_section'])

    if request.method == "POST":
        if is_primary:
            for level in ['ee', 'me', 'ae', 'be']:
                setattr(comment_obj, f'comment_{level}', request.POST.get(f'comment_{level}', ''))
        else:
            for level in ['ee1', 'ee2', 'me1', 'me2', 'ae1', 'ae2', 'be1', 'be2']:
                setattr(comment_obj, f'comment_{level}', request.POST.get(f'comment_{level}', ''))
        comment_obj.closing_date = request.POST.get('closing_date') or None
        comment_obj.opening_date = request.POST.get('opening_date') or None
        comment_obj.save()
        messages.success(request, "Report card configuration has been saved.")
        context_key = f"{year}|{term}|{db_assessment}|{grade}|{stream}"
        redirect_url = f'{reverse("report_card_select")}?context={context_key}'
        return redirect(redirect_url)

    context = {
        'comment_obj':        comment_obj,
        'selected_grade':     grade,
        'selected_stream':    stream,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': assessment,
        'is_primary':         is_primary,
    }

    # Return modal fragment for AJAX requests, full page otherwise
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('modal') == 'true':
        return render(request, 'students/comments_modal.html', context)

    return render(request, 'students/comments_modal.html', context)


@login_required(login_url='login')
@school_admin_required
def manage_headteacher_comments(request):
    """
    School admin headteacher remarks: PLV-based headteacher comment boxes.
    Primary gets 4 boxes (EE, ME, AE, BE); JSS gets 8 boxes (EE1-BE2).
    Saved school-wide (not per class) so all report cards share the same remarks.
    Returns a modal fragment for AJAX requests or a full page otherwise.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('report_card_select')

    year       = request.GET.get('year')
    term       = request.GET.get('term')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    section = get_request_school_section(request) or 'JSS'
    is_primary = section == 'PRIMARY'

    comment_obj, _ = SchoolHeadteacherComment.objects.get_or_create(
        school=school, year=year, term=term, exam_type=db_assessment,
        school_section=section,
    )

    if request.method == "POST":
        if is_primary:
            for level in ['ee', 'me', 'ae', 'be']:
                setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
        else:
            for level in ['ee1', 'ee2', 'me1', 'me2', 'ae1', 'ae2', 'be1', 'be2']:
                setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
        comment_obj.save()
        messages.success(request, "Headteacher remarks have been saved for the whole school.")
        redirect_url = f'{reverse("report_card_select")}?year={year}&term={term}&assessment={assessment}'
        return redirect(redirect_url)

    context = {
        'comment_obj':        comment_obj,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': assessment,
        'is_primary':         is_primary,
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('modal') == 'true':
        return render(request, 'students/headteacher_comments_modal.html', context)

    return redirect('report_card_select')


def _get_grade_streams(school, section):
    """Return list of 'Class Teacher Grade X Stream' role options for the current section."""
    from ..models import Grade
    grades = Grade.all_objects.filter(school=school).prefetch_related('streams').order_by('order')
    if section == 'LOWER_PRIMARY':
        grades = grades.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        grades = grades.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        grades = grades.filter(school_section='JSS')
    options = []
    for grade in grades:
        for stream in grade.streams.all():
            options.append(f"Class Teacher {grade.name} {stream.name}")
    return options


# ==============================================================================
# SECTION 9 — FACULTY MANAGEMENT VIEW
# ==============================================================================

@login_required(login_url='login')
@school_admin_required
def manage_faculty_matrix(request):
    """
    Admin control panel for teacher onboarding, profile editing/deletion,
    and subject-grade-stream assignment management.
    """
    if request.method == 'POST':
        action_type = request.POST.get('action_type')

        # --- Create new teacher profile ---
        if action_type == 'create_profile':
            title        = request.POST.get('title')
            full_name    = request.POST.get('full_name', '').strip()
            tsc_number   = request.POST.get('tsc_number', '').strip()
            phone_number = request.POST.get('phone_number', '').strip()
            school       = get_request_school(request)

            if not school:
                messages.error(request, "This admin account is not linked to a school.")
                return redirect('manage_faculty_matrix')

            login_username = f"{phone_number}@{school.code}"

            if User.objects.filter(username=login_username).exists():
                messages.error(request, f"A user with login '{login_username}' is already registered.")
                return redirect('manage_faculty_matrix')
            if Teacher.objects.filter(school=school, tsc_number=tsc_number).exists():
                messages.error(request, f"A teacher with TSC Number '{tsc_number}' already exists.")
                return redirect('manage_faculty_matrix')

            try:
                default_password = generate_default_password()
                email_address = request.POST.get('email', '').strip()
                section = get_request_school_section(request)
                with transaction.atomic():
                    new_user = User.objects.create_user(
                        username=login_username,
                        email=email_address,
                        password=default_password,
                        first_name=full_name,
                        last_name='',
                    )
                    Teacher.objects.create(
                        user=new_user, title=title, tsc_number=tsc_number,
                        phone_number=phone_number, email=email_address,
                        school=school, school_section=section or 'BOTH',
                        assigned_task='Teacher', subjects_taught='', classes='',
                        must_change_password=True,
                    )

                # Send welcome email with credentials
                try:
                    login_url = f"{request.scheme}://{request.get_host()}/login/"
                    email_context = {
                        'teacher_name': f"{title} {full_name}",
                        'username': login_username,
                        'password': default_password,
                        'login_url': login_url,
                        'school_name': school.name,
                    }
                    html_message = render_to_string('email/teacher_welcome_email.html', email_context)
                    from django.conf import settings
                    email = EmailMessage(
                        subject=f'Welcome to EDUNEXUS - Your Login Credentials',
                        body=html_message,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[email_address],
                    )
                    email.content_subtype = 'html'
                    email.send(fail_silently=True)
                    email_sent = True
                except Exception as e:
                    email_sent = False

                if email_sent:
                    messages.success(request, f"Profile '{title} {full_name}' created successfully. Login credentials have been sent to {email_address}.")
                else:
                    messages.warning(request, f"Profile '{title} {full_name}' created. Email could not be sent. Username: {login_username} | Password: {default_password}")
            except Exception as e:
                messages.error(request, f"Error creating profile: {e}")
            return redirect('manage_faculty_matrix')

        # --- Edit existing teacher profile ---
        elif action_type == 'edit_profile':
            school = get_request_school(request)
            try:
                with transaction.atomic():
                    teacher      = Teacher.objects.get(id=request.POST.get('teacher_id'), school=school)
                    full_name    = request.POST.get('full_name', '').strip()
                    phone_number = request.POST.get('phone_number', '').strip()
                    email        = request.POST.get('email', '').strip()

                    teacher.user.first_name = full_name
                    teacher.user.last_name  = ''
                    teacher.user.username   = f"{phone_number}@{teacher.school.code}" if teacher.school_id else phone_number
                    teacher.user.email      = email
                    teacher.user.save()

                    teacher.title         = request.POST.get('title')
                    teacher.tsc_number    = request.POST.get('tsc_number', '').strip()
                    teacher.phone_number  = phone_number
                    teacher.email         = email
                    teacher.assigned_task = request.POST.get('assigned_task', 'Teacher')
                    teacher.save()

                messages.success(request, f"Demographics updated for {teacher.get_full_title()}.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception as e:
                messages.error(request, f"Update failed: {e}")
            return redirect('manage_faculty_matrix')

        # --- Delete teacher profile ---
        elif action_type == 'delete_profile':
            school = get_request_school(request)
            try:
                with transaction.atomic():
                    teacher = Teacher.objects.get(id=request.POST.get('teacher_id'), school=school)
                    name    = teacher.get_full_title()
                    teacher.user.delete()   # CASCADE removes Teacher record too
                messages.success(request, f"'{name}' and their login account were deleted.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception as e:
                messages.error(request, f"Deletion failed: {e}")
            return redirect('manage_faculty_matrix')

        # --- Assign subject to teacher ---
        else:
            school = get_request_school(request)
            teacher_id = request.POST.get('teacher_id')
            section = get_request_school_section(request)
            subject_id = request.POST.get('subject_code')
            from ..models import Subject
            subject = Subject.objects.filter(id=subject_id, school=school).first()
            if subject:
                # Determine sub_section from workspace
                sub_section = None
                if section == 'LOWER_PRIMARY':
                    sub_section = 'LOWER'
                elif section == 'PRIMARY':
                    sub_section = 'UPPER'

                SubjectAssignment.objects.update_or_create(
                    school=school,
                    class_name=request.POST.get('grade'),
                    stream=request.POST.get('stream'),
                    subject=subject,
                    defaults={
                        'teacher_profile_id': teacher_id,
                        'school_section': 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS',
                        'sub_section': sub_section,
                    }
                )
                try:
                    target = Teacher.objects.get(id=teacher_id, school=school)
                    all_a  = target.assignments.select_related('subject').all()
                    target.subjects_taught = ", ".join(set(a.subject.name for a in all_a))
                    target.classes         = ", ".join(set(f"{a.class_name} {a.stream}" for a in all_a))
                    target.save()
                except Teacher.DoesNotExist:
                    pass
                messages.success(request, "Subject assignment updated successfully.")
            else:
                messages.error(request, "Invalid subject selected.")
            return redirect('manage_faculty_matrix')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Filter by workspace section
    section = get_request_school_section(request)

    teachers = Teacher.objects.filter(school=school).select_related('user').filter(is_active=True)
    assignments = SubjectAssignment.objects.filter(school=school).select_related('teacher_profile__user').all()

    if section == 'LOWER_PRIMARY':
        teachers = teachers.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['LOWER', None])
        assignments = assignments.filter(school_section='PRIMARY', sub_section='LOWER')
    elif section == 'PRIMARY':
        teachers = teachers.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['UPPER', None])
        assignments = assignments.filter(school_section='PRIMARY', sub_section='UPPER')
    elif section == 'JSS':
        teachers = teachers.filter(school_section__in=['JSS', 'BOTH'])
        assignments = assignments.filter(school_section='JSS')

    # Pull grades from DB (school's actual class structure)
    from ..models import Grade, Stream, Subject

    if section == 'LOWER_PRIMARY':
        db_grades = Grade.all_objects.filter(school=school, school_section='PRIMARY', sub_section='LOWER').order_by('order')
    elif section == 'PRIMARY':
        db_grades = Grade.all_objects.filter(school=school, school_section='PRIMARY', sub_section='UPPER').order_by('order')
    else:
        db_grades = Grade.all_objects.filter(school=school, school_section=section).order_by('order')
    grades_for_section = [g.name for g in db_grades]

    # Pull streams from DB (school's actual streams)
    if section in ('LOWER_PRIMARY', 'PRIMARY'):
        grade_names = [g.name for g in db_grades]
        db_streams = Stream.all_objects.filter(school=school, school_section='PRIMARY', grade__name__in=grade_names).select_related('grade').order_by('grade__order', 'name')
    else:
        db_streams = Stream.all_objects.filter(school=school, school_section=section).select_related('grade').order_by('grade__order', 'name')
    streams_for_section = list(dict.fromkeys(s.name for s in db_streams))

    # Subjects: from school's Subject table
    if section == 'LOWER_PRIMARY':
        subjects_for_section = Subject.objects.filter(school=school, school_section='LOWER_PRIMARY', is_active=True).order_by('grade', 'code')
    elif section == 'PRIMARY':
        subjects_for_section = Subject.objects.filter(school=school, school_section='PRIMARY', is_active=True).order_by('grade', 'code')
    else:
        subjects_for_section = Subject.objects.filter(school=school, school_section=section, is_active=True).order_by('grade', 'code')

    return render(request, 'students/manage_faculty.html', {
        'assignments': assignments,
        'teachers':    teachers,
        'subjects':    subjects_for_section,
        'grades':      grades_for_section,
        'streams':     streams_for_section,
        'grade_streams': _get_grade_streams(school, section),
    })


@login_required(login_url='login')
def learner_profile(request, student_id):
    """
    Longitudinal learner account: shows a learner's assessment history, subject
    trends, performance levels, and graph-ready analytics across years.
    """
    import logging
    logger = logging.getLogger(__name__)
    student = get_school_object_or_403(Student, request, id=student_id)
    logger.warning(f"GET request - Student stream from DB: {student.stream}, id: {student.id}")
    if not user_can_view_learner_profile(request.user, student):
        messages.error(request, "You are not allowed to view this learner profile.")
        return redirect('class_lists')

    can_edit_student = user_can_edit_learner_profile(request.user, student)
    is_school_admin = user_has_main_school_admin_override(request.user)

    if request.method == "POST":
        if not can_edit_student:
            messages.error(request, "You are not allowed to edit this learner profile.")
            return redirect('learner_profile', student_id=student.id)

        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"POST data: {dict(request.POST)}")
        
        previous_religion = student.religion
        form = StudentEditForm(request.POST, instance=student, school=student.school)
        logger.warning(f"Form stream choices: {form.fields['stream'].choices}")
        logger.warning(f"Form is_valid: {form.is_valid()}")
        if not form.is_valid():
            logger.warning(f"Form errors: {form.errors}")
        if form.is_valid():
            logger.warning(f"Cleaned data: {form.cleaned_data}")
            updated_student = form.save(commit=False)
            logger.warning(f"Updated student stream before save: {updated_student.stream}")

            if is_school_admin:
                guardian_phone = form.cleaned_data["guardian_phone"].strip()
                guardian_obj, _ = Guardian.objects.get_or_create(
                    school=student.school,
                    phone=guardian_phone,
                    defaults={
                        "name": form.cleaned_data["guardian_name"].strip(),
                        "school_section": student.school_section,
                    },
                )
                guardian_obj.name = form.cleaned_data["guardian_name"].strip()
                guardian_obj.save()
                updated_student.guardian = guardian_obj

            updated_student.save()
            logger.warning(f"Updated student stream AFTER save: {updated_student.stream}")
            # Refresh from DB
            updated_student.refresh_from_db()
            logger.warning(f"Updated student stream AFTER refresh: {updated_student.stream}")

            if previous_religion != updated_student.religion and updated_student.religion in ["CRE", "IRE"]:
                opposite_code = OPPOSITE_RELIGION_SUBJECT.get(updated_student.religion)
                if opposite_code:
                    opposite = Subject.objects.filter(
                        school=student.school, code=opposite_code,
                        school_section=student.school_section,
                        grade=student.class_name,
                    ).first()
                    if opposite:
                        Mark.objects.filter(
                            school=student.school,
                            student=updated_student,
                            subject=opposite,
                        ).delete()

            messages.success(request, "Learner account details updated successfully.")
            return redirect('learner_profile', student_id=updated_student.id)

        messages.error(request, "Please correct the highlighted learner details.")
    else:
        form = StudentEditForm(
            instance=student,
            school=student.school,
            initial={
                "guardian_name": student.guardian.name if student.guardian else "",
                "guardian_phone": student.guardian.phone if student.guardian else "",
            },
        )

    marks = (
        Mark.objects.filter(student=student)
        .order_by('year', 'term', 'exam_type', 'subject')
    )
    is_primary = student.school_section == 'PRIMARY'
    subject_mapping = PRIMARY_SUBJECT_NAMES if is_primary else {s.code: s.name for s in Subject.objects.filter(school=student.school)}
    short_mapping = PRIMARY_SUBJECT_SHORT_MAP if is_primary else SUBJECT_SHORT_MAP
    exam_groups = {}
    subject_trends = {}

    for mark in marks:
        key = f"{mark.year}|{mark.term}|{mark.exam_type}"
        if key not in exam_groups:
            exam_groups[key] = {
                "year": mark.year,
                "term": mark.term,
                "exam_type": mark.exam_type,
                "marks": [],
                "total": 0,
                "points": 0,
                "subjects": 0,
            }

        score_value = 0 if mark.is_absent or mark.score is None else mark.score
        if mark.is_absent:
            level_value, points_value = "AB", 0
        elif is_primary:
            level_value, points_value = _get_primary_performance(mark.score or 0)
        else:
            level_value, points_value = get_performance_level(mark.score)
        exam_groups[key]["marks"].append({
            "subject": subject_mapping.get(mark.subject.code, mark.subject.name),
            "short": short_mapping.get(mark.subject.code, mark.subject.code),
            "score": "AB" if mark.is_absent else mark.score,
            "level": level_value,
            "points": "-" if mark.is_absent else points_value,
        })
        exam_groups[key]["total"] += score_value
        exam_groups[key]["points"] += points_value
        exam_groups[key]["subjects"] += 1

        short = short_mapping.get(mark.subject.code, mark.subject.code)
        subject_trends.setdefault(short, []).append({
            "label": f"{mark.exam_type} {mark.term} {mark.year}",
            "score": score_value,
        })

    exam_history = []
    for item in exam_groups.values():
        item["mean"] = round(item["total"] / item["subjects"], 1) if item["subjects"] else 0
        item["plv"] = calculate_primary_plv(item["total"], item["subjects"]) if is_primary else calculate_report_plv(item["points"], item["total"])
        exam_history.append(item)
    exam_history.sort(key=lambda x: (x["year"], x["term"], x["exam_type"]), reverse=True)

    chart_labels = [f"{item['exam_type']} {item['term']} {item['year']}" for item in reversed(exam_history)]
    chart_scores = [item["mean"] for item in reversed(exam_history)]
    chart_history = list(reversed(exam_history))

    return render(request, "students/learner_profile.html", {
        "student": student,
        "exam_history": exam_history,
        "chart_history": chart_history,
        "subject_trends": subject_trends,
        "chart_labels": json.dumps(chart_labels),
        "chart_scores": json.dumps(chart_scores),
        "exam_count": len(exam_history),
        "latest_exam": exam_history[0] if exam_history else None,
        "form": form,
        "can_edit_student": can_edit_student,
        "is_school_admin": is_school_admin,
    })
