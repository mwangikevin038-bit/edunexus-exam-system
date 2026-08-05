"""
PDF export views for broadsheet results and class list registers.

Uses WeasyPrint to render Django templates to PDF,
applying screen-emulated CSS overrides so the output matches the web view.
"""

import base64
import datetime
import io
import json
import logging
import mimetypes
import traceback

import matplotlib
matplotlib.use('Agg')  # Thread-safe headless backend for local django servers
import matplotlib.pyplot as plt
import numpy as np

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Prefetch, Q, Sum, IntegerField
from django.db.models.functions import Cast, Length, Substr
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.text import slugify
from pypdf import PdfWriter
from weasyprint import HTML

from .constants import ASSESSMENT_MAP, GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, LOWER_PRIMARY_SUBJECT_NAMES, LOWER_PRIMARY_SUBJECT_SHORT_MAP, ORDERED_LEVELS, PRIMARY_PERF_LEVELS, PRIMARY_SUBJECT_NAMES, PRIMARY_SUBJECT_SHORT_MAP, SUBJECT_DISPLAY_ORDER, SUBJECT_SHORT_MAP, get_streams_for_school, sort_subjects
from .reports import PRIMARY_ORDERED_LEVELS, _grading_config_for
from .exams import _get_primary_performance
from .helpers import (
    calculate_broadsheet_plv,
    calculate_primary_plv,
    calculate_report_plv,
    get_cached_class_averages,
    get_class_leaderboard,
    get_class_teacher_scope,
    get_learner_contexts_for_user,
    get_performance_level,
    get_published_contexts_for_user,
    get_published_subject_codes,
    get_selected_context,
    get_teacher_for_user,
    user_can_access_class_stream,
)
from ..models import ClassTeacherMasterComment, Mark, SchoolHeadteacherComment, Student, Subject, SubjectAssignment, Teacher
from ..security import get_request_school, get_request_school_section, get_school_object_or_403, rate_limit, user_has_main_school_admin_override

logger = logging.getLogger('pdf_export')


def generate_python_chart_base64(labels, student_scores, class_averages):
    if not labels:
        return ""
    try:
        fig, ax = plt.subplots(figsize=(5.5, 1.4))
        x = np.arange(len(labels))

        # Attempt smooth cubic spline interpolation for browser-matching curves
        try:
            from scipy.interpolate import make_interp_spline
            x_smooth = np.linspace(x.min(), x.max(), 200)
            spline_student = make_interp_spline(x, student_scores, k=3)
            student_smooth = np.clip(spline_student(x_smooth), 0, 100)
            spline_class = make_interp_spline(x, class_averages, k=3)
            class_smooth = np.clip(spline_class(x_smooth), 0, 100)
            ax.plot(x_smooth, student_smooth, color='#4f46e5', linewidth=2, label='Student', zorder=3)
            ax.plot(x_smooth, class_smooth, color='#94a3b8', linewidth=1.5, linestyle='--', label='Class Avg', zorder=2)
        except ImportError:
            ax.plot(x, student_scores, marker='o', color='#4f46e5', linewidth=2, label='Student', zorder=3)
            ax.plot(x, class_averages, marker='s', linestyle='--', color='#94a3b8', linewidth=1.5, label='Class Avg', zorder=2)
            x_smooth = x
            student_smooth = student_scores
            class_smooth = class_averages

        # Filled gradient under student line
        ax.fill_between(x_smooth, student_smooth, color='#4f46e5', alpha=0.15, zorder=1)

        # Marker dots on original data points
        ax.scatter(x, student_scores, color='#4f46e5', edgecolors='white', s=30, zorder=4)
        ax.scatter(x, class_averages, color='#94a3b8', s=20, marker='s', zorder=4)

        # Premium theme
        ax.grid(True, linestyle=':', alpha=0.5, color='#cbd5e1')
        ax.set_ylabel('Marks', fontsize=8, fontweight='bold', color='#1e293b')
        ax.set_ylim(0, 105)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=12, ha='right', fontsize=6.5, color='#475569')
        ax.legend(loc='upper right', fontsize=6.5, framealpha=0.8, edgecolor='#e2e8f0')
        for spine in ['top', 'right']:
            ax.spines[spine].set_visible(False)
        ax.spines['left'].set_color('#e2e8f0')
        ax.spines['bottom'].set_color('#e2e8f0')

        plt.tight_layout(pad=0.1)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=160, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"
    except Exception:
        return ""


def _log_pdf_error(view_name, error, context=None):
    """
    Log PDF generation errors with full traceback to server_err.log.
    Includes view name, error type, message, and optional context.
    """
    tb = traceback.format_exc()
    context_str = ""
    if context:
        context_str = "\n  Context: " + " | ".join(f"{k}={v}" for k, v in context.items())

    logger.error(
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
        "PDF GENERATION ERROR — %s\n"
        "═══════════════════════════════════════════════════════════════\n"
        "View: %s\n"
        "Error Type: %s\n"
        "Error Message: %s%s\n"
        "Full Traceback:\n%s\n"
        "═══════════════════════════════════════════════════════════════\n",
        view_name,
        view_name,
        type(error).__name__,
        str(error),
        context_str,
        tb,
    )


# ==============================================================================
# WEASYPRESS PDF GENERATION
# ==============================================================================

def _generate_pdf(patched_html, *, landscape=False, margin=None, **kwargs):
    """Generates PDF directly from HTML string using WeasyPrint in-memory compilation"""
    try:
        # Create WeasyPrint HTML document instance directly from string
        html_doc = HTML(string=patched_html)

        # Write the PDF directly to bytes memory
        pdf_bytes = html_doc.write_pdf()
        return {'pdf': pdf_bytes}
    except Exception as e:
        logger.error(f"[pdf] WeasyPrint generation failed: {str(e)}")
        return {'pdf': None, 'error': str(e)}


def _inject_pdf_css(template_html, pdf_css, base_tag):
    """
    Bulletproof CSS injection — never relies on loose string replacement.

    Strategy:
    1. Try inserting before </head> (standard HTML)
    2. Try inserting before </body> (fallback)
    3. Try inserting after <html> (last resort)
    4. Prepend to document (guaranteed to work)
    """
    css_block = base_tag + pdf_css

    # Strategy 1: Insert before </head>
    if '</head>' in template_html:
        return template_html.replace('</head>', css_block + '</head>', 1)

    # Strategy 2: Insert before </body>
    if '</body>' in template_html:
        return template_html.replace('</body>', css_block + '</body>', 1)

    # Strategy 3: Insert after <html>
    if '<html' in template_html:
        idx = template_html.index('<html') + len(template_html[template_html.index('<html'):].split('>')[0]) + 1
        return template_html[:idx] + css_block + template_html[idx:]

    # Strategy 4: Prepend (guaranteed)
    return css_block + template_html


def _embed_logo_base64(template_html, request):
    """Replace ALL school logo <img> src with a base64 data URI for PDF reliability."""
    try:
        school_logo = getattr(getattr(request, "school", None), "logo", None)
        if school_logo:
            logo_url = school_logo.url
            logo_type = mimetypes.guess_type(logo_url)[0] or "image/png"
            with school_logo.open("rb") as logo_file:
                logo_data = base64.b64encode(logo_file.read()).decode("ascii")
            data_uri = f'data:{logo_type};base64,{logo_data}'
            template_html = template_html.replace(f'src="{logo_url}"', f'src="{data_uri}"')
    except Exception:
        logger.warning("Failed to embed school logo as base64", exc_info=True)
    return template_html


# ==============================================================================
# download_broadsheet_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_broadsheet_pdf(request):
    """
    Renders the real results_list.html, injects PDF overrides, and hands it
    to Playwright with emulate_media('screen') so the screen styles win —
    giving a PDF that looks exactly like the web view.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    # ── Determine workspace section first ────────────────────────────────────
    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary

    active_sub = request.GET.get('sub', '').strip().upper()
    if is_lower_primary:
        active_sub = 'LOWER'
    elif is_primary and active_sub not in ('LOWER', 'UPPER'):
        active_sub = request.session.get('active_sub', 'UPPER')
    if is_primary and active_sub not in ('LOWER', 'UPPER'):
        active_sub = 'UPPER'

    # ── 1. Rebuild exact same data context as results_list ────────────────────
    published_contexts = get_published_contexts_for_user(request.user, sub_section=active_sub if is_primary else None)
    selected_context   = get_selected_context(request, published_contexts) if request.GET.get("context") else None

    if not selected_context and published_contexts:
        selected_context = published_contexts[0]

    year      = str(selected_context["year"])   if selected_context else None
    term      = selected_context["term"]         if selected_context else None
    grade     = selected_context["class_name"]   if selected_context else None
    stream    = selected_context["stream"]        if selected_context else None
    exam_type = selected_context["exam_name"]     if selected_context else None

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
        published_subject_codes = get_published_subject_codes(grade, stream, year, term, exam_type, sub_section=active_sub if is_primary else None)
        published_subject_count = len(published_subject_codes)
        from ..models import Subject
        published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

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

        for a in SubjectAssignment.all_objects.filter(
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
            queryset=Mark.all_objects.filter(
                school=school,
                year=year, term=term, exam_type=exam_type,
                subject__in=published_subjects_qs,
            ).order_by('subject', '-date_recorded', '-id'),
            to_attr='cached_marks',
        )
        students      = Student.all_objects.filter(school=school, class_name=grade, stream=stream, is_active=True).prefetch_related(marks_prefetch)
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
                        level, points = _get_primary_performance(m.score, school=school, section=section, sub_section=active_sub if is_primary else None) if is_primary else get_performance_level(m.score)
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
                'plv':     calculate_primary_plv(total_marks, assessed_subjects, sub_section=active_sub if is_primary else None, school=school, section=section) if is_primary else calculate_broadsheet_plv(total_marks, total_points),
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

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    section_accent = section_colors.get(section, '#305CDE')

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
        'section_accent':          section_accent,
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
  * {
    -webkit-print-color-adjust: exact !important;
    print-color-adjust: exact !important;
  }

  @page { size: A4 landscape; margin: 0.15in 0.15in; }

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

    # Insert overrides using bulletproof injector
    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    # ── 4. WeasyPrint — generate PDF directly from HTML ──
    try:
        pdf_data = _generate_pdf(patched_html, landscape=True)
    except Exception as e:
        _log_pdf_error('download_broadsheet_pdf', e, {
            'year': year, 'term': term, 'section': section,
            'grade': grade, 'stream': stream,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    # ── 5. Return as download or inline ────────────────────────────────────────
    if pdf_data.get('pdf'):
        slug_grade  = slugify(grade  or "class")
        slug_stream = slugify(stream or "stream")
        current_year = datetime.date.today().year
        filename    = f"{slug_grade}_{slug_stream}_Premium_Results_List_{year or current_year}.pdf"

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_classlist_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_classlist_pdf(request):
    """
    Renders the class_lists register sheet and converts it to a
    high-quality PDF using Playwright (same approach as broadsheet).
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    section = get_request_school_section(request)

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    section_accent = section_colors.get(section, '#305CDE')

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
            .filter(school=school, class_name=selected_grade, stream=selected_stream, is_active=True)
            .filter(admission_no__regex=r'^[0-9]+[PJ]$')
            .select_related('guardian')
            .annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
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

    pdf_css = f"""
<style id="pdf-override">
  * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}

  html, body {{
    margin: 0 !important; padding: 0 !important;
    background: #ffffff !important;
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important; color: #000 !important;
    width: 100% !important;
    overflow: visible !important;
  }}

  /* Hide all screen chrome */
  .sidebar, nav, header, .hamburger-btn, .sidebar-overlay,
  .directory-hero, .summary-grid, .toolbar, .mode-tabs,
  .no-print, .context-strip, .access-pill, .empty-state {{
    display: none !important;
    visibility: hidden !important;
  }}

  /* Kill sidebar layout offset */
  body > *, .main-content, main, [class*="content"], [class*="wrapper"] {{
    margin-left: 0 !important;
    padding-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
    transform: none !important;
    position: static !important;
  }}

  .directory-page {{
    padding: 0 !important;
    background: #ffffff !important;
    min-height: unset !important;
    width: 100% !important;
  }}

  .register-sheet {{
    border: none !important;
    box-shadow: none !important;
    border-radius: 0 !important;
    padding: 0 !important;
    overflow: visible !important;
    max-height: none !important;
    height: auto !important;
    background: #ffffff !important;
  }}

  .sheet-heading {{
    text-align: left;
    margin-bottom: 10pt !important;
  }}

  .sheet-letterhead {{
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    gap: 18px !important;
    margin-bottom: 8pt !important;
    padding-bottom: 8pt !important;
    border-bottom: 4px solid {section_accent} !important;
  }}

  .sheet-logo {{
    height: 122px !important;
    width: 122px !important;
    object-fit: contain !important;
    flex: 0 0 122px !important;
  }}

  .sheet-heading-copy {{
    min-width: 0 !important;
  }}

  .sheet-heading h2 {{
    font-family: "Times New Roman", Times, serif !important;
    font-size: 25pt !important;
    font-weight: 900 !important;
    line-height: 1 !important;
    color: #000 !important;
    text-transform: uppercase !important;
    margin: 0 0 6pt !important;
  }}

  .sheet-heading p {{
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important;
    color: #111 !important;
    margin: 0 !important;
    text-transform: uppercase !important;
  }}

  .register-table {{
    width: 100% !important;
    min-width: 0 !important;
    border-collapse: collapse !important;
    border: 1.5px solid #000 !important;
    background: #ffffff !important;
    table-layout: fixed !important;
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important;
  }}

  .register-table th {{
    background: #f2f2f2 !important;
    color: #000 !important;
    font-weight: 700 !important;
    font-size: 12pt !important;
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    text-align: left !important;
    line-height: 1.05 !important;
  }}

  .register-table td {{
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    color: #000 !important;
    font-weight: 400 !important;
    font-size: 12pt !important;
    line-height: 1.05 !important;
    vertical-align: middle !important;
  }}

  .teacher-register th:nth-child(1),
  .teacher-register td:nth-child(1) {{ width: 8% !important; }}
  .teacher-register th:nth-child(2),
  .teacher-register td:nth-child(2) {{ width: 23% !important; }}
  .teacher-register th:nth-child(3),
  .teacher-register td:nth-child(3) {{ width: 14% !important; }}
  .teacher-register th:nth-child(n+4),
  .teacher-register td:nth-child(n+4) {{
    width: 3.6% !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
  }}

  .register-table a {{
    color: #000 !important;
    text-decoration: none !important;
  }}

  .register-table tr {{
    page-break-inside: avoid !important;
    break-inside: avoid !important;
  }}

  .grid-cell {{
    width: 24px !important;
    height: 21pt !important;
    background: #ffffff !important;
  }}

  .print-watermark-footer {{
    display: none !important;
    visibility: hidden !important;
  }}

  @page {{
    margin: 0.62in 0.38in 0.72in 0.5in;
  }}
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'

    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    # ── WeasyPrint — generate PDF directly from HTML ──
    try:
        pdf_data = _generate_pdf(patched_html, landscape=False)
    except Exception as e:
        _log_pdf_error('download_classlist_pdf', e, {
            'grade': selected_grade, 'stream': selected_stream,
            'context': selected_key, 'view_mode': view_mode,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        slug_grade  = slugify(selected_grade  or "class")
        slug_stream = slugify(selected_stream or "stream")
        year = datetime.date.today().year
        filename = f"{slug_grade}_{slug_stream}_Class_List_{year}.pdf"

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_individual_report_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_individual_report_pdf(request, student_id):
    """
    Server-side PDF for individual report cards.
    Reuses the same data context as individual_report() and renders
    via Playwright with emulate_media('print') for high-quality output.
    """

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not student:
        return JsonResponse({'error': 'Student not found.'}, status=404)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        return JsonResponse({'error': 'You are not allowed to print report cards for this class stream.'}, status=403)

    year       = request.GET.get('year', datetime.date.today().year)
    term       = request.GET.get('term', 'Term 1')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    from .reports import _grading_config_for

    student_sub_section = 'LOWER' if student.class_name in LOWER_PRIMARY_GRADE_CHOICES else ('UPPER' if student.school_section == 'PRIMARY' else None)

    published_subject_codes = get_published_subject_codes(
        student.class_name, student.stream, year, term, db_assessment,
        sub_section=student_sub_section,
    )
    from ..models import Subject
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

    marks = Mark.all_objects.filter(
        school=school, student=student, year=year, term=term,
        exam_type=db_assessment, subject__in=published_subjects_qs,
        school_section=student.school_section,
    ).select_related('subject')

    totals = marks.aggregate(
        total_score=Sum('score'),
        total_pts=Sum('points'),
    )
    total_marks = totals['total_score'] or 0
    total_points = totals['total_pts'] or 0

    marks = sorted(marks, key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))

    leaderboard = get_class_leaderboard(
        school, student.class_name, student.stream,
        year, term, db_assessment, published_subjects_qs,
    )
    class_leaderboard_rank = {sid: rank for rank, sid in enumerate(leaderboard['sorted_ids'], 1)}
    class_count = leaderboard['class_count']
    position = class_leaderboard_rank.get(student.id, 0)

    is_lower_primary = student.school_section == 'PRIMARY' and student.sub_section == 'LOWER'
    is_primary = student.school_section == 'PRIMARY'
    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}

    teacher_map = {
        a.subject.code: a.teacher_profile.get_full_title()
        for a in SubjectAssignment.all_objects.filter(
            school=school, class_name=student.class_name, stream=student.stream
        ).select_related('teacher_profile__user', 'subject')
        if a.subject
    }

    from ..models import Teacher
    class_teacher_name = ""
    ct_q = Teacher.all_objects.filter(
        school=school, assigned_task__icontains=student.class_name,
    ).filter(
        Q(assigned_task__icontains=student.stream),
    ).select_related('user').first()
    if ct_q:
        class_teacher_name = ct_q.get_full_title()

    marks_list = list(marks)
    for mark in marks_list:
        mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
        mark.teacher_name = teacher_map.get(mark.subject.code, '\u2014')
        if is_primary and not mark.is_absent:
            pct = mark.score or 0
            mark.performance_level, mark.points = _get_primary_performance(pct)

    class_subject_avgs = (
        Mark.all_objects.filter(
            school=school,
            student__class_name=student.class_name, student__stream=student.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .exclude(is_absent=True)
        .values('subject__code')
        .annotate(avg_score=Avg('score'))
    )
    class_avg_map = {row['subject__code']: round(row['avg_score'], 1) for row in class_subject_avgs}

    for mark in marks_list:
        class_avg = class_avg_map.get(mark.subject.code)
        mark.class_average = class_avg
        if class_avg is not None and mark.score is not None and not mark.is_absent:
            mark.deviation = round(mark.score - class_avg, 1)
        else:
            mark.deviation = None

    grading_config = _grading_config_for(school, student.school_section, student.sub_section)
    grade_descriptors = grading_config.subject_scale if grading_config else []

    assessed_subjects   = sum(1 for m in marks_list if m.score is not None and not m.is_absent)
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))
    mean_points         = round(total_points / assessed_subjects, 1) if assessed_subjects else 0
    max_total_marks     = assessed_subjects * 100
    max_total_points    = assessed_subjects * max_points_per_subj

    chart_data_json = json.dumps({
        'labels':    [m.subject_name for m in marks_list if not m.is_absent],
        'student':   [m.score for m in marks_list if not m.is_absent],
        'class_avg': [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent],
    })

    # Generate server-side chart image for PDF rendering (WeasyPrint compatible)
    chart_labels = [m.subject_name for m in marks_list if not m.is_absent]
    chart_student = [m.score for m in marks_list if not m.is_absent]
    chart_class_avg = [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent]

    # Check Redis cache first before generating new chart
    chart_cache_key = f"student_chart_{student.id}_{year}_{term}"
    chart_base64_image = cache.get(chart_cache_key)

    if not chart_base64_image:
        chart_base64_image = generate_python_chart_base64(chart_labels, chart_student, chart_class_avg)
        if chart_base64_image:
            cache.set(chart_cache_key, chart_base64_image, timeout=86400)

    overall_plv = calculate_primary_plv(total_marks, assessed_subjects, sub_section=student.sub_section, school=school, section=student.school_section) if is_primary else calculate_report_plv(total_points, total_marks)

    from ..models import ClassTeacherMasterComment, SchoolHeadteacherComment
    master_comment = ClassTeacherMasterComment.objects.filter(
        school=school, year=year, term=term, grade=student.class_name,
        stream=student.stream, exam_type=db_assessment,
    ).first()
    school_ht_comment = SchoolHeadteacherComment.objects.filter(
        school=school, year=year, term=term, exam_type=db_assessment,
        school_section=student.school_section,
    ).first()

    class_teacher_remark = ""
    headteacher_comment = ""
    closing_date = None
    opening_date = None
    freeze_threshold = datetime.timedelta(days=30)
    now = datetime.datetime.now(datetime.timezone.utc)

    if master_comment and overall_plv != '-':
        ct_comment_field = f"comment_{overall_plv.lower()}"
        live_ct = getattr(master_comment, ct_comment_field, "") or ""
        if live_ct.strip():
            age = now - (master_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if master_comment.last_modified.tzinfo is None else master_comment.last_modified)
            class_teacher_remark = live_ct
            if age >= freeze_threshold:
                for m in marks_list:
                    if not m.frozen_class_teacher_comment:
                        m.frozen_class_teacher_comment = live_ct
                        m.frozen_closing_date = master_comment.closing_date
                        m.frozen_opening_date = master_comment.opening_date
                Mark.all_objects.filter(id__in=[m.id for m in marks_list]).update(
                    frozen_class_teacher_comment=live_ct,
                    frozen_closing_date=master_comment.closing_date,
                    frozen_opening_date=master_comment.opening_date,
                )
        elif marks_list and marks_list[0].frozen_class_teacher_comment:
            class_teacher_remark = marks_list[0].frozen_class_teacher_comment

    if school_ht_comment and overall_plv != '-':
        ht_comment_field = f"ht_comment_{overall_plv.lower()}"
        live_ht = getattr(school_ht_comment, ht_comment_field, "") or ""
        if live_ht.strip():
            age = now - (school_ht_comment.last_modified.replace(tzinfo=datetime.timezone.utc) if school_ht_comment.last_modified.tzinfo is None else school_ht_comment.last_modified)
            headteacher_comment = live_ht
            if age >= freeze_threshold:
                for m in marks_list:
                    if not m.frozen_headteacher_comment:
                        m.frozen_headteacher_comment = live_ht
                Mark.all_objects.filter(id__in=[m.id for m in marks_list]).update(
                    frozen_headteacher_comment=live_ht,
                )
        elif marks_list and marks_list[0].frozen_headteacher_comment:
            headteacher_comment = marks_list[0].frozen_headteacher_comment

    if master_comment:
        closing_date = master_comment.closing_date
        opening_date = master_comment.opening_date
    if not closing_date and marks_list and marks_list[0].frozen_closing_date:
        closing_date = marks_list[0].frozen_closing_date
    if not opening_date and marks_list and marks_list[0].frozen_opening_date:
        opening_date = marks_list[0].frozen_opening_date

    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if student.school_section == 'PRIMARY' and student.sub_section == 'LOWER':
        section_accent = section_colors['LOWER_PRIMARY']
    elif student.school_section == 'PRIMARY':
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    # ── Render template ───────────────────────────────────────────────────────
    template_html = render_to_string('students/individual_report_card.html', {
        'student':             student,
        'marks':               marks_list,
        'total_marks':         total_marks,
        'total_points':        total_points,
        'position':            position,
        'class_count':         class_count,
        'overall_plv':         overall_plv,
        'mean_points':         mean_points,
        'mean_points_max':     max_points_per_subj,
        'max_total_marks':     max_total_marks,
        'max_total_points':    max_total_points,
        'grade_descriptors':   grade_descriptors,
        'chart_data_json':     chart_data_json,
        'chart_base64_image':  chart_base64_image,
        'class_teacher_remark': class_teacher_remark,
        'headteacher_comment': headteacher_comment,
        'closing_date':        closing_date,
        'opening_date':        opening_date,
        'selected_year':       year,
        'selected_term':       term,
        'selected_assessment': ASSESSMENT_MAP.get(assessment, assessment),
        'today':               datetime.date.today(),
        'section_accent':      section_accent,
        'student_marks_list':  [{
            'student': student, 'marks': marks_list,
            'total_marks': total_marks, 'total_points': total_points,
            'overall_plv': overall_plv,
            'mean_points': mean_points,
            'mean_points_max': max_points_per_subj,
            'max_total_marks': max_total_marks,
            'max_total_points': max_total_points,
            'grade_descriptors': grade_descriptors,
            'chart_data_json': chart_data_json,
            'chart_base64_image': chart_base64_image,
            'class_teacher_remark': class_teacher_remark,
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment': headteacher_comment,
            'closing_date': closing_date,
            'opening_date': opening_date,
            'position': position, 'class_count': class_count,
        }],
    }, request=request)

    template_html = _embed_logo_base64(template_html, request)

    # ── PDF overrides: hide screen chrome, force print styles ─────────────────
    pdf_css = """
<style id="pdf-override">
  * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
  .rv-shell, .rv-header, .rv-hero, .rv-actions, .rv-scroll,
  .sidebar, .sidebar-overlay, .sidebar-header, .sidebar-footer, .sidebar-nav, .sidebar-user, nav, header, .mobile-topbar, .hamburger-btn,
  .global-loader-overlay, .bottom-nav, .d-print-none, .topbar,
  .btn-print, .btn-print-action,
  .mobile-menu-sheet, .mobile-menu-panel, .mobile-menu-body, .mobile-menu-header, .mobile-menu-backdrop,
  .system-footer {{ display: none !important; visibility: hidden !important; height: 0 !important; overflow: hidden !important; }}
  html, body { margin: 0 !important; padding: 0 !important; background: white !important; overflow: visible !important; }
  .container-fluid { margin: 0 !important; padding: 0 !important; max-width: none !important; width: 100% !important; }
  #reportCardsContainer { display: block !important; width: 100% !important; margin: 0 !important; padding: 0 !important; }
  .report-card {
    display: flex !important; page-break-inside: avoid !important; break-inside: avoid !important;
    margin: 0 auto !important; width: 7.4in !important; max-height: none !important; overflow: visible !important;
    border: none !important; border-left: 12px solid var(--section-accent, #305CDE) !important;
    position: relative !important; font-family: 'Times New Roman', Times, serif !important;
    font-size: 12pt !important; box-sizing: border-box !important;
    padding: 0.12in 0.35in 0.2in !important; line-height: 1.2 !important;
  }
  .report-card + .report-card { page-break-before: always !important; }
  .report-content { display: flex !important; flex-direction: column !important; flex: 1 !important; gap: 8px !important; }
  .report-logo, .rc-logo-placeholder { width: 78px !important; height: 78px !important; }
  .rc-logo-spacer { width: 78px !important; }
  .rc-logo-placeholder { font-size: 30px !important; }
  .rc-schoolinfo h1 { font-size: 16pt !important; margin: 0 0 2px !important; color: var(--section-accent, var(--rc-green-dark)) !important; }
  .rc-schoolinfo .rc-tagline { font-size: 8pt !important; margin-bottom: 3px !important; }
  .rc-schoolinfo .rc-address { font-size: 11pt !important; margin-bottom: 1px !important; }
  .rc-schoolinfo .rc-contact-line { font-size: 9pt !important; }
  .rc-header { gap: 12px !important; padding-bottom: 7px !important; border-bottom: 3px solid var(--section-accent, var(--rc-green)) !important; }
  .rc-banner { padding: 6px 8px !important; font-size: 11pt !important; }
  .rc-top-grid { gap: 16px !important; }
  .rc-photo-placeholder { width: 58px !important; height: 58px !important; font-size: 22px !important; border-radius: 8px !important; }
  .rc-student-name { font-size: 14pt !important; margin-bottom: 4px !important; }
  .rc-detail { font-size: 11pt !important; margin-bottom: 3px !important; }
  .rc-chart-title { font-size: 10pt !important; margin-bottom: 4px !important; }
  .rc-chart-block { padding: 7px !important; }
  .rc-chart-block canvas { height: 100px !important; width: auto !important; max-width: 100% !important; }
  .rc-stats { gap: 8px !important; }
  .rc-stat { padding: 8px 8px !important; border-top: 3px solid var(--section-accent, var(--rc-blue)) !important; }
  .rc-stat-label { font-size: 9pt !important; margin-bottom: 3px !important; }
  .rc-stat-value { font-size: 14pt !important; }
  .table-scroll { overflow: visible !important; }
  .rc-table td { padding: 4px 6px !important; font-size: 11pt !important; line-height: 1.15 !important; }
  .rc-table thead th { padding: 5px 6px !important; font-size: 10pt !important; }
  .rc-remarks-grid { gap: 14px !important; }
  .rc-remark-box { padding: 9px 12px !important; }
  .rc-remark-title { font-size: 10pt !important; margin-bottom: 4px !important; color: var(--section-accent, var(--rc-green)) !important; }
  .rc-remark-author { font-size: 10pt !important; font-weight: 700 !important; color: #000000 !important; margin-bottom: 5px !important; }
  .rc-remark-text { font-size: 12pt !important; min-height: 30px !important; margin-bottom: 6px !important; line-height: 1.2 !important; }
  .rc-signature { font-size: 10pt !important; padding-top: 4px !important; }
  .rc-descriptors-title { font-size: 9pt !important; margin-bottom: 3px !important; }
  .rc-descriptors-table th, .rc-descriptors-table td { padding: 3px 4px !important; font-size: 9pt !important; }
  .footer-dates { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 30px !important; padding-top: 8px !important; margin-top: auto !important; }
  .date-box { font-size: 10pt !important; padding-bottom: 3px !important; border-bottom: 2px solid var(--section-accent, var(--rc-green)) !important; }
  .date-box strong { font-size: 9pt !important; }
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    # ── WeasyPrint — generate PDF directly from HTML ──
    try:
        pdf_data = _generate_pdf(patched_html, landscape=False)
    except Exception as e:
        _log_pdf_error('download_individual_report_pdf', e, {
            'student_id': student_id, 'year': year, 'term': term,
            'assessment': assessment,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        safe_student_name = student.name.strip().replace(" ", "_")
        filename = f"{safe_student_name}_report.pdf"

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_bulk_report_pdf — Parallel PDF Stitching Engine
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=5, window_seconds=60, methods=["GET", "POST"])
def download_bulk_report_pdf(request):
    """
    High-performance bulk report card PDF via per-student WeasyPrint compilation
    and in-memory PdfMerger stitching. Each student is rendered individually,
    then all pages are stitched into a unified PDF stream.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    student_ids   = [sid for sid in request.GET.get('ids', '').split(',') if sid]
    year          = request.GET.get('year', str(datetime.date.today().year))
    term          = request.GET.get('term', 'Term 1')
    assessment    = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    if not student_ids:
        return JsonResponse({'error': 'No students selected for PDF generation.'}, status=400)

    selected_students_base = Student.all_objects.filter(id__in=student_ids, school=school)
    sample = selected_students_base.first()
    if not sample:
        return JsonResponse({'error': 'No valid students found.'}, status=404)

    if not user_can_access_class_stream(request.user, sample.class_name, sample.stream, require_class_teacher=True):
        return JsonResponse({'error': 'You are not allowed to print report cards for this class stream.'}, status=403)

    selected_students_base = selected_students_base.filter(class_name=sample.class_name, stream=sample.stream)

    is_primary = sample.school_section == 'PRIMARY'
    is_lower_primary = is_primary and sample.sub_section == 'LOWER'

    published_subject_codes = get_published_subject_codes(
        sample.class_name, sample.stream, year, term, db_assessment,
        sub_section=sample.sub_section if is_primary else None,
    )
    published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)

    if is_lower_primary:
        subject_mapping = LOWER_PRIMARY_SUBJECT_NAMES
    elif is_primary:
        subject_mapping = PRIMARY_SUBJECT_NAMES
    else:
        subject_mapping = {s.code: s.name for s in published_subjects_qs}

    marks_prefetch = Prefetch(
        'cached_marks',
        queryset=Mark.all_objects.filter(
            school=school, year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs, school_section=sample.school_section,
        ).select_related('subject'),
        to_attr='cached_marks',
    )
    selected_students = selected_students_base.prefetch_related(marks_prefetch)

    leaderboard = get_class_leaderboard(
        school, sample.class_name, sample.stream,
        year, term, db_assessment, published_subjects_qs,
    )
    class_leaderboard_rank = {sid: rank for rank, sid in enumerate(leaderboard['sorted_ids'], 1)}
    total_class_count = leaderboard['class_count']

    class_avg_map = get_cached_class_averages(
        school, sample.class_name, sample.stream,
        year, term, db_assessment, published_subjects_qs,
    )

    grading_config = _grading_config_for(school, sample.school_section, sample.sub_section)
    grade_descriptors = grading_config.subject_scale if grading_config else []
    max_points_per_subj = max((e['points'] for e in grade_descriptors), default=(4 if is_primary else 8))

    teacher_map = {
        a.subject.code: a.teacher_profile.get_full_title()
        for a in SubjectAssignment.all_objects.filter(
            school=school, class_name=sample.class_name, stream=sample.stream
        ).select_related('teacher_profile__user', 'subject')
        if a.subject
    }

    ct_q = Teacher.all_objects.filter(
        school=school, assigned_task__icontains=sample.class_name,
    ).filter(
        Q(assigned_task__icontains=sample.stream),
    ).select_related('user').first()
    class_teacher_name = ct_q.get_full_title() if ct_q else ""

    master_comment = ClassTeacherMasterComment.objects.filter(
        school=school, year=year, term=term, grade=sample.class_name,
        stream=sample.stream, exam_type=db_assessment,
    ).first()
    school_ht_comment = SchoolHeadteacherComment.objects.filter(
        school=school, year=year, term=term, exam_type=db_assessment,
        school_section=sample.school_section,
    ).first()

    # Bulk DB aggregation — one query for all students
    student_totals_qs = (
        Mark.all_objects.filter(
            school=school, year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs, school_section=sample.school_section,
            student__in=selected_students,
        )
        .values('student_id')
        .annotate(total_score=Sum('score'), total_pts=Sum('points'))
    )
    totals_map = {row['student_id']: row for row in student_totals_qs}

    # Section accent colors
    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if is_lower_primary:
        section_accent = section_colors['LOWER_PRIMARY']
    elif is_primary:
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    # ── PDF Stitching Pipeline ────────────────────────────────────────────────
    merger = PdfWriter()

    for student in selected_students:
        marks = sorted(student.cached_marks, key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))
        student_totals = totals_map.get(student.id, {})
        total_marks = student_totals.get('total_score') or 0
        total_points = student_totals.get('total_pts') or 0

        for mark in marks:
            mark.subject_name = subject_mapping.get(mark.subject.code, mark.subject.code)
            mark.teacher_name = teacher_map.get(mark.subject.code, '\u2014')
            if is_primary and not mark.is_absent:
                mark.performance_level, mark.points = _get_primary_performance(
                    mark.score or 0, school=school, section=student.school_section, sub_section=student.sub_section,
                )
            class_avg = class_avg_map.get(mark.subject.code)
            mark.class_average = class_avg
            mark.deviation = round(mark.score - class_avg, 1) if class_avg is not None and mark.score is not None and not mark.is_absent else None

        assessed_subjects = sum(1 for m in marks if m.score is not None and not m.is_absent)
        mean_points = round(total_points / assessed_subjects, 1) if assessed_subjects else 0

        # Chart generation with Redis cache
        chart_labels = [m.subject_name for m in marks if not m.is_absent]
        chart_student = [m.score for m in marks if not m.is_absent]
        chart_class_avg = [class_avg_map.get(m.subject.code, 0) for m in marks if not m.is_absent]

        chart_cache_key = f"student_chart_{student.id}_{year}_{term}"
        chart_base64_image = cache.get(chart_cache_key)
        if not chart_base64_image:
            chart_base64_image = generate_python_chart_base64(chart_labels, chart_student, chart_class_avg)
            if chart_base64_image:
                cache.set(chart_cache_key, chart_base64_image, timeout=86400)

        position = class_leaderboard_rank.get(student.id, 0)

        overall_plv = calculate_primary_plv(
            total_marks, sum(1 for m in marks if m.score),
            sub_section=sample.sub_section, school=school, section=sample.school_section,
        ) if is_primary else calculate_report_plv(total_points, total_marks)

        class_teacher_remark = ""
        headteacher_comment = ""
        closing_date = None
        opening_date = None

        if master_comment and overall_plv != '-':
            ct_field = f"comment_{overall_plv.lower()}"
            live_ct = getattr(master_comment, ct_field, "") or ""
            if live_ct.strip():
                class_teacher_remark = live_ct
            elif marks and marks[0].frozen_class_teacher_comment:
                class_teacher_remark = marks[0].frozen_class_teacher_comment

        if school_ht_comment and overall_plv != '-':
            ht_field = f"ht_comment_{overall_plv.lower()}"
            live_ht = getattr(school_ht_comment, ht_field, "") or ""
            if live_ht.strip():
                headteacher_comment = live_ht
            elif marks and marks[0].frozen_headteacher_comment:
                headteacher_comment = marks[0].frozen_headteacher_comment

        if master_comment:
            closing_date = master_comment.closing_date
            opening_date = master_comment.opening_date
        if not closing_date and marks and marks[0].frozen_closing_date:
            closing_date = marks[0].frozen_closing_date
        if not opening_date and marks and marks[0].frozen_opening_date:
            opening_date = marks[0].frozen_opening_date

        # Build individual student context for the single-card template
        marks_list = list(marks)
        chart_data_json = json.dumps({
            'labels':    chart_labels,
            'student':   chart_student,
            'class_avg': chart_class_avg,
        })

        student_context = {
            'student_marks_list': [{
                'student':              student,
                'marks':                marks_list,
                'total_marks':          total_marks,
                'total_points':         total_points,
                'overall_plv':          overall_plv,
                'mean_points':          mean_points,
                'mean_points_max':      max_points_per_subj,
                'max_total_marks':      assessed_subjects * 100,
                'max_total_points':     assessed_subjects * max_points_per_subj,
                'grade_descriptors':    grade_descriptors,
                'chart_data_json':      chart_data_json,
                'chart_base64_image':   chart_base64_image,
                'class_teacher_remark': class_teacher_remark,
                'class_teacher_name':   class_teacher_name,
                'headteacher_comment':  headteacher_comment,
                'closing_date':         closing_date,
                'opening_date':         opening_date,
                'position':             position,
                'class_count':          total_class_count,
            }],
            'selected_year':       year,
            'selected_term':       term,
            'selected_assessment': db_assessment,
            'class_count':         total_class_count,
            'closing_date':        master_comment.closing_date if master_comment else None,
            'opening_date':        master_comment.opening_date if master_comment else None,
            'section_accent':      section_accent,
        }

        # Render single student HTML
        single_html = render_to_string('students/individual_report_card.html', student_context, request=request)
        single_html = _embed_logo_base64(single_html, request)

        # Inject print CSS for single page
        single_pdf_css = f"""
<style id="pdf-override">
  * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
  html, body {{ margin: 0 !important; padding: 0 !important; background: white !important; font-family: 'Times New Roman', Times, serif !important; font-size: 12pt !important; }}
  .report-card {{ display: block !important; margin: 0 !important; width: 7.4in !important; max-height: 282mm !important; overflow: hidden !important; border: none !important; border-left: 12px solid {section_accent} !important; box-sizing: border-box !important; padding: 0.12in 0.35in 0.2in !important; page-break-inside: avoid !important; break-inside: avoid !important; flex-shrink: 1 !important; }}
  .report-content {{ display: flex !important; flex-direction: column !important; flex: 1 !important; gap: 6px !important; }}
  .rc-chart-img {{ display: block !important; max-height: 110px !important; width: auto !important; margin: 4px auto !important; }}
  .report-card canvas {{ display: none !important; }}
  .rc-table td {{ padding: 2px 5px !important; font-size: 0.86em !important; line-height: 1.05 !important; }}
  .rc-table thead th {{ padding: 2px 5px !important; font-size: 0.86em !important; }}
  .system-footer {{ display: none !important; }}
  .rc-print-watermark {{ display: none !important; }}
  @page {{ size: A4 portrait; margin: 4mm 8mm 4mm 8mm !important; }}
</style>
"""
        single_html = _inject_pdf_css(single_html, single_pdf_css, f'<base href="{request.build_absolute_uri("/")}">')

        # Compile to PDF bytes
        try:
            single_pdf_bytes = HTML(string=single_html).write_pdf(optimize_size='none')
            if single_pdf_bytes:
                merger.append(io.BytesIO(single_pdf_bytes))
        except Exception as e:
            logger.warning("[pdf] Failed to compile PDF for student %s: %s", student.name, str(e))
            continue

    # Stitch all pages into final output
    output_buffer = io.BytesIO()
    merger.write(output_buffer)
    merger.close()

    grade_slug = slugify(sample.class_name or "class")
    stream_slug = slugify(sample.stream or "stream")
    filename = f"Bulk_Report_Cards_{grade_slug}_{stream_slug}_{year}_{slugify(term)}.pdf"

    response = HttpResponse(output_buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    output_buffer.close()
    return response
