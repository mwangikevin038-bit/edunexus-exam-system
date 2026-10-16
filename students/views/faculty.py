"""
Faculty and learner profile views for the EduNexus student management system.

Handles class teacher master comments, school headteacher remarks,
teacher onboarding/assignment management, and longitudinal learner profiles.
"""

import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Avg, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.core.mail import EmailMessage
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache

from .constants import (
    ASSESSMENT_MAP,
    LOWER_PRIMARY_SUBJECT_NAMES,
    LOWER_PRIMARY_SUBJECT_SHORT_MAP,
    OPPOSITE_RELIGION_SUBJECT,
    PRIMARY_SUBJECT_NAMES,
    PRIMARY_SUBJECT_SHORT_MAP,
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
    bump_report_cache_version,
    calculate_primary_plv,
    calculate_report_plv,
    clear_teacher_cache,
    discard_media_file,
    filter_exams_by_classes,
    form_field_edited,
    generate_default_password,
    get_class_teacher_scope,
    get_performance_level,
    get_teacher_for_user,
    invalidate_report_forms_cache,
    polish_signature_upload,
    safe_pdf_filename,
    save_school_signature,
    user_can_edit_learner_profile,
    user_can_view_learner_profile,
    validate_signature_upload,
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

def resolve_exam_type_name(school, year, term, assessment, section=None):
    """Canonical ``exam_type`` for ClassTeacherMasterComment / SchoolHeadteacherComment.

    Marks, ExamSummary and every report-card reader key comments on the Exam's
    real name (``'Opener Assessment'``). Request params can arrive as a slug
    (``'opener'``) or a shortened label (``'Opener'``), and writing those
    straight through creates a row no reader ever matches — which is how an
    ``'End of Term'`` comment ended up sitting next to the
    ``'End of Term Assessment'`` one nobody could load.
    """
    from ..models import Exam
    from .constants import ASSESSMENT_SLUG_MAP

    raw = (assessment or '').strip()
    if not raw:
        return raw

    base = Exam.all_objects.filter(school=school, is_deleted=False)
    if year:
        base = base.filter(year=year)
    if term:
        base = base.filter(term=term)

    def _first(qs):
        return qs.order_by('-id').values_list('name', flat=True).first()

    # Section-scoped exact match FIRST: a Grade 4 (PRIMARY) comment must
    # never resolve to a same-named JSS exam. An unscoped exact match let
    # 'KNEC ASSESSMENT' (a JSS exam) swallow a PRIMARY Grade 4 remark.
    if section:
        exact = _first(base.filter(name=raw, school_section=section))
        if exact:
            return exact
    exact = _first(base.filter(name=raw))
    if exact:
        return exact

    slug = (
        ASSESSMENT_SLUG_MAP.get(raw)
        or ASSESSMENT_SLUG_MAP.get(ASSESSMENT_MAP.get(raw, raw), raw)
    )
    label = ASSESSMENT_MAP.get(slug, raw)

    scoped = base.filter(school_section=section) if section else base
    match = (
        _first(scoped.filter(name__istartswith=label))
        or _first(base.filter(name__istartswith=label))
        # Nothing in this year/term yet (e.g. a comment written before the
        # End-of-Term exam existed). Readers match on Exam.name exactly, so a
        # shortened label would silently never be found again.
        or _first(
            Exam.all_objects.filter(school=school, is_deleted=False)
            .filter(name__istartswith=label)
        )
    )
    return match or ASSESSMENT_MAP.get(raw, raw)


# --- Headteacher comment helpers -------------------------------------------------
# F4 (all-keys rule): the native admin forms always post EVERY box for the
# active section. A partial POST (e.g. a stray XHR that only includes one
# ht_comment_* key) must never blank the other boxes, so we only write the
# comment fields when ALL active-section keys are present in request.POST.
# Signature-only / probe POSTs that carry no ht_comment_* keys at all are
# likewise left alone (the signature guard above already handles those).

HT_PRIMARY_BOXES = ('ee', 'me', 'ae', 'be')
HT_JSS_BOXES = ('ee1', 'ee2', 'me1', 'me2', 'ae1', 'ae2', 'be1', 'be2')


def _ht_post_is_complete(request, is_primary):
    """True only when every active-section ht_comment_* key is present in POST."""
    levels = HT_PRIMARY_BOXES if is_primary else HT_JSS_BOXES
    return all(f'ht_comment_{lvl}' in request.POST for lvl in levels)


def _seed_ht_comment_from_sibling(school, year, term, section):
    """Newest non-empty headteacher-comment row for the same school/year/term/section.

    Used by ``_get_or_create_ht_comment`` so a brand-new exam scope (e.g. the
    End-of-Term exam created mid-term) inherits the Principal's existing
    remarks instead of binding an empty row that shadows the fallback on cards.
    """
    qs = SchoolHeadteacherComment.all_objects.filter(
        school=school, year=str(year), term=term, school_section=section,
    ).order_by('-last_modified', '-id')
    for row in qs:
        for lvl in (HT_PRIMARY_BOXES if section == 'PRIMARY' else HT_JSS_BOXES):
            if (getattr(row, f'ht_comment_{lvl}', '') or '').strip():
                return row
    return None


def _get_or_create_ht_comment(school, year, term, exam_type, section):
    """get_or_create that seeds a NEW row from the newest non-empty sibling.

    Existing rows are returned as-is (never re-seeded) so a headteacher who
    deliberately cleared a box keeps it cleared across exam switches.
    """
    from django.db import IntegrityError
    try:
        comment_obj, created = SchoolHeadteacherComment.all_objects.get_or_create(
            school=school, year=str(year), term=term, exam_type=exam_type,
            school_section=section,
        )
    except IntegrityError:
        comment_obj = SchoolHeadteacherComment.all_objects.filter(
            school=school, year=str(year), term=term, exam_type=exam_type,
            school_section=section,
        ).first()
        if comment_obj is None:
            raise
        return comment_obj, False
    if created:
        sibling = _seed_ht_comment_from_sibling(school, year, term, section)
        if sibling is not None and sibling.pk != comment_obj.pk:
            for lvl in (HT_PRIMARY_BOXES if section == 'PRIMARY' else HT_JSS_BOXES):
                setattr(
                    comment_obj, f'ht_comment_{lvl}',
                    getattr(sibling, f'ht_comment_{lvl}', '') or '',
                )
            comment_obj.save(update_fields=[
                f'ht_comment_{lvl}' for lvl in
                (HT_PRIMARY_BOXES if section == 'PRIMARY' else HT_JSS_BOXES)
            ] + ['last_modified'])
    return comment_obj, created


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
    from .constants import section_for_class
    _cmt_section, _cmt_sub = section_for_class(grade) if grade else (None, None)
    db_assessment = resolve_exam_type_name(
        school, year, term, assessment, section=_cmt_section,
    )

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

    # all_objects + section from the CLASS (not the workspace) — scoped
    # .objects hid JSS comments when an admin's workspace was PRIMARY, and
    # school_section was written from workspace so JSS rows landed as PRIMARY.
    from .constants import section_for_class as _sfc
    _grade_sec, _ = _sfc(grade) if grade else (None, None)
    _comment_section = _grade_sec or ('PRIMARY' if is_primary else 'JSS')
    comment_obj, _ = ClassTeacherMasterComment.all_objects.get_or_create(
        school=school, year=str(year), term=term, grade=grade, stream=stream, exam_type=db_assessment,
        defaults={'school_section': _comment_section},
    )

    if request.method == "POST":
        # F4 all-keys rule: only write the comment boxes when EVERY active-
        # section comment_* key is present. Native forms always post all
        # keys, so a legit save/clear still works; a partial POST becomes a
        # no-op instead of blanking the other boxes.
        _cmt_levels = ['ee', 'me', 'ae', 'be'] if is_primary else [
            'ee1', 'ee2', 'me1', 'me2', 'ae1', 'ae2', 'be1', 'be2'
        ]
        if all(f'comment_{lvl}' in request.POST for lvl in _cmt_levels):
            for level in _cmt_levels:
                setattr(comment_obj, f'comment_{level}', request.POST.get(f'comment_{level}', ''))
            # Only touch dates when they are actually present in the POST so
            # a partial (comment-only) POST never blanks the exam window.
            if 'closing_date' in request.POST:
                comment_obj.closing_date = request.POST.get('closing_date') or None
            if 'opening_date' in request.POST:
                comment_obj.opening_date = request.POST.get('opening_date') or None
            comment_obj.save()
            # A saved comment must stick until the teacher changes it, and a
            # BLANKED box must actually blank — so clear any frozen Mark snapshot
            # for this scope so stale frozen text never resurfaces as a fallback.
            Mark.all_objects.filter(
                school=school, year=year, term=term,
                student__class_name=grade, student__stream=stream,
                exam_type=db_assessment,
            ).update(
                frozen_class_teacher_comment='',
                frozen_closing_date=None,
                frozen_opening_date=None,
            )
            # Compiled PDF fragments embed the comment text, and the report_forms
            # cache holds the rendered context — bump the version and evict both
            # so newly saved comments show up immediately, never at TTL expiry.
            bump_report_cache_version(school.pk)
            invalidate_report_forms_cache(school.pk)
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
            exam_obj = Exam.all_objects.get(id=exam_id)
            year = str(exam_obj.year)
            term = exam_obj.term
            # Comments are keyed on the Exam's real name. Slugifying it here
            # ('Opener Assessment' -> 'Opener') produced a key the report card
            # never queries, so the saved comment never showed up.
            assessment = exam_obj.name
        except Exam.DoesNotExist:
            return HttpResponse('<div style="padding:20px;color:#94a3b8;">Exam not found.</div>')

    if not (year and term and assessment):
        return HttpResponse('<div style="padding:20px;color:#94a3b8;">Please select an exam first to load the comment form.</div>')

    section = get_request_school_section(request) or 'JSS'
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    from .constants import section_for_class
    _cmt_section, _cmt_sub = section_for_class(ct_grade) if ct_grade else (None, None)
    db_assessment = resolve_exam_type_name(
        school, year, term, assessment, section=_cmt_section,
    )

    # all_objects + section from the CLASS (not the workspace) — scoped
    # .objects hid JSS comments when an admin's workspace was PRIMARY, and
    # school_section was written from workspace so JSS rows landed as PRIMARY.
    comment_obj, _ = ClassTeacherMasterComment.all_objects.get_or_create(
        school=school, year=str(year), term=term, grade=ct_grade, stream=ct_stream, exam_type=db_assessment,
        defaults={'school_section': _cmt_section or ('PRIMARY' if is_primary else 'JSS')},
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
    """Class Comments page - class teachers only. Auto-loads the latest exam and pre-fills saved comments."""
    from ..models import Exam, ClassTeacherMasterComment

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

    # Auto-find the latest exam for this school. Newest year, then newest
    # term, then newest row — the old `'-year', 'term'` ordering kept
    # ascending on term, so it opened the *earliest* exam of the year.
    # Scoped to the teacher's grade: an exam the admin assigned only to
    # other grades must never become this class's comment context.
    latest_exam = (
        filter_exams_by_classes(
            Exam.all_objects.filter(school=school, is_deleted=False),
            ct_grade,
        )
        .order_by('-year', '-term', '-id')
        .first()
    )

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
        # Comments are keyed on the Exam's real name — the same string
        # Mark.exam_type / ExamSummary.exam_name and every report-card reader
        # use. Slugifying it produced 'Opener' / 'End of Term' rows that were
        # never matched again.
        assessment = latest_exam.name
        assessment_display = latest_exam.name

    # Prefer the comment for the exam we just resolved, then the most recently
    # edited one so older records stay reachable. always all_objects — scoped
    # .objects hides rows when the admin's workspace section differs.
    existing_comment = None
    if assessment_display:
        existing_comment = ClassTeacherMasterComment.all_objects.filter(
            school=school, grade=ct_grade, stream=ct_stream,
            year=str(year), term=term, exam_type=assessment_display,
        ).order_by('-last_modified').first()
    if not existing_comment:
        existing_comment = ClassTeacherMasterComment.all_objects.filter(
            school=school, grade=ct_grade, stream=ct_stream
        ).order_by('-last_modified').first()

    matches_current_exam = bool(
        existing_comment
        and str(existing_comment.year) == str(year)
        and existing_comment.term == term
        and existing_comment.exam_type == assessment_display
    )

    if matches_current_exam:
        comment_obj = existing_comment
    elif latest_exam:
        # No saved comment for the current exam — open a blank one.
        from .constants import section_for_class as _sfc
        _grade_sec, _ = _sfc(ct_grade) if ct_grade else (None, None)
        comment_obj, _ = ClassTeacherMasterComment.all_objects.get_or_create(
            school=school, year=str(year), term=term, grade=ct_grade, stream=ct_stream, exam_type=assessment_display,
            defaults={'school_section': _grade_sec or ('PRIMARY' if is_primary else 'JSS')},
        )
    elif existing_comment:
        comment_obj = existing_comment
        year = existing_comment.year
        term = existing_comment.term
        assessment_display = existing_comment.exam_type
        assessment = existing_comment.exam_type

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

    year       = request.GET.get('year') or str(__import__('datetime').date.today().year)
    term       = request.GET.get('term') or 'Term 3'
    assessment = request.GET.get('assessment', 'opener')
    # Optional section override so the standalone Comments page can edit
    # JSS and Primary without switching the admin workspace.
    section = (
        request.GET.get('section')
        or request.POST.get('section')
        or get_request_school_section(request)
        or 'JSS'
    ).upper()
    if section == 'LOWER_PRIMARY':
        section = 'PRIMARY'
    is_primary = section == 'PRIMARY'

    db_assessment = resolve_exam_type_name(
        school, year, term, assessment,
        section='PRIMARY' if is_primary else 'JSS',
    )

    # all_objects: section override must not be filtered by the session
    # workspace section (JSS admin opening Primary remarks).
    # F1 seed-on-create: a brand-new exam scope inherits the Principal's
    # existing remarks from the newest non-empty sibling row for this
    # school/year/term/section, so comments stick across exam switches.
    comment_obj, _ = _get_or_create_ht_comment(
        school, year, term, db_assessment,
        'PRIMARY' if is_primary else 'JSS',
    )

    if request.method == "POST":
        sig_upload = request.FILES.get('headteacher_signature')
        # Signature-only AJAX save from the capture modal: just the file +
        # X-Requested-With, no comment fields. Must run BEFORE the full-form
        # handler below, which would blank every remark box with '' defaults.
        if (
            sig_upload
            and request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            and not any(k.startswith('ht_comment_') for k in request.POST)
        ):
            sig_error, sig_url = save_school_signature(school, sig_upload)
            if sig_error:
                return JsonResponse({'ok': False, 'message': sig_error}, status=400)
            bump_report_cache_version(school.pk)
            invalidate_report_forms_cache(school.pk)
            return JsonResponse({
                'ok': True,
                'message': 'Signature saved!',
                'signature_url': sig_url,
            })

        # F4 all-keys rule: only write the comment boxes when EVERY active-
        # section ht_comment_* key is present. Native forms always post all
        # keys, so a legit save/clear still works; a partial POST (stray XHR,
        # probe script) becomes a no-op instead of blanking the other boxes.
        if _ht_post_is_complete(request, is_primary):
            if is_primary:
                for level in HT_PRIMARY_BOXES:
                    setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
            else:
                for level in HT_JSS_BOXES:
                    setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
            comment_obj.save()
            # Blank a box → it must actually blank. Clear the frozen headteacher
            # snapshot across the whole section/scope so stale frozen text never
            # resurfaces after the headteacher deletes a remark.
            Mark.all_objects.filter(
                school=school, year=year, term=term, exam_type=db_assessment,
                school_section='PRIMARY' if is_primary else 'JSS',
            ).update(frozen_headteacher_comment='')

        # Optional headteacher signature — shared save_school_signature
        # validates, cleans, discards the old file and drops the School
        # object cache so report cards pick it up on the next request.
        sig_upload = request.FILES.get('headteacher_signature')
        if sig_upload:
            sig_error, _sig_url = save_school_signature(school, sig_upload)
            if sig_error:
                messages.error(request, sig_error)

        # Same as master comments: compiled PDF fragments embed remark text.
        bump_report_cache_version(school.pk)
        invalidate_report_forms_cache(school.pk)
        messages.success(request, "Headteacher remarks have been saved for the whole school.")
        # Standalone Comments page posts here — stay on that page.
        if request.POST.get('next') == 'admin_comments' or request.GET.get('from') == 'comments':
            from urllib.parse import urlencode
            return redirect(
                reverse('admin_comments') + '?' + urlencode({
                    'section': 'PRIMARY' if is_primary else 'JSS',
                    'year': year, 'term': term, 'assessment': assessment,
                })
            )
        redirect_url = f'{reverse("report_card_select")}?year={year}&term={term}&assessment={assessment}'
        return redirect(redirect_url)

    context = {
        'comment_obj':        comment_obj,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': assessment,
        'selected_section':   'PRIMARY' if is_primary else 'JSS',
        'is_primary':         is_primary,
        'school_signature':   school.headteacher_signature.url if getattr(school, 'headteacher_signature', None) else '',
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.GET.get('modal') == 'true':
        return render(request, 'students/headteacher_comments_modal.html', context)

    return redirect('report_card_select')


@login_required(login_url='login')
@school_admin_required
def admin_comments(request):
    """
    Admin Comments workspace — whole-school Headteacher/Principal remarks.
    Same PLV boxes as Class Comments (4 Primary / 8 JSS), saved school-wide
    so every report card fetches them from SchoolHeadteacherComment.
    """
    from ..models import Exam

    school = get_request_school(request)
    if not school:
        messages.error(request, "No school context found.")
        return redirect('school_admin_dashboard')

    section = (request.GET.get('section') or 'JSS').upper()
    if section not in ('JSS', 'PRIMARY'):
        section = 'JSS'
    is_primary = section == 'PRIMARY'

    # Optional year/term/assessment overrides; default to latest exam in section.
    year = (request.GET.get('year') or '').strip()
    term = (request.GET.get('term') or '').strip()
    assessment = (request.GET.get('assessment') or '').strip()

    exam_qs = Exam.all_objects.filter(school=school, is_deleted=False)
    if is_primary:
        exam_qs = exam_qs.filter(school_section='PRIMARY')
    else:
        exam_qs = exam_qs.filter(school_section='JSS')

    latest_exam = exam_qs.order_by('-year', '-term', '-id').first()

    if not year and latest_exam:
        year = str(latest_exam.year)
    if not term and latest_exam:
        term = latest_exam.term
    if not assessment and latest_exam:
        assessment = latest_exam.name
    if not year:
        year = str(__import__('datetime').date.today().year)
    if not term:
        term = 'Term 3'
    if not assessment:
        assessment = 'opener'

    db_assessment = resolve_exam_type_name(
        school, year, term, assessment,
        section='PRIMARY' if is_primary else 'JSS',
    )

    # Sticky: prefer the row for this exact context, else create it seeded
    # from the newest non-empty sibling (F1) so a new exam scope inherits
    # the Principal's existing remarks instead of shadowing the fallback
    # with an empty exact row.
    # all_objects bypasses the section-scoped manager so a JSS workspace
    # session can still load/save the PRIMARY remark row (and vice versa).
    comment_obj, _ = _get_or_create_ht_comment(
        school, year, term, db_assessment,
        'PRIMARY' if is_primary else 'JSS',
    )

    if request.method == 'POST':
        # F4 all-keys rule: only write the comment boxes when EVERY active-
        # section ht_comment_* key is present. Native forms always post all
        # keys, so a legit save/clear still works; a partial POST (stray XHR,
        # probe script) becomes a no-op instead of blanking the other boxes.
        if _ht_post_is_complete(request, is_primary):
            if is_primary:
                for level in HT_PRIMARY_BOXES:
                    setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
            else:
                for level in HT_JSS_BOXES:
                    setattr(comment_obj, f'ht_comment_{level}', request.POST.get(f'ht_comment_{level}', ''))
            comment_obj.save()

            Mark.all_objects.filter(
                school=school, year=year, term=term, exam_type=db_assessment,
                school_section='PRIMARY' if is_primary else 'JSS',
            ).update(frozen_headteacher_comment='')

        sig_upload = request.FILES.get('headteacher_signature')
        if sig_upload:
            sig_error, _sig_url = save_school_signature(school, sig_upload)
            if sig_error:
                messages.error(request, sig_error)

        bump_report_cache_version(school.pk)
        invalidate_report_forms_cache(school.pk)
        messages.success(
            request,
            f"{'Primary' if is_primary else 'JSS'} Headteacher remarks saved. They will appear on every report card until you change them.",
        )
        from urllib.parse import urlencode
        return redirect(
            reverse('admin_comments') + '?' + urlencode({
                'section': section, 'year': year,
                'term': term, 'assessment': assessment,
            })
        )

    return render(request, 'students/admin_comments.html', {
        'comment_obj':           comment_obj,
        'selected_year':         year,
        'selected_term':         term,
        'selected_assessment':   assessment,
        'selected_assessment_display': db_assessment,
        'selected_section':      section,
        'is_primary':            is_primary,
        'latest_exam':           latest_exam,
        'school_signature':      school.headteacher_signature.url if getattr(school, 'headteacher_signature', None) else '',
    })


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

            # Phone IS the teacher's login (phonenumber@schoolcode) — a blank
            # here used to build the username '@schoolcode' and create an
            # account that can never log in.
            if not (phone_number.isdigit() and len(phone_number) == 10 and phone_number.startswith('0')):
                messages.error(
                    request,
                    "Phone number is required — 10 digits starting with 0 (e.g. 0712345678). "
                    "It is the teacher's login (phonenumber@schoolcode).",
                )
                return redirect('manage_faculty_matrix')

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
                    new_teacher = Teacher.objects.create(
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

                    # One class teacher per class stream: demote anyone the new
                    # profile displaces (same rule as edit_profile) so report
                    # cards never have two candidate signatures to pick from.
                    if (new_teacher.assigned_task or '').startswith('Class Teacher'):
                        conflict_list = list(Teacher.all_objects.filter(
                            school=school, is_active=True,
                            assigned_task=new_teacher.assigned_task,
                        ).exclude(pk=new_teacher.pk))
                        for conflict in conflict_list:
                            conflict.assigned_task = 'Teacher'
                            conflict.save(update_fields=['assigned_task'])
                            clear_teacher_cache(conflict.user_id)
                            try:
                                from django.core.cache import cache
                                cache.delete(f"ct_scope:{school.pk}:{conflict.pk}")
                            except Exception:
                                pass
                        if conflict_list:
                            names = ', '.join(c.get_full_title() for c in conflict_list)
                            messages.warning(request,
                                f"{names} was/were replaced as {new_teacher.assigned_task}.")

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
                    email = EmailMessage(
                        subject='Welcome to EDUNEXUS - Your Login Credentials',
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
                except Exception:
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.exception("Failed to send welcome email to %s", email_address)
                    email_sent = False

                if email_sent:
                    messages.success(request, f"Profile '{title} {full_name}' created successfully. Login credentials have been sent to {email_address}.")
                else:
                    messages.warning(request, f"Profile '{title} {full_name}' created. Email could not be sent. Username: {login_username} | Password: {default_password}. Use 'Reset Password' to set a new password.")
            except Exception:
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
                    full_name    = (request.POST.get('full_name', '') or '').strip()
                    surname      = (request.POST.get('surname', '') or '').strip()
                    phone_number = (request.POST.get('phone_number', '') or '').strip()
                    email        = (request.POST.get('personal_email', '').strip() or request.POST.get('email', '').strip())
                    new_tsc      = (request.POST.get('tsc_number', '') or '').strip()

                    changed_user_fields = []
                    changed_teacher_fields = []

                    # ── Names: persist ONLY fields the user actually edited ──
                    if form_field_edited(request, 'full_name', teacher.user.first_name):
                        if len(full_name) < 2:
                            messages.error(request, "Full name must be at least 2 characters.")
                            return redirect('manage_faculty_matrix')
                        teacher.user.first_name = full_name
                        changed_user_fields.append('first_name')
                    if form_field_edited(request, 'surname', teacher.user.last_name):
                        teacher.user.last_name = surname
                        changed_user_fields.append('last_name')

                    # ── Phone IS the login identity — validate and sync the
                    # username ONLY when the user changed it. An untouched
                    # phone is never rewritten or re-uniqueness-checked. ──
                    if form_field_edited(request, 'phone_number', teacher.phone_number):
                        if not phone_number:
                            messages.error(request,
                                "Phone number is required — it is the teacher's login (phonenumber@schoolcode).")
                            return redirect('manage_faculty_matrix')
                        if not (phone_number.isdigit() and len(phone_number) == 10 and phone_number.startswith('0')):
                            messages.error(request,
                                "Phone number must be 10 digits starting with 0 (e.g. 0712345678).")
                            return redirect('manage_faculty_matrix')
                        login_username = f"{phone_number}@{school.code}"
                        if User.objects.filter(username=login_username).exclude(pk=teacher.user_id).exists():
                            messages.error(request,
                                f"Phone {phone_number} is already used as a login username by another account.")
                            return redirect('manage_faculty_matrix')
                        teacher.phone_number = phone_number
                        teacher.user.username = login_username
                        changed_teacher_fields.append('phone_number')
                        changed_user_fields.append('username')

                    # ── Email: blank keeps the old value on BOTH rows — an
                    # untouched/empty field must never wipe an address. ──
                    if email and form_field_edited(request, 'personal_email', teacher.email):
                        teacher.email = email
                        teacher.user.email = email
                        changed_teacher_fields.append('email')
                        changed_user_fields.append('email')

                    # ── TSC: validate real edits only; blank keeps old ──
                    if new_tsc and form_field_edited(request, 'tsc_number', teacher.tsc_number):
                        if not new_tsc.isdigit():
                            messages.error(request, "TSC number must be digits only.")
                            return redirect('manage_faculty_matrix')
                        if Teacher.all_objects.filter(school=school, tsc_number=new_tsc).exclude(pk=teacher.pk).exists():
                            messages.error(request,
                                f"Another teacher already has TSC Number '{new_tsc}'.")
                            return redirect('manage_faculty_matrix')
                        teacher.tsc_number = new_tsc
                        changed_teacher_fields.append('tsc_number')

                    # ── title is NOT NULL — never write blank/None. Only a
                    # real edit (Mr. → Ms.) persists; the modal's "Mr."
                    # prefill default can no longer CREATE a title nobody
                    # entered. ──
                    _title = (request.POST.get('title') or '').strip()
                    if _title and form_field_edited(request, 'title', teacher.title):
                        teacher.title = _title
                        changed_teacher_fields.append('title')

                    # ── Demographics: write only fields the user edited ──
                    if form_field_edited(request, 'gender', teacher.gender):
                        teacher.gender = request.POST.get('gender') or ''
                        changed_teacher_fields.append('gender')
                    if form_field_edited(request, 'national_id', teacher.national_id):
                        teacher.national_id = (request.POST.get('national_id') or '').strip()
                        changed_teacher_fields.append('national_id')
                    if form_field_edited(request, 'address', teacher.address):
                        teacher.address = (request.POST.get('address') or '').strip()
                        changed_teacher_fields.append('address')
                    if form_field_edited(request, 'employee_number', teacher.employee_number):
                        teacher.employee_number = (request.POST.get('employee_number') or '').strip()
                        changed_teacher_fields.append('employee_number')
                    if form_field_edited(request, 'bio', teacher.bio):
                        teacher.bio = (request.POST.get('bio') or '').strip()
                        changed_teacher_fields.append('bio')

                    # ── Assigned role: only when the user changed it. The
                    # modal keeps Save disabled until the role select is
                    # applied, so a slow role load can't silently demote a
                    # class teacher (or displace another one). ──
                    if form_field_edited(request, 'assigned_task', teacher.assigned_task):
                        new_task = request.POST.get('assigned_task') or teacher.assigned_task
                        if new_task.startswith('Class Teacher'):
                            conflict_list = list(Teacher.all_objects.filter(
                                school=school, is_active=True, assigned_task=new_task,
                            ).exclude(pk=teacher.pk))
                            for conflict in conflict_list:
                                conflict.assigned_task = 'Teacher'
                                conflict.save(update_fields=['assigned_task'])
                                clear_teacher_cache(conflict.user_id)
                                try:
                                    from django.core.cache import cache
                                    cache.delete(f"ct_scope:{school.pk}:{conflict.pk}")
                                except Exception:
                                    pass
                            if conflict_list:
                                names = ', '.join(c.get_full_title() for c in conflict_list)
                                messages.warning(request,
                                    f"{names} was/were replaced as {new_task}.")
                        teacher.assigned_task = new_task
                        changed_teacher_fields.append('assigned_task')

                    # Persist ONLY the edited fields — untouched demographics
                    # are never written back (a stale form page can no longer
                    # revert newer data, and a signature-only save writes no
                    # profile fields at all).
                    if changed_user_fields:
                        teacher.user.save(update_fields=changed_user_fields)
                    if changed_teacher_fields:
                        teacher.save(update_fields=changed_teacher_fields)

                    # ── Signature: class teachers only, validated server-side ──
                    # Saved on its OWN update_fields path so a demographics
                    # failure can never wipe a valid signature upload (and
                    # vice versa).
                    sig_upload = request.FILES.get('signature')
                    sig_saved = False
                    if sig_upload:
                        if not (teacher.assigned_task or '').startswith('Class Teacher'):
                            messages.warning(request,
                                "A signature can only be set for a class teacher — the upload was not saved.")
                        else:
                            sig_error = validate_signature_upload(sig_upload)
                            if sig_error:
                                messages.error(request, sig_error)
                            else:
                                old_sig = teacher.signature.name if teacher.signature else None
                                teacher.signature = polish_signature_upload(sig_upload, teacher.pk)
                                try:
                                    teacher.save(update_fields=['signature'])
                                except Exception:
                                    import logging
                                    logging.getLogger(__name__).exception(
                                        "Signature save failed for teacher_id=%s", teacher.pk)
                                    messages.error(request, "Could not save the signature image. Please try again.")
                                    teacher.refresh_from_db()
                                else:
                                    sig_saved = True
                                    if old_sig and teacher.signature and teacher.signature.name != old_sig:
                                        discard_media_file(old_sig)
                                    try:
                                        bump_report_cache_version(teacher.school_id)
                                    except Exception:
                                        pass

                clear_teacher_cache(teacher.user_id)
                if changed_teacher_fields or changed_user_fields:
                    messages.success(request, f"Demographics updated for {teacher.get_full_title()}.")
                elif sig_saved:
                    messages.success(request, f"Signature updated for {teacher.get_full_title()}.")
                else:
                    messages.success(request, "No changes to save.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Profile update failed for teacher_id=%s", request.POST.get('teacher_id'))
                messages.error(request, "An error occurred while updating the profile. Please try again.")
            return redirect('manage_faculty_matrix')

        # --- Delete teacher profile (hard-delete with audit trail) ---
        elif action_type == 'delete_profile':
            school = get_request_school(request)
            teacher_id = request.POST.get('teacher_id')
            try:
                with transaction.atomic():
                    teacher = Teacher.all_objects.select_related('user').get(id=teacher_id, school=school)
                    name = teacher.get_full_title()
                    user_id = teacher.user_id
                    user_username = teacher.user.username

                    # 1. Deactivate + unlink all subject assignments (preserves marks data)
                    assignments_to_fix = SubjectAssignment.all_objects.filter(
                        teacher_profile=teacher
                    )
                    deactivated = assignments_to_fix.update(is_active=False)
                    assignments_to_fix.update(teacher_profile=None)

                    # 2. Unlink mark submissions too
                    from ..models import MarkSubmission
                    MarkSubmission.all_objects.filter(
                        teacher=teacher
                    ).update(teacher=None)

                    # 3. Log to SecurityAuditLog before deletion
                    from students.models import SecurityAuditLog
                    SecurityAuditLog.objects.create(
                        actor=request.user,
                        client_ip=request.META.get('REMOTE_ADDR'),
                        action='delete',
                        target_model='students.teacher',
                        target_id=str(teacher.pk),
                        target_fields=['user', 'teacher_profile'],
                        old_values={
                            'teacher_id': teacher.pk,
                            'user_id': user_id,
                            'username': user_username,
                            'name': name,
                            'subjects_taught': teacher.subjects_taught or '',
                            'school_section': teacher.school_section,
                            'sub_section': teacher.sub_section or '',
                            'deactivated_assignments': deactivated,
                        },
                        new_values={},
                        school_id_snapshot=school.pk,
                    )

                    # 4. Hard-delete the User account (CASCADE removes Teacher record)
                    teacher.user.delete()

                messages.success(request, f"'{name}' and their login account were permanently deleted. {deactivated} subject assignment(s) deactivated.")
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Profile deletion failed for teacher_id=%s", teacher_id)
                messages.error(request, "An error occurred while deleting the profile. Please try again.")
            return redirect('manage_faculty_matrix')

        # --- Reassign teacher to a different section/sub-section ---
        elif action_type == 'reassign_section':
            school = get_request_school(request)
            try:
                import re
                from django.core.cache import cache
                with transaction.atomic():
                    teacher = Teacher.all_objects.get(id=request.POST.get('teacher_id'), school=school)
                    new_section_token = (request.POST.get('new_section') or '').strip()

                    section_map = {
                        'LOWER_PRIMARY': ('PRIMARY', 'LOWER'),
                        'PRIMARY':       ('PRIMARY', 'UPPER'),
                        'JSS':           ('JSS', None),
                    }
                    if new_section_token not in section_map:
                        messages.error(request, "Invalid target section.")
                        return redirect('manage_faculty_matrix')

                    new_db_section, new_db_sub = section_map[new_section_token]
                    old_label = teacher.get_full_title()

                    if teacher.school_section == new_db_section and teacher.sub_section == new_db_sub:
                        messages.info(request, f"{old_label} is already in that section.")
                        return redirect('manage_faculty_matrix')

                    task_changed = False
                    displaced_teacher_name = None
                    if teacher.assigned_task and teacher.assigned_task.startswith('Class Teacher'):
                        m = re.search(r'Grade\s+(\d+)', teacher.assigned_task)
                        if m:
                            grade_num = int(m.group(1))
                            if new_db_section == 'PRIMARY' and new_db_sub == 'LOWER':
                                valid = grade_num in (1, 2, 3)
                            elif new_db_section == 'PRIMARY' and new_db_sub == 'UPPER':
                                valid = grade_num in (4, 5, 6)
                            elif new_db_section == 'JSS':
                                valid = grade_num in (7, 8, 9)
                            else:
                                valid = False

                            if not valid:
                                teacher.assigned_task = 'Teacher'
                                task_changed = True
                            else:
                                old_task = teacher.assigned_task
                                conflict = Teacher.all_objects.filter(
                                    school=school,
                                    is_active=True,
                                    assigned_task=old_task,
                                ).exclude(pk=teacher.pk).first()
                                if conflict:
                                    displaced_teacher_name = conflict.get_full_title()
                                    conflict.assigned_task = 'Teacher'
                                    conflict.save(update_fields=['assigned_task'])
                                    cache.delete(f"ct_scope:{school.pk}:{conflict.pk}")

                    teacher.school_section = new_db_section
                    teacher.sub_section    = new_db_sub
                    teacher.save()

                    SubjectAssignment.objects.filter(
                        school=school, teacher_profile=teacher
                    ).update(
                        school_section=new_db_section,
                        sub_section=new_db_sub,
                    )

                    _refresh_teacher_summary(teacher)

                clear_teacher_cache(teacher.user_id)
                try:
                    cache.delete(f"user_school_section:{teacher.user_id}")
                    if task_changed:
                        cache.delete(f"ct_scope:{school.pk}:{teacher.pk}")
                except Exception:
                    pass

                new_label = (
                    'Lower Primary' if new_section_token == 'LOWER_PRIMARY'
                    else 'Upper Primary' if new_section_token == 'PRIMARY'
                    else 'Junior Secondary'
                )
                msg = f"{old_label} has been reassigned to {new_label}."
                if task_changed:
                    msg += " Class teacher designation was cleared (grade not in new section)."
                elif displaced_teacher_name:
                    msg += f" {displaced_teacher_name} was replaced as class teacher for the same grade."
                else:
                    msg += " Their account will reflect the change on next page load."
                messages.success(request, msg)
            except Teacher.DoesNotExist:
                messages.error(request, "Teacher record not found.")
            except Exception:
                import logging
                logger = logging.getLogger(__name__)
                logger.exception("Section reassignment failed for teacher_id=%s", request.POST.get('teacher_id'))
                messages.error(request, "An error occurred while reassigning the teacher. Please try again.")
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

            # Always derive section/sub from the GRADE (not the workspace token),
            # so Grade 5 is never written as LOWER just because the admin was
            # in the Lower Primary workspace when they assigned.
            _, sub_section = section_for_class(grade)
            db_school_section = 'PRIMARY' if section in ('LOWER_PRIMARY', 'PRIMARY') else 'JSS'
            if sub_section is not None or section in ('LOWER_PRIMARY', 'PRIMARY'):
                db_school_section = 'PRIMARY'
            if section == 'JSS':
                db_school_section = 'JSS'
                sub_section = None

            # all_objects: scoped manager hides LOWER rows in UPPER workspace
            # and was skipping/creating wrong rows for Grade 1-3.
            SubjectAssignment.all_objects.update_or_create(
                school=school,
                class_name=grade,
                stream=stream,
                subject=subject,
                defaults={
                    'teacher_profile_id': teacher_id,
                    'school_section': db_school_section,
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
    assignments = SubjectAssignment.all_objects.filter(school=school, is_active=True).select_related('teacher_profile__user').all()

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
        SubjectAssignment.all_objects.filter(school=school, is_active=True)
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
            level_value, points_value = get_performance_level(
                mark.score,
                subject_id=mark.subject_id,
                section=student.school_section,
                sub_section=student.sub_section,
                school=student.school,
            )
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
    filename = safe_pdf_filename('Teachers_List', school.code, datetime.date.today().year)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required(login_url='login')
def teacher_classes(request, teacher_id):
    """Show performance cards: one per subject assignment (previous exam), class teacher class last."""
    import datetime as _dt
    from django.db.models import Avg
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
        school=school, teacher_profile=teacher, is_active=True
    ).select_related('subject').order_by('class_name', 'stream', 'subject__code')

    # Get active exams per section for fallback labels
    now = _dt.date.today()
    active_exams = {}
    for section_key in ('JSS', 'PRIMARY'):
        exams = Exam.all_objects.filter(
            school=school, school_section=section_key, year=now.year, status='active', is_deleted=False
        ).order_by('-created_at')
        if exams.exists():
            active_exams[section_key] = exams.first()

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

    # Build one card per subject assignment
    subject_cards = []
    for a in assignments:
        if not a.subject:
            continue
        class_name = a.class_name
        stream = a.stream
        section_key = 'JSS' if class_name in ('Grade 7', 'Grade 8', 'Grade 9') else 'PRIMARY'

        # Base filter for this class/stream
        base_filter = dict(
            school=school,
            student__class_name=class_name,
            student__stream=stream,
            student__is_active=True,
        )

        # Find the most recent mark to determine which exam to show
        latest_mark = Mark.all_objects.filter(
            subject=a.subject, **base_filter
        ).order_by('-year', '-id').first()

        # Fallback: try without subject filter in case subject FK differs
        if not latest_mark:
            latest_mark = Mark.all_objects.filter(**base_filter).order_by('-year', '-id').first()

        if latest_mark:
            exam_term = latest_mark.term
            exam_year = latest_mark.year
            exam_label = (latest_mark.exam_type or '').upper()
            mark_qs = Mark.all_objects.filter(
                subject=a.subject, **base_filter,
                term=exam_term, year=exam_year, exam_type=latest_mark.exam_type,
            )
            # Fallback: if subject filter yields nothing, try without
            if not mark_qs.exists():
                mark_qs = Mark.all_objects.filter(
                    **base_filter,
                    term=exam_term, year=exam_year, exam_type=latest_mark.exam_type,
                )
        else:
            # No marks — fall back to active exam label
            exam_term = ''
            exam_year = ''
            fallback = active_exams.get(section_key)
            exam_label = fallback.name.upper() if fallback else '—'
            mark_qs = Mark.all_objects.none()

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
            'exam_name': exam_label,
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
