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
from django.db.models import Avg, Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from django.http import HttpResponse
from django.views.decorators.cache import never_cache

from .constants import (
    ASSESSMENT_MAP,
    LOWER_PRIMARY_SUBJECT_NAMES,
    LOWER_PRIMARY_SUBJECT_SHORT_MAP,
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
    clear_teacher_cache,
    generate_default_password,
    get_class_teacher_scope,
    get_performance_level,
    get_teacher_for_user,
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
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    if not user_has_main_school_admin_override(request.user):
        teacher = get_teacher_for_user(request.user)
        class_scope = get_class_teacher_scope(teacher)
        if not class_scope or class_scope != (grade, stream):
            messages.error(request, "You are not the class teacher for this class stream.")
            redirect_target = 'teacher_report_forms' if teacher else 'report_card_select'
            return redirect(redirect_target)

    comment_obj, _ = ClassTeacherMasterComment.objects.get_or_create(
        school=school, year=year, term=term, grade=grade, stream=stream, exam_type=db_assessment,
        defaults={'school_section': 'PRIMARY' if is_primary else 'JSS'},
    )

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
        next_url = request.GET.get('next')
        if next_url:
            return redirect(next_url)
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
@never_cache
def teacher_load_comments(request):
    """
    HTMX endpoint: returns an inline comment form card for the teacher's Report Cards page.
    Pre-populated with the class teacher's grade/stream. Posts to manage_master_comments.
    """
    from ..models import ClassTeacherMasterComment
    from .constants import ASSESSMENT_MAP

    school = get_request_school(request)
    if not school:
        return HttpResponse('<div style="padding:20px;color:#94a3b8;">School context required.</div>')

    teacher = get_teacher_for_user(request.user)
    class_scope = get_class_teacher_scope(teacher)
    if not class_scope:
        return HttpResponse('<div style="padding:20px;color:#94a3b8;">Only class teachers can add comments.</div>')

    ct_grade, ct_stream = class_scope

    exam_id = request.GET.get('exam_id', '').strip()
    year = request.GET.get('year', '').strip()
    term = request.GET.get('term', '').strip()
    assessment = request.GET.get('assessment', '').strip()

    # If exam_id is provided, look up year/term/assessment from the Exam model
    if exam_id:
        try:
            from ..models import Exam
            from .constants import ASSESSMENT_SLUG_MAP
            exam_obj = Exam.all_objects.get(id=exam_id)
            year = str(exam_obj.year)
            term = exam_obj.term
            # Convert display name to slug (e.g. "Mid Term Assessment" -> "mid")
            exam_display = exam_obj.name.replace(' Assessment', '').strip()
            assessment = ASSESSMENT_SLUG_MAP.get(exam_display, exam_display)
        except Exam.DoesNotExist:
            return HttpResponse('<div style="padding:20px;color:#94a3b8;">Exam not found.</div>')

    if not (year and term and assessment):
        return HttpResponse('<div style="padding:20px;color:#94a3b8;">Please select an exam first to load the comment form.</div>')

    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    section = get_request_school_section(request) or 'JSS'
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    comment_obj, _ = ClassTeacherMasterComment.objects.get_or_create(
        school=school, year=year, term=term, grade=ct_grade, stream=ct_stream, exam_type=db_assessment,
        defaults={'school_section': 'PRIMARY' if is_primary else 'JSS'},
    )

    context = {
        'comment_obj': comment_obj,
        'selected_grade': ct_grade,
        'selected_stream': ct_stream,
        'selected_year': year,
        'selected_term': term,
        'selected_assessment': assessment,
        'selected_assessment_display': db_assessment,
        'is_primary': is_primary,
        'exam_id': exam_id,
    }
    return render(request, 'students/teacher_comment_form.html', context)


@login_required(login_url='login')
@never_cache
def teacher_class_comments(request):
    """Class Comments page — class teachers only. Auto-loads the latest exam and pre-fills saved comments."""
    from ..models import Exam, ClassTeacherMasterComment
    from .constants import ASSESSMENT_MAP, ASSESSMENT_SLUG_MAP

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('dashboard_alt')

    teacher = get_teacher_for_user(request.user)
    class_teacher_scope = get_class_teacher_scope(teacher)
    if not class_teacher_scope:
        messages.error(request, "Report cards are available to class teachers only.")
        return redirect('merit_list')

    ct_grade, ct_stream = class_teacher_scope
    section = get_request_school_section(request) or 'JSS'
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    # Auto-find the latest exam for this school
    latest_exam = Exam.all_objects.filter(school=school, is_deleted=False).order_by('-year', 'term', 'name').first()

    comment_obj = None
    exam_id = ''
    year = ''
    term = ''
    assessment = ''
    assessment_display = ''

    if latest_exam:
        exam_id = str(latest_exam.id)
        year = str(latest_exam.year)
        term = latest_exam.term
        exam_display = latest_exam.name.replace(' Assessment', '').strip()
        assessment = ASSESSMENT_SLUG_MAP.get(exam_display, exam_display)
        assessment_display = ASSESSMENT_MAP.get(assessment, assessment)

    # Find the most recently saved comment for this teacher's class
    existing_comment = ClassTeacherMasterComment.objects.filter(
        school=school, grade=ct_grade, stream=ct_stream
    ).order_by('-last_modified').first()

    if existing_comment:
        comment_obj = existing_comment
        # Use the saved comment's year/term/exam so form submits to the right record
        year = existing_comment.year
        term = existing_comment.term
        assessment_display = existing_comment.exam_type
        reverse_map = {v: k for k, v in ASSESSMENT_MAP.items()}
        assessment = reverse_map.get(assessment_display, assessment)
    elif latest_exam:
        # No saved comment, create blank
        comment_obj, _ = ClassTeacherMasterComment.objects.get_or_create(
            school=school, year=year, term=term, grade=ct_grade, stream=ct_stream, exam_type=assessment_display,
            defaults={'school_section': 'PRIMARY' if is_primary else 'JSS'},
        )

    return render(request, 'students/teacher_class_comments.html', {
        'selected_grade': ct_grade,
        'selected_stream': ct_stream,
        'selected_exam_id': exam_id,
        'selected_year': year,
        'selected_term': term,
        'selected_assessment': assessment,
        'selected_assessment_display': assessment_display,
        'comment_obj': comment_obj,
        'is_primary': is_primary,
        'latest_exam': latest_exam,
    })


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
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    comment_obj, _ = SchoolHeadteacherComment.objects.get_or_create(
        school=school, year=year, term=term, exam_type=db_assessment,
        school_section='PRIMARY' if is_primary else 'JSS',
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


def section_target_qs(school, section):
    """Return a Teacher queryset for the given workspace section (BOTH -> all)."""
    qs = Teacher.all_objects.filter(school=school, is_active=True)
    if section == 'LOWER_PRIMARY':
        return qs.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['LOWER', None, ''])
    if section == 'PRIMARY':
        return qs.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['UPPER', None, ''])
    if section == 'JSS':
        return qs.filter(school_section__in=['JSS', 'BOTH'])
    return qs


def _refresh_teacher_summary(teacher):
    """Re-derive the denormalised subjects_taught / classes fields."""
    try:
        all_a = teacher.assignments.select_related('subject').all()
        teacher.subjects_taught = ", ".join(sorted(set(a.subject.name for a in all_a)))
        teacher.classes         = ", ".join(sorted(set(f"{a.class_name} {a.stream}" for a in all_a)))
        teacher.save()
    except Exception:
        pass


def _group_assignments_by_subject(assignments):
    """
    Group SubjectAssignment rows by subject.
    Returns a list of dicts:
      [{'subject': <Subject>, 'rows': [<SubjectAssignment>, ...], 'classes': ['G7 Blue', 'G9 Main']}, ...]
    """
    from collections import OrderedDict
    groups = OrderedDict()
    for a in assignments:
        sid = a.subject_id
        if sid not in groups:
            groups[sid] = {
                'subject': a.subject,
                'rows': [],
                'classes': [],
            }
        groups[sid]['rows'].append(a)
        groups[sid]['classes'].append(f"{a.class_name} {a.stream}")
    return list(groups.values())


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

            # Section is now taken EXPLICITLY from the form, not inherited
            # from the workspace. This stops the "I was in JSS workspace and
            # accidentally created a Lower Primary teacher" bug.
            section_token = (request.POST.get('school_section_posting') or '').strip()
            valid_sections = {'LOWER_PRIMARY', 'PRIMARY', 'JSS'}
            if section_token not in valid_sections:
                messages.error(request, "Please pick where this teacher will be posted (Lower / Upper / JSS).")
                return redirect('manage_faculty_matrix')
            if section_token == 'LOWER_PRIMARY':
                db_section, db_sub = 'PRIMARY', 'LOWER'
            elif section_token == 'PRIMARY':
                db_section, db_sub = 'PRIMARY', 'UPPER'
            else:  # JSS
                db_section, db_sub = 'JSS', None

            login_username = f"{phone_number}@{school.code}"

            if User.objects.filter(username=login_username).exists():
                messages.error(request, f"A user with login '{login_username}' is already registered.")
                return redirect('manage_faculty_matrix')
            if Teacher.all_objects.filter(school=school, tsc_number=tsc_number).exists():
                messages.error(request, f"A teacher with TSC Number '{tsc_number}' already exists.")
                return redirect('manage_faculty_matrix')

            try:
                default_password = generate_default_password()
                email_address = request.POST.get('personal_email', '').strip() or request.POST.get('email', '').strip()
                surname = request.POST.get('surname', '').strip()
                with transaction.atomic():
                    new_user = User.objects.create_user(
                        username=login_username,
                        email=email_address,
                        password=default_password,
                        first_name=full_name,
                        last_name=surname,
                    )
                    Teacher.objects.create(
                        user=new_user, title=title, tsc_number=tsc_number,
                        phone_number=phone_number, email=email_address,
                        school=school,
                        school_section=db_section,
                        sub_section=db_sub,
                        assigned_task=request.POST.get('assigned_task', 'Teacher'),
                        subjects_taught='', classes='',
                        must_change_password=True,
                        gender=request.POST.get('gender', ''),
                        national_id=request.POST.get('national_id', '').strip(),
                        address=request.POST.get('address', '').strip(),
                        employee_number=request.POST.get('employee_number', '').strip(),
                        bio=request.POST.get('bio', '').strip(),
                    )

                clear_teacher_cache(new_user.pk)

                # Send welcome email with credentials
                try:
                    site_url = getattr(settings, 'SITE_URL', f"{request.scheme}://{request.get_host()}")
                    login_url = f"{site_url}/login/"
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
                        headers={
                            'Reply-To': settings.DEFAULT_FROM_EMAIL,
                            'Precedence': 'bulk',
                            'List-Unsubscribe': f'<{site_url}/login/>',
                            'X-Auto-Response-Suppress': 'All',
                        },
                    )
                    email.content_subtype = 'html'
                    email.send(fail_silently=False)
                    email_sent = True
                except Exception as e:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.exception("Failed to send welcome email to %s", email_address)
                    email_sent = False

                if email_sent:
                    messages.success(request, f"Profile '{title} {full_name}' created successfully. Login credentials have been sent to {email_address}.")
                else:
                    messages.warning(request, f"Profile '{title} {full_name}' created. Email could not be sent. Username: {login_username} | Password: {default_password}. Use 'Reset Password' to set a new password.")
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Profile creation failed for teacher %s", full_name)
                messages.error(request, "An error occurred while creating the profile. Please try again.")
            return redirect('manage_faculty_matrix')

        # --- Edit existing teacher profile ---
        elif action_type == 'edit_profile':
            school = get_request_school(request)
            try:
                with transaction.atomic():
                    teacher      = Teacher.all_objects.get(id=request.POST.get('teacher_id'), school=school)
                    full_name    = request.POST.get('full_name', '').strip()
                    surname      = request.POST.get('surname', '').strip()
                    phone_number = request.POST.get('phone_number', '').strip()
                    email        = request.POST.get('personal_email', '').strip() or request.POST.get('email', '').strip()
                    new_tsc      = request.POST.get('tsc_number', '').strip()

                    # ── Input validation ───────────────────────────────────
                    if len(full_name) < 2:
                        messages.error(request, "Full name must be at least 2 characters.")
                        return redirect('manage_faculty_matrix')
                    if phone_number and not (phone_number.isdigit() and len(phone_number) == 10 and phone_number.startswith('0')):
                        messages.error(request,
                            "Phone number must be 10 digits starting with 0 (e.g. 0712345678).")
                        return redirect('manage_faculty_matrix')
                    if not new_tsc or not new_tsc.isdigit():
                        messages.error(request, "TSC number must be digits only.")
                        return redirect('manage_faculty_matrix')
                    # TSC must be unique within the school
                    if Teacher.all_objects.filter(school=school, tsc_number=new_tsc).exclude(pk=teacher.pk).exists():
                        messages.error(request,
                            f"Another teacher already has TSC Number '{new_tsc}'.")
                        return redirect('manage_faculty_matrix')
                    # Phone number must be unique within the school (used as login)
                    if phone_number:
                        login_username = f"{phone_number}@{school.code}"
                        if User.objects.filter(username=login_username).exclude(pk=teacher.user_id).exists():
                            messages.error(request,
                                f"Phone {phone_number} is already used as a login username by another account.")
                            return redirect('manage_faculty_matrix')

                    # ── Demographics (school_section / sub_section are intentionally NOT updatable) ──
                    teacher.user.first_name = full_name
                    teacher.user.last_name  = surname
                    teacher.user.username   = f"{phone_number}@{school.code}" if phone_number else teacher.user.username
                    teacher.user.email      = email
                    teacher.user.save()

                    teacher.title          = request.POST.get('title')
                    teacher.tsc_number     = new_tsc
                    teacher.phone_number   = phone_number
                    teacher.email          = email
                    teacher.gender         = request.POST.get('gender', teacher.gender)
                    teacher.national_id    = request.POST.get('national_id', teacher.national_id).strip()
                    teacher.address        = request.POST.get('address', teacher.address).strip()
                    teacher.employee_number = request.POST.get('employee_number', teacher.employee_number).strip()
                    teacher.bio            = request.POST.get('bio', teacher.bio).strip()
                    if 'assigned_task' in request.POST:
                        teacher.assigned_task = request.POST.get('assigned_task', teacher.assigned_task)
                    if request.FILES.get('signature'):
                        teacher.signature = request.FILES['signature']
                    teacher.save()

                clear_teacher_cache(teacher.user_id)
                messages.success(request, f"Demographics updated for {teacher.get_full_title()}.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Profile update failed for teacher_id=%s", request.POST.get('teacher_id'))
                messages.error(request, "An error occurred while updating the profile. Please try again.")
            return redirect('manage_faculty_matrix')

        # --- Delete teacher profile ---
        elif action_type == 'delete_profile':
            school = get_request_school(request)
            try:
                with transaction.atomic():
                    teacher = Teacher.all_objects.get(id=request.POST.get('teacher_id'), school=school)
                    name    = teacher.get_full_title()
                    teacher.user.delete()   # CASCADE removes Teacher record too
                messages.success(request, f"'{name}' and their login account were deleted.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception as e:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Profile deletion failed for teacher_id=%s", request.POST.get('teacher_id'))
                messages.error(request, "An error occurred while deleting the profile. Please try again.")
            return redirect('manage_faculty_matrix')

        # --- Assign subject to teacher ---
        else:
            school = get_request_school(request)
            action = request.POST.get('action_type', 'assign')

            # ── Delete an entire subject group from a teacher ────────────
            if action == 'delete_subject_group':
                teacher_id = request.POST.get('teacher_id')
                subject_id = request.POST.get('subject_id')
                teacher = Teacher.all_objects.filter(id=teacher_id, school=school).first()
                from ..models import Subject
                subject = Subject.all_objects.filter(id=subject_id, school=school).first()
                if not teacher or not subject:
                    messages.error(request, "Teacher or subject not found.")
                    return redirect('manage_faculty_matrix')
                # Section guard: same-section only
                section = get_request_school_section(request)
                if section != 'BOTH' and teacher.school_section != 'BOTH' and teacher.school_section != {
                    'LOWER_PRIMARY': 'PRIMARY', 'PRIMARY': 'PRIMARY', 'JSS': 'JSS'
                }.get(section):
                    messages.error(request, "You can only modify teachers in your current workspace.")
                    return redirect('manage_faculty_matrix')
                deleted, _ = SubjectAssignment.objects.filter(
                    school=school, teacher_profile=teacher, subject=subject
                ).delete()
                _refresh_teacher_summary(teacher)
                messages.success(request,
                    f"Removed {deleted} assignment(s) of {subject.name} from {teacher.get_full_title()}.")
                return redirect('manage_faculty_matrix')

            # ── Reassign an entire subject group to a different teacher ───
            if action == 'reassign_subject_group':
                teacher_id = request.POST.get('teacher_id')
                subject_id = request.POST.get('subject_id')
                new_teacher_id = request.POST.get('new_teacher_id', '').strip()
                if not new_teacher_id:
                    messages.error(request, "Please pick a teacher to reassign to.")
                    return redirect('manage_faculty_matrix')
                from_teacher = Teacher.all_objects.filter(id=teacher_id, school=school).first()
                to_teacher   = Teacher.all_objects.filter(id=new_teacher_id, school=school).first()
                from ..models import Subject
                subject = Subject.all_objects.filter(id=subject_id, school=school).first()
                if not (from_teacher and to_teacher and subject):
                    messages.error(request, "Teacher or subject not found.")
                    return redirect('manage_faculty_matrix')
                if from_teacher.id == to_teacher.id:
                    messages.warning(request, "Source and destination are the same teacher.")
                    return redirect('manage_faculty_matrix')
                # Section guard: target teacher must be in the same section
                section = get_request_school_section(request)
                if section != 'BOTH':
                    valid_targets = section_target_qs(school, section).exclude(id=from_teacher.id)
                    if not valid_targets.filter(id=to_teacher.id).exists():
                        messages.error(request,
                            f"{to_teacher.get_full_title()} is not in the {section} workspace.")
                        return redirect('manage_faculty_matrix')
                # Move all subject assignments
                moved = SubjectAssignment.objects.filter(
                    school=school, teacher_profile=from_teacher, subject=subject
                ).update(teacher_profile=to_teacher)
                _refresh_teacher_summary(from_teacher)
                _refresh_teacher_summary(to_teacher)
                messages.success(request,
                    f"Reassigned {moved} assignment(s) of {subject.name} "
                    f"from {from_teacher.get_full_title()} to {to_teacher.get_full_title()}.")
                return redirect('manage_faculty_matrix')

            # ── Default: create / update one assignment (teacher + subject + class + stream) ──
            teacher_id = request.POST.get('teacher_id')
            section = get_request_school_section(request)
            subject_id = request.POST.get('subject_code')
            grade = request.POST.get('grade', '').strip()
            stream = request.POST.get('stream', '').strip()
            from ..models import Subject
            from ..views.constants import classes_for_section, section_for_class

            # ── Validate section consistency ─────────────────────────────
            if section not in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
                messages.error(request, "Pick a valid workspace before allocating subjects.")
                return redirect('manage_faculty_matrix')
            if not grade or not stream:
                messages.error(request, "Both grade and stream are required.")
                return redirect('manage_faculty_matrix')
            allowed_grades = classes_for_section(section)
            if grade not in allowed_grades:
                messages.error(request,
                    f"Grade '{grade}' is not part of the {section} workspace.")
                return redirect('manage_faculty_matrix')

            teacher = Teacher.all_objects.filter(id=teacher_id, school=school).first()
            # Resolve subject by code + grade (dropdown is deduplicated, so the
            # subject_id is from the first grade — we need the correct one).
            chosen_subject = Subject.all_objects.filter(id=subject_id, school=school).first()
            if chosen_subject:
                subject = Subject.all_objects.filter(
                    school=school, code=chosen_subject.code, grade=grade, is_active=True
                ).first() or chosen_subject
            else:
                subject = None
            if not teacher:
                messages.error(request, "Please pick a teacher.")
                return redirect('manage_faculty_matrix')
            if not subject:
                messages.error(request, "Invalid subject selected.")
                return redirect('manage_faculty_matrix')

            # The subject MUST belong to the section we're assigning into.
            expected_school_section, expected_sub_section = section_for_class(grade)
            if subject.school_section == 'LOWER_PRIMARY' and section == 'LOWER_PRIMARY':
                pass  # OK
            elif subject.school_section == expected_school_section and \
                 (subject.sub_section or '').upper() == (expected_sub_section or '').upper():
                pass  # OK
            elif subject.school_section == 'JSS' and section == 'JSS':
                pass  # OK
            else:
                messages.error(request,
                    f"Subject '{subject.name}' does not belong to the {section} workspace. "
                    f"Switch workspaces or pick a different subject.")
                return redirect('manage_faculty_matrix')

            # The teacher MUST belong to this section.
            teacher_section = teacher.school_section
            teacher_sub = (teacher.sub_section or '').upper()
            if teacher_section == 'BOTH':
                pass  # cross-section teacher can teach anywhere
            elif teacher_section == 'PRIMARY' and section in ('LOWER_PRIMARY', 'PRIMARY'):
                # Derive expected sub from the grade, not the workspace
                _, expected_sub = section_for_class(grade)
                if expected_sub and teacher_sub and teacher_sub != expected_sub.upper():
                    messages.error(request,
                        f"{teacher.get_full_title()} is posted to a different section "
                        f"and cannot be assigned here.")
                    return redirect('manage_faculty_matrix')
                if expected_sub and not teacher_sub:
                    messages.error(request,
                        f"{teacher.get_full_title()} has no sub-section assigned. "
                        f"Update their profile before assigning to {grade}.")
                    return redirect('manage_faculty_matrix')
            elif teacher_section == 'JSS' and section == 'JSS':
                pass
            else:
                messages.error(request,
                    f"{teacher.get_full_title()} is posted to a different section "
                    f"and cannot be assigned here.")
                return redirect('manage_faculty_matrix')

            sub_section = None
            if section == 'LOWER_PRIMARY':
                sub_section = 'LOWER'
            elif section == 'PRIMARY':
                _, sub_section = section_for_class(grade)

            SubjectAssignment.objects.update_or_create(
                school=school,
                class_name=grade,
                stream=stream,
                subject=subject,
                defaults={
                    'teacher_profile_id': teacher_id,
                    'school_section': 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS',
                    'sub_section': sub_section,
                }
            )
            try:
                all_a = teacher.assignments.select_related('subject').all()
                teacher.subjects_taught = ", ".join(set(a.subject.name for a in all_a))
                teacher.classes         = ", ".join(set(f"{a.class_name} {a.stream}" for a in all_a))
                teacher.save()
            except Exception:
                pass
            messages.success(
                request,
                f"Assigned {subject.name} to {teacher.get_full_title()} for {grade} {stream}."
            )
            return redirect('manage_faculty_matrix')

    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # Filter by workspace section
    section = get_request_school_section(request)

    # Section summary (for the page header cards) - counts are school-wide,
    # independent of the workspace filter, so the admin always sees
    # distribution across all 3 sections.
    # Single aggregation query instead of 4 separate COUNT queries
    from django.db.models import Count, Q
    counts = Teacher.all_objects.filter(school=school, is_active=True).aggregate(
        LOWER_PRIMARY=Count('id', filter=Q(school_section='PRIMARY', sub_section='LOWER')),
        PRIMARY=Count('id', filter=Q(school_section='PRIMARY', sub_section='UPPER')),
        JSS=Count('id', filter=Q(school_section='JSS')),
        BOTH=Count('id', filter=Q(school_section='BOTH')),
    )
    section_summary = counts
    section_summary['TOTAL'] = sum(counts.values())

    # Use all_objects to bypass SchoolScopedManager — we explicitly filter
    # by school and sub_section below, and PRIMARY workspace needs BOTH.
    teachers = Teacher.all_objects.filter(school=school).select_related('user').filter(is_active=True)
    assignments = SubjectAssignment.all_objects.filter(school=school).select_related('teacher_profile__user').all()

    # Admin users see ALL teachers regardless of section
    if not user_has_main_school_admin_override(request.user):
        if section == 'LOWER_PRIMARY':
            teachers = teachers.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['LOWER', None])
            assignments = assignments.filter(school_section='PRIMARY', sub_section='LOWER')
        elif section == 'PRIMARY':
            teachers = teachers.filter(school_section__in=['PRIMARY', 'BOTH'], sub_section__in=['LOWER', 'UPPER', None])
            assignments = assignments.filter(school_section='PRIMARY', sub_section__in=['LOWER', 'UPPER'])
        elif section == 'JSS':
            teachers = teachers.filter(school_section__in=['JSS', 'BOTH'])
            assignments = assignments.filter(school_section='JSS')

    # ── Per-teacher metadata for the cards ──
    # Attach extra attributes directly to the Teacher instances so the
    # existing template (`{{ teacher.user.first_name }}`, etc.) keeps
    # working unchanged.
    def _section_label(t):
        if t.school_section == 'BOTH':
            return 'Cross-Section'
        if t.school_section == 'PRIMARY':
            return 'Lower Primary' if t.sub_section == 'LOWER' else 'Upper Primary'
        if t.school_section == 'JSS':
            return 'JSS'
        return '—'

    teacher_assignment_count = dict(
        SubjectAssignment.all_objects.filter(school=school)
        .values('teacher_profile_id').annotate(n=Count('id'))
        .values_list('teacher_profile_id', 'n')
    )

    teachers_list = list(teachers)
    for t in teachers_list:
        t.assignment_count   = teacher_assignment_count.get(t.id, 0)
        t.last_login         = t.user.last_login if t.user_id else None
        t.section_label      = _section_label(t)

    # Pre-attach filtered assignments to each teacher so the template uses
    # these instead of teacher.assignments.all (which hits SchoolScopedManager
    # and loses LOWER primary assignments in PRIMARY workspace).
    from collections import defaultdict
    teacher_id_to_assignments = defaultdict(list)
    for a in assignments:
        teacher_id_to_assignments[a.teacher_profile_id].append(a)
    for t in teachers_list:
        t.filtered_assignments = teacher_id_to_assignments.get(t.id, [])

    # Pull grades from DB (school's actual class structure)
    from ..models import Grade, Stream, Subject

    if section == 'LOWER_PRIMARY':
        db_grades = Grade.all_objects.filter(school=school, school_section='PRIMARY', sub_section='LOWER').order_by('order')
    elif section == 'PRIMARY':
        db_grades = Grade.all_objects.filter(school=school, school_section='PRIMARY', sub_section__in=['LOWER', 'UPPER']).order_by('order')
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

    # Subjects: use all_objects to bypass SchoolScopedManager which forces
    # sub_section='UPPER' in PRIMARY workspace — we need BOTH LOWER and UPPER.
    # Deduplicate by code since the same subject exists per grade but the
    # dropdown picks subject + grade separately.
    # Sort by SUBJECT_DISPLAY_ORDER so the dropdown matches marksheets/report cards.
    from .constants import SUBJECT_DISPLAY_ORDER
    if section == 'LOWER_PRIMARY':
        subjects_for_section = Subject.all_objects.filter(school=school, school_section='PRIMARY', sub_section='LOWER', is_active=True).order_by('grade', 'code')
    elif section == 'PRIMARY':
        subjects_for_section = Subject.all_objects.filter(school=school, school_section='PRIMARY', sub_section__in=['LOWER', 'UPPER'], is_active=True).order_by('grade', 'code')
    else:
        subjects_for_section = Subject.all_objects.filter(school=school, school_section=section, is_active=True).order_by('grade', 'code')

    seen_codes = set()
    unique_subjects = []
    for s in sorted(subjects_for_section, key=lambda x: SUBJECT_DISPLAY_ORDER.get(x.code, 99)):
        if s.code not in seen_codes:
            seen_codes.add(s.code)
            unique_subjects.append(s)
    subjects_for_section = unique_subjects

    # Human label for the current workspace (for the allocation breadcrumb)
    section_breadcrumb_label = {
        'LOWER_PRIMARY': 'Lower Primary (Grades 1-3)',
        'PRIMARY':       'Primary (Grades 1-6)',
        'JSS':           'Junior Secondary (Grades 7-9)',
        'BOTH':          'All Sections',
    }.get(section, section)

    # Grade streams: ALWAYS include all sections so edit modal can show
    # the teacher's current assigned_task even if it's from a different section
    lower_gs = _get_grade_streams(school, 'LOWER_PRIMARY')
    upper_gs = _get_grade_streams(school, 'PRIMARY')
    jss_gs = _get_grade_streams(school, 'JSS')
    all_grade_streams = lower_gs + upper_gs + jss_gs

    if section == 'PRIMARY':
        reassign_qs = Teacher.all_objects.filter(school=school, is_active=True, school_section__in=['PRIMARY', 'BOTH'])
    else:
        reassign_qs = section_target_qs(school, section)

    active_tab = request.GET.get('tab', 'manage')

    return render(request, 'students/manage_faculty.html', {
        'active_tab':         active_tab,
        'assignments':        assignments,
        'assignment_groups':  _group_assignments_by_subject(assignments),
        'teachers':           teachers_list,
        'subjects':           subjects_for_section,
        'grades':             grades_for_section,
        'streams':            streams_for_section,
        'reassign_targets':   reassign_qs,
        'grade_streams':      all_grade_streams,
        'section':            section,
        'section_breadcrumb_label': section_breadcrumb_label,
        'section_summary':    section_summary,
    })


@login_required(login_url='login')
def faculty_grade_streams(request):
    """AJAX endpoint: returns grade_streams options for a given section."""
    from django.http import JsonResponse
    section = request.GET.get('section', 'JSS')
    school = get_request_school(request)
    if not school:
        return JsonResponse({'options': []})
    return JsonResponse({'options': _get_grade_streams(school, section)})


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
        logger.info("Learner profile update attempt for student_id=%s by user_id=%s", student_id, request.user.id)
        
        previous_religion = student.religion
        form = StudentEditForm(request.POST, instance=student, school=student.school)
        if form.is_valid():
            updated_student = form.save(commit=False)

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
            # Refresh from DB
            updated_student.refresh_from_db()

            if previous_religion != updated_student.religion and updated_student.religion in ["CRE", "IRE"]:
                opposite_code = OPPOSITE_RELIGION_SUBJECT.get(updated_student.religion)
                if opposite_code:
                    opposite = Subject.all_objects.filter(
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
        Mark.all_objects.filter(student=student)
        .select_related('subject')
        .order_by('year', 'term', 'exam_type', 'subject')
    )
    is_lower_primary = student.school_section == 'PRIMARY' and student.sub_section == 'LOWER'
    is_primary = student.school_section == 'PRIMARY'
    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
        short_mapping = LOWER_PRIMARY_SUBJECT_SHORT_MAP
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
        short_mapping = PRIMARY_SUBJECT_SHORT_MAP
    else:
        subject_mapping = {s.code: s.name for s in Subject.all_objects.filter(school=student.school)}
        short_mapping = SUBJECT_SHORT_MAP
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
            level_value, points_value = _get_primary_performance(mark.score or 0, school=student.school, section=student.school_section, sub_section=student.sub_section)
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

    # DB-level per-subject average (single query, no Python loops)
    subject_avg_qs = (
        Mark.all_objects.filter(student=student)
        .values('subject__code')
        .annotate(avg_score=Avg('score', filter=Q(score__gt=0)))
    )
    db_subject_avgs = {
        short_mapping.get(row['subject__code'], row['subject__code']): round(row['avg_score'], 1)
        for row in subject_avg_qs if row['avg_score'] is not None
    }

    subject_averages = []
    for short, entries in subject_trends.items():
        avg = db_subject_avgs.get(short, 0)
        subject_averages.append({
            "short": short,
            "name": entries[0]["label"].split(" ")[0] if entries else short,
            "average": avg,
            "exam_count": len(entries),
            "exams": entries,
        })
    subject_averages.sort(key=lambda x: x["average"], reverse=True)

    return render(request, "students/learner_profile.html", {
        "student": student,
        "exam_history": exam_history,
        "chart_history": chart_history,
        "subject_trends": subject_trends,
        "subject_averages": subject_averages,
        "chart_labels": json.dumps(chart_labels),
        "chart_scores": json.dumps(chart_scores),
        "exam_count": len(exam_history),
        "latest_exam": exam_history[0] if exam_history else None,
        "form": form,
        "can_edit_student": can_edit_student,
        "is_school_admin": is_school_admin,
    })


@login_required(login_url='login')
def teachers_list_printout(request):
    """Display the teachers list in the browser for printing."""
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('printouts_hub')

    teachers = Teacher.all_objects.filter(school=school, is_active=True).select_related('user').order_by('user__first_name')

    def _section_label(t):
        if t.school_section == 'BOTH':
            return 'Cross-Section'
        if t.school_section == 'PRIMARY':
            return 'Lower Primary' if t.sub_section == 'LOWER' else 'Upper Primary'
        if t.school_section == 'JSS':
            return 'JSS'
        return '—'

    for t in teachers:
        t.section_label = _section_label(t)

    return render(request, 'students/teachers_list_printout.html', {
        'school': school,
        'teachers': teachers,
        'total_count': teachers.count(),
    })


@login_required(login_url='login')
def download_teachers_list_pdf(request):
    """Generate a PDF of the teachers list for the current school."""
    import base64
    import datetime
    import mimetypes

    from django.http import HttpResponse
    from django.template.loader import render_to_string
    from weasyprint import HTML

    from ..models import Teacher

    school = get_request_school(request)
    if not school:
        from django.http import JsonResponse
        return JsonResponse({'error': 'School context is required.'}, status=400)

    teachers = Teacher.all_objects.filter(school=school, is_active=True).select_related('user').order_by('user__first_name')

    def _section_label(t):
        if t.school_section == 'BOTH':
            return 'Cross-Section'
        if t.school_section == 'PRIMARY':
            return 'Lower Primary' if t.sub_section == 'LOWER' else 'Upper Primary'
        if t.school_section == 'JSS':
            return 'JSS'
        return '—'

    for t in teachers:
        t.section_label = _section_label(t)

    # Embed school logo as base64
    logo_data_uri = ''
    try:
        if school.logo:
            logo_type = mimetypes.guess_type(school.logo.url)[0] or 'image/png'
            with school.logo.open('rb') as f:
                logo_data = base64.b64encode(f.read()).decode('ascii')
            logo_data_uri = f'data:{logo_type};base64,{logo_data}'
    except Exception:
        pass

    template_html = render_to_string('students/teachers_list_pdf.html', {
        'school': school,
        'teachers': teachers,
        'logo_data_uri': logo_data_uri,
        'generated_date': datetime.date.today().strftime('%d %B %Y'),
    })

    try:
        html_doc = HTML(string=template_html, base_url=request.build_absolute_uri('/'))
        pdf_bytes = html_doc.write_pdf()
    except Exception as e:
        from django.http import JsonResponse
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    filename = f"Teachers_List_{school.code}_{datetime.date.today().year}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url='login')
def teacher_classes(request, teacher_id):
    """Show performance cards: one per subject assignment (previous exam), class teacher class last."""
    import datetime as _dt
    from django.db.models import Avg, Count
    from ..models import Teacher, SubjectAssignment, Mark, Student, Exam, GradingConfig

    school = get_request_school(request)
    if not school:
        from django.http import JsonResponse
        return JsonResponse({'error': 'School context required.'}, status=400)

    # Admin users bypass section isolation
    if user_has_main_school_admin_override(request.user):
        teacher = Teacher.all_objects.filter(school=school, id=teacher_id).first()
    else:
        teacher = get_school_object_or_403(Teacher, request, using="all_objects", id=teacher_id)
    if not teacher:
        messages.error(request, "Teacher not found.")
        return redirect('manage_faculty_matrix')

    # Accent colors per grade — emerald green family for PRIMARY, blue/amber for JSS
    grade_colors = {
        'Grade 1': '#B45309', 'Grade 2': '#B45309', 'Grade 3': '#B45309',
        'Grade 4': '#00674F', 'Grade 5': '#00674F', 'Grade 6': '#00674F',
        'Grade 7': '#305CDE', 'Grade 8': '#305CDE', 'Grade 9': '#305CDE',
    }

    # Get all subject assignments for this teacher
    assignments = SubjectAssignment.all_objects.filter(
        school=school, teacher_profile=teacher
    ).select_related('subject').order_by('class_name', 'stream', 'subject__code')

    # Get the latest published exam per section
    now = _dt.date.today()
    latest_exams = {}
    for section_key in ('JSS', 'PRIMARY'):
        exam = Exam.all_objects.filter(
            school=school, school_section=section_key, year=now.year, status='active', is_deleted=False
        ).order_by('-created_at').first()
        if exam:
            latest_exams[section_key] = exam

    # Get grading config
    grading_config = GradingConfig.objects.filter(school=school).first()

    def _mean_grade(avg_score, section_key='PRIMARY'):
        # Always use section-specific default scale
        scale = GradingConfig.get_default_subject_scale(section_key)
        for entry in scale:
            if entry['min_score'] <= avg_score < entry['max_score'] + 1:
                return entry['level']
        return '—'

    def _get_exam_label(exam):
        if not exam:
            return '—', '', ''
        return exam.name.upper(), exam.year, exam.term

    # Parse class teacher assignment
    is_class_teacher = False
    ct_class = None
    ct_stream = None
    if teacher.assigned_task and teacher.assigned_task != 'Teacher':
        task_text = teacher.assigned_task.replace('Class Teacher', '').strip()
        task_words = task_text.split()
        if len(task_words) >= 2:
            ct_class = ' '.join(task_words[:-1])
            ct_stream = task_words[-1]
            is_class_teacher = True

    # Build one card per subject assignment using PREVIOUS exam data
    subject_cards = []
    for a in assignments:
        if not a.subject:
            continue
        class_name = a.class_name
        stream = a.stream
        section_key = 'JSS' if class_name in ('Grade 7', 'Grade 8', 'Grade 9') else 'PRIMARY'
        exam = latest_exams.get(section_key)

        # Latest exam marks for this specific subject in this class+stream
        mark_qs = Mark.all_objects.filter(
            school=school,
            student__class_name=class_name,
            student__stream=stream,
            student__is_active=True,
            subject=a.subject,
        )
        if exam:
            mark_qs = mark_qs.filter(term=exam.term, year=exam.year, exam_type=exam.name)

        stats = mark_qs.aggregate(
            mean_score=Avg('score'),
            mean_points=Avg('points'),
        )
        mean_score = round(stats['mean_score'] or 0, 2)
        mean_points = round(stats['mean_points'] or 0, 4)
        mean_grade = _mean_grade(mean_score, section_key)

        student_count = Student.all_objects.filter(
            school=school, class_name=class_name, stream=stream, is_active=True
        ).count()

        exam_name, exam_year, exam_term = _get_exam_label(exam)
        accent = grade_colors.get(class_name, '#8ae325')

        # Mark if this is the class teacher's supervised class
        is_ct_class = is_class_teacher and class_name == ct_class and stream == ct_stream

        subject_cards.append({
            'class_name': class_name,
            'stream': stream,
            'display_name': f"{class_name} {stream}",
            'subject_code': a.subject.code,
            'subject_name': a.subject.name,
            'mean_score': mean_score,
            'mean_points': mean_points,
            'mean_grade': mean_grade,
            'student_count': student_count,
            'exam_name': exam_name,
            'exam_term': exam_term,
            'exam_year': exam_year,
            'accent': accent,
            'is_ct_class': is_ct_class,
        })

    # Sort: class teacher's class at the end
    subject_cards.sort(key=lambda c: (c['is_ct_class'], c['class_name'], c['stream']))

    return render(request, 'students/teacher_classes.html', {
        'teacher': teacher,
        'cards': subject_cards,
        'is_class_teacher': is_class_teacher,
        'ct_class': ct_class,
        'ct_stream': ct_stream,
    })
