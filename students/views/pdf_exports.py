"""
PDF export views for broadsheet results and class list registers.

Uses Playwright (headless Chromium) to render Django templates to PDF,
applying screen-emulated CSS overrides so the output matches the web view.
"""

import base64
import datetime
import logging
import mimetypes
import traceback

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.text import slugify
from playwright.sync_api import sync_playwright

from .constants import GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, LOWER_PRIMARY_SUBJECT_SHORT_MAP, ORDERED_LEVELS, PRIMARY_PERF_LEVELS, PRIMARY_SUBJECT_SHORT_MAP, SUBJECT_SHORT_MAP, get_streams_for_school, sort_subjects
from .reports import PRIMARY_ORDERED_LEVELS
from .exams import _get_primary_performance
from .helpers import (
    calculate_broadsheet_plv,
    calculate_primary_plv,
    get_class_teacher_scope,
    get_learner_contexts_for_user,
    get_performance_level,
    get_published_contexts_for_user,
    get_published_subject_codes,
    get_selected_context,
    get_teacher_for_user,
)
from ..models import Mark, Student, SubjectAssignment
from ..security import get_request_school, get_request_school_section, rate_limit, user_has_main_school_admin_override

logger = logging.getLogger('pdf_export')


def _embed_logo_base64(template_html, request):
    """Replace the school logo <img> src with a base64 data URI for PDF reliability."""
    try:
        school_logo = getattr(getattr(request, "school", None), "logo", None)
        if school_logo:
            logo_url = school_logo.url
            logo_type = mimetypes.guess_type(logo_url)[0] or "image/png"
            with school_logo.open("rb") as logo_file:
                logo_data = base64.b64encode(logo_file.read()).decode("ascii")
            template_html = template_html.replace(
                f'src="{logo_url}"',
                f'src="data:{logo_type};base64,{logo_data}"',
                1,
            )
    except Exception:
        logger.warning("Failed to embed school logo as base64", exc_info=True)
    return template_html


# ==============================================================================
# download_broadsheet_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60)
def download_broadsheet_pdf(request):
    """
    Renders the real results_list.html, injects PDF overrides, and hands it
    to Playwright with emulate_media('screen') so the screen styles win —
    giving a PDF that looks exactly like the web view.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    # ── 1. Rebuild exact same data context as results_list ────────────────────
    published_contexts = get_published_contexts_for_user(request.user)
    selected_context   = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    year      = str(selected_context["year"])   if selected_context else None
    term      = selected_context["term"]         if selected_context else None
    grade     = selected_context["class_name"]   if selected_context else None
    stream    = selected_context["stream"]        if selected_context else None
    exam_type = selected_context["exam_name"]     if selected_context else None

    # ── Determine workspace section early for primary-aware grading ─────────
    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary
    if is_lower_primary:
        subject_map = LOWER_PRIMARY_SUBJECT_SHORT_MAP
    elif is_primary:
        subject_map = PRIMARY_SUBJECT_SHORT_MAP
    else:
        subject_map = SUBJECT_SHORT_MAP
    subject_codes = list(subject_map.keys())
    active_levels = PRIMARY_PERF_LEVELS if is_primary else ORDERED_LEVELS

    analysis_data = {
        short: {
            'entries': 0, 'total_score': 0, 'mean_score': 0.0,
            'distribution': {lvl: 0 for lvl in active_levels},
            'teacher_name': '—',
        }
        for short in subject_map.values()
    }

    broadsheet              = []
    published_subject_count = 0
    student_count           = 0
    published_subjects      = []

    if year and term and grade and stream and exam_type:
        published_subject_codes = get_published_subject_codes(grade, stream, year, term, exam_type)
        published_subject_count = len(published_subject_codes)
        from ..models import Subject
        published_subjects_qs = Subject.objects.filter(school=school, code__in=published_subject_codes)

        # Always show ALL subjects as columns (even without marks yet).
        subject_label_map = {
            s.code: (subject_map.get(s.code) or s.name or s.code)
            for s in published_subjects_qs
        }
        published_subjects = sort_subjects([
            (code, subject_label_map.get(code, subject_map.get(code, code)))
            for code in published_subject_codes
        ])
        for _code, short in published_subjects:
            analysis_data.setdefault(short, {
                'entries': 0, 'total_score': 0, 'mean_score': 0.0,
                'distribution': {lvl: 0 for lvl in active_levels},
                'teacher_name': '—',
            })

        for a in SubjectAssignment.objects.filter(
            school=school, class_name=grade, stream=stream
        ).select_related('teacher_profile__user', 'subject'):
            code = a.subject.code if a.subject else None
            if code:
                short = subject_label_map.get(code, subject_map.get(code, code))
                analysis_data.setdefault(short, {
                    'entries': 0, 'total_score': 0, 'mean_score': 0.0,
                    'distribution': {lvl: 0 for lvl in active_levels},
                    'teacher_name': '—',
                })
                analysis_data[short]['teacher_name'] = a.teacher_profile.get_full_title()

        marks_prefetch = Prefetch(
            'marks',
            queryset=Mark.objects.filter(
                school=school,
                year=year, term=term, exam_type=exam_type,
                subject__in=published_subjects_qs,
            ).order_by('subject', '-date_recorded', '-id'),
            to_attr='cached_marks',
        )
        students      = Student.objects.filter(school=school, class_name=grade, stream=stream).prefetch_related(marks_prefetch)
        student_count = students.count()

        for student in students:
            marks_dict   = {}
            for mark in student.cached_marks:
                marks_dict.setdefault(mark.subject.code, mark)
            row_scores   = []
            total_marks  = 0
            total_points = 0
            assessed_subjects = 0

            for code, short in published_subjects:
                m = marks_dict.get(code)
                if m and m.score is not None:
                    if m.is_absent:
                        row_scores.append({'score': 'AB', 'level': 'AB'})
                    else:
                        level, points = _get_primary_performance(m.score) if is_primary else get_performance_level(m.score)
                        row_scores.append({'score': m.score, 'level': level})
                        total_marks  += m.score
                        total_points += points
                        assessed_subjects += 1
                    if not m.is_absent:
                        analysis_data[short]['entries']     += 1
                        analysis_data[short]['total_score'] += m.score
                        if level in analysis_data[short]['distribution']:
                            analysis_data[short]['distribution'][level] += 1
                else:
                    row_scores.append({'score': '-', 'level': '-'})

            broadsheet.append({
                'student': student,
                'scores':  row_scores,
                'tps':     total_points,
                'total':   total_marks,
                'plv':     calculate_primary_plv(total_marks, assessed_subjects) if is_primary else calculate_broadsheet_plv(total_marks, total_points),
            })

        broadsheet.sort(key=lambda x: (-x['total'], -x['tps']))

        for short, data in analysis_data.items():
            if data['entries'] > 0:
                data['mean_score'] = round(data['total_score'] / data['entries'], 2)

        # Build ordered analysis rows for only published subjects, in display order
        analysis_rows = [
            {'short': short, **analysis_data[short]} for code, short in published_subjects
        ]
    else:
        analysis_rows = []

    # ── 2. Render the actual template ──────────────────────────────────────────
    template_name = 'students/results_list_primary.html' if is_primary else 'students/results_list.html'

    template_html = render_to_string(template_name, {
        'broadsheet':              broadsheet,
        'analysis_data':           analysis_data,
        'analysis_rows':           analysis_rows,
        'ordered_levels':          active_levels,
        'show_table':              True,
        'selected_year':           year,
        'selected_term':           term,
        'selected_exam':           exam_type,
        'selected_grade':          grade,
        'selected_stream':         stream,
        'selected_context_key':    selected_context["context_key"] if selected_context else "",
        'published_contexts':      published_contexts,
        'published_subjects':      published_subjects,
        'published_subject_count': published_subject_count,
        'student_count':           student_count,
        'is_admin_view':           user_has_main_school_admin_override(request.user),
        'access_label':            'Official Results Export',
    }, request=request)

    # Embed the school logo for PDF export so it prints reliably even when
    # Playwright is rendering HTML outside the normal browser page.
    template_html = _embed_logo_base64(template_html, request)

    # ── 3. Minimal PDF overlay CSS ────────────────────────────────────────────
    #
    # Playwright now uses emulate_media("print") so the template's own
    # @media print CSS does all the heavy lifting (table styling, colors,
    # fonts, page-break, @page rules). We only inject CSS here to hide
    # screen-only chrome that the template's print CSS doesn't cover.
    #
    pdf_css = """
<style id="pdf-override">
  /* Force background colours to print (Chromium blocks them by default) */
  * {
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }

  /* Hide all screen-only chrome */
  .sidebar,
  .sidebar-overlay,
  nav,
  header,
  .mobile-topbar,
  .hamburger-btn,
  .global-loader-overlay,
  .official-results-hero,
  .d-print-none,
  .published-switcher,
  .exam-groups-wrapper,
  .empty-official-state,
  .btn-print-action,
  .topbar,
  .topbar-right,
  .topbar-user,
  .topbar-avatar,
  .topbar-username,
  .topbar-chevron,
  .topbar-dropdown,
  .topbar-spacer,
  .workspace-toggle {
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    overflow: hidden !important;
  }

  /* Kill sidebar layout offset so broadsheet fills full page width */
  .main-content {
    margin-left: 0 !important;
    padding-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
  }
  body > .sidebar ~ .main-content {
    margin-left: 0 !important;
  }
  html, body {
    overflow: visible !important;
  }
</style>
"""

    # Give Playwright a real origin so relative media/static URLs load in PDFs.
    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'

    # Insert overrides just before </head>
    if '</head>' in template_html:
        patched_html = template_html.replace('</head>', pdf_base_tag + pdf_css + '</head>', 1)
    else:
        patched_html = pdf_base_tag + pdf_css + template_html

    # ── 4. Playwright — PRINT media so template's @media print CSS activates ──
    #
    # Run Playwright in a dedicated thread with its own event loop to avoid
    # conflicts with Django's dev server auto-reloader on Windows (Python 3.13+).
    #
    import threading

    _playwright_result = {}
    _playwright_error = [None]

    def _generate_pdf():
        try:
            import asyncio, sys
            if sys.platform == 'win32':
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                pg      = browser.new_page()
                pg.set_viewport_size({"width": 1200, "height": 900})
                pg.emulate_media(media="print")
                pg.set_content(patched_html, wait_until="networkidle")
                pg.wait_for_function("document.fonts.ready")
                pg.wait_for_timeout(500)

                pdf_bytes = pg.pdf(
                    format="A4",
                    landscape=True,
                    print_background=True,
                    display_header_footer=False,
                    margin={
                        "top":    "0.5in",
                        "right":  "0.3in",
                        "bottom": "0.5in",
                        "left":   "0.3in",
                    },
                )
                browser.close()
                _playwright_result['pdf'] = pdf_bytes
        except Exception as e:
            _playwright_error[0] = e

    t = threading.Thread(target=_generate_pdf, daemon=True)
    t.start()
    t.join(timeout=90)

    if _playwright_error[0] is not None:
        e = _playwright_error[0]
        tb_str = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
        logger.error('PDF generation failed: %s\n%s', str(e), tb_str)
        messages.error(request, "PDF generation failed. Please try again.")
        return redirect('results_list')

    if 'pdf' not in _playwright_result:
        logger.error('PDF generation timed out after 90 seconds')
        messages.error(request, "PDF generation timed out. Please try again.")
        return redirect('results_list')

    pdf_bytes = _playwright_result['pdf']

    # ── 5. Return as download ──────────────────────────────────────────────────
    slug_grade  = slugify(grade  or "class")
    slug_stream = slugify(stream or "stream")
    current_year = datetime.date.today().year
    filename    = f"{slug_grade}_{slug_stream}_Premium_Results_List_{year or current_year}.pdf"

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ==============================================================================
# download_classlist_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60)
def download_classlist_pdf(request):
    """
    Renders the class_lists register sheet and converts it to a
    high-quality PDF using Playwright (same approach as broadsheet).
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    section = get_request_school_section(request)

    section_colors = {
        'JSS':           '#3A6AD8',
        'PRIMARY':       '#047857',
        'LOWER_PRIMARY': '#B45309',
    }
    section_accent = section_colors.get(section, '#3A6AD8')

    # Reuse the same context-building logic as class_lists view
    view_mode = request.GET.get('view_mode', 'teacher')
    if view_mode not in ('teacher', 'admin'):
        view_mode = 'teacher'

    teacher = get_teacher_for_user(request.user)
    class_teacher_scope = get_class_teacher_scope(teacher)
    is_admin_view = user_has_main_school_admin_override(request.user)
    contexts = get_learner_contexts_for_user(request.user)

    selected_key = request.GET.get('context')
    selected_context = None
    if selected_key:
        selected_context = next((item for item in contexts if item['context_key'] == selected_key), None)
    if not selected_context and contexts:
        selected_context = contexts[0]

    selected_grade = selected_context['class_name'] if selected_context else None
    selected_stream = selected_context['stream'] if selected_context else None
    can_access_admin_register = (
        is_admin_view or
        (class_teacher_scope == (selected_grade, selected_stream))
    )
    if view_mode == 'admin' and not can_access_admin_register:
        view_mode = 'teacher'

    students = Student.objects.none()
    if selected_context:
        student_manager = Student.all_objects if is_admin_view else Student.objects
        students = (
            student_manager
            .filter(school=school, class_name=selected_grade, stream=selected_stream)
            .filter(admission_no__regex=r'^[0-9]+$')
            .select_related('guardian')
            .annotate(adm_int=Cast('admission_no', IntegerField()))
            .order_by('adm_int')
        )

    template_html = render_to_string('students/class_lists.html', {
        'students':              students,
        'selected_grade':        selected_grade,
        'selected_stream':       selected_stream,
        'selected_context_key':  selected_context['context_key'] if selected_context else '',
        'learner_contexts':      contexts,
        'current_view_mode':     view_mode,
        'can_access_admin_register': can_access_admin_register,
        'is_admin_view':         is_admin_view,
        'access_label':          'PDF Export',
        'section_accent':        section_accent,
        'grades':                GRADE_CHOICES,
        'streams':               get_streams_for_school(school, section),
    }, request=request)

    template_html = _embed_logo_base64(template_html, request)

    pdf_css = """
<style id="pdf-override">
  * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }

  html, body {
    margin: 0 !important; padding: 0 !important;
    background: #ffffff !important;
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important; color: #000 !important;
    width: 100% !important;
    overflow: visible !important;
  }

  /* Hide all screen chrome */
  .sidebar, nav, header, .hamburger-btn, .sidebar-overlay,
  .directory-hero, .summary-grid, .toolbar, .mode-tabs,
  .no-print, .context-strip, .access-pill, .empty-state {
    display: none !important;
    visibility: hidden !important;
  }

  /* Kill sidebar layout offset */
  body > *, .main-content, main, [class*="content"], [class*="wrapper"] {
    margin-left: 0 !important;
    padding-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
    transform: none !important;
    position: static !important;
  }

  .directory-page {
    padding: 0 !important;
    background: #ffffff !important;
    min-height: unset !important;
    width: 100% !important;
  }

  .register-sheet {
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
    max-height: none !important;
    height: auto !important;
    background: #ffffff !important;
  }

  .sheet-heading {
    text-align: left;
    margin-bottom: 10pt !important;
  }

  .sheet-letterhead {
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    gap: 18px !important;
    margin-bottom: 8pt !important;
    padding-bottom: 8pt !important;
    border-bottom: 4px solid #9be816 !important;
  }

  .sheet-logo {
    height: 122px !important;
    width: 122px !important;
    object-fit: contain !important;
    flex: 0 0 122px !important;
  }

  .sheet-heading-copy {
    min-width: 0 !important;
  }

  .sheet-heading h2 {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 25pt !important;
    font-weight: 900 !important;
    line-height: 1 !important;
    color: #000 !important;
    text-transform: uppercase !important;
    margin: 0 0 6pt !important;
  }

  .sheet-heading p {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important;
    color: #111 !important;
    margin: 0 !important;
    text-transform: uppercase !important;
  }

  .register-table {
    width: 100% !important;
    min-width: 0 !important;
    border-collapse: collapse !important;
    border: 1.5px solid #000 !important;
    background: #ffffff !important;
    table-layout: fixed !important;
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important;
  }

  .register-table th {
    background: #f2f2f2 !important;
    color: #000 !important;
    font-weight: 700 !important;
    font-size: 12pt !important;
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    text-align: left !important;
    line-height: 1.05 !important;
  }

  .register-table td {
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    color: #000 !important;
    font-weight: 400 !important;
    font-size: 12pt !important;
    line-height: 1.05 !important;
    vertical-align: middle !important;
  }

  .teacher-register th:nth-child(1),
  .teacher-register td:nth-child(1) { width: 8% !important; }
  .teacher-register th:nth-child(2),
  .teacher-register td:nth-child(2) { width: 23% !important; }
  .teacher-register th:nth-child(3),
  .teacher-register td:nth-child(3) { width: 14% !important; }
  .teacher-register th:nth-child(n+4),
  .teacher-register td:nth-child(n+4) {
    width: 3.6% !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
  }

  .register-table a {
    color: #000 !important;
    text-decoration: none !important;
  }

  .register-table tr {
    page-break-inside: avoid !important;
    break-inside: avoid !important;
  }

  .grid-cell {
    width: 24px !important;
    height: 21pt !important;
    background: #ffffff !important;
  }

  .print-watermark-footer {
    display: none !important;
    visibility: hidden !important;
  }

  @page {
    margin: 0.62in 0.38in 0.72in 0.5in;
    @bottom-center {
      content: "GENERATED FROM EDUNEXUS EXAM SYSTEM @2026";
      font-family: "Times New Roman", Times, serif;
      font-size: 10pt;
      font-weight: 700;
      color: rgba(0, 0, 0, 0.55);
      text-transform: uppercase;
      letter-spacing: 0.4pt;
    }
  }
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'

    if '</head>' in template_html:
        patched_html = template_html.replace('</head>', pdf_base_tag + pdf_css + '</head>', 1)
    else:
        patched_html = pdf_base_tag + pdf_css + template_html

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            pg = browser.new_page()
            pg.set_viewport_size({"width": 794, "height": 1123})
            pg.emulate_media(media="print")
            pg.set_content(patched_html, wait_until="networkidle")
            pg.wait_for_function("""
                () => {
                    const logo = document.querySelector('.sheet-logo');
                    return !logo || (logo.complete && logo.naturalWidth > 0);
                }
            """, timeout=5000)

            pdf_bytes = pg.pdf(
                format="A4",
                landscape=False,
                print_background=True,
                display_header_footer=False,
                margin={
                    "top":    "0.62in",
                    "right":  "0.38in",
                    "bottom": "0.72in",
                    "left":   "0.5in",
                },
            )
            browser.close()
    except Exception as e:
        logger.error('Class list PDF generation failed: %s\n%s', str(e), traceback.format_exc())
        messages.error(request, "PDF generation failed. Please try again.")
        return redirect('class_lists')

    slug_grade  = slugify(selected_grade  or "class")
    slug_stream = slugify(selected_stream or "stream")
    year = datetime.date.today().year
    filename = f"{slug_grade}_{slug_stream}_Class_List_{year}.pdf"

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
