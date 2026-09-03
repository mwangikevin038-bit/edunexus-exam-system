"""
PDF export views for broadsheet results and class list registers.

Uses WeasyPrint to render Django templates to PDF,
applying screen-emulated CSS overrides so the output matches the web view.
"""

import base64
import datetime
import hashlib
import io
import json
import logging
import mimetypes
import os
import traceback
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from functools import partial

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
from pathlib import Path

from .constants import ASSESSMENT_MAP, GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, LOWER_PRIMARY_SUBJECT_NAMES, LOWER_PRIMARY_SUBJECT_SHORT_MAP, ORDERED_LEVELS, PRIMARY_PERF_LEVELS, PRIMARY_SUBJECT_NAMES, PRIMARY_SUBJECT_SHORT_MAP, SUBJECT_DISPLAY_ORDER, SUBJECT_SHORT_MAP, get_streams_for_school, sort_subjects
from .reports import PRIMARY_ORDERED_LEVELS
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
    resolve_term_dates,
    user_can_access_class_stream,
)
from ..models import ClassTeacherMasterComment, ExamSummary, Mark, SchoolHeadteacherComment, Student, Subject, SubjectAssignment, Teacher
from ..security import get_request_school, get_request_school_section, get_school_object_or_403, rate_limit, user_has_main_school_admin_override

logger = logging.getLogger('pdf_export')


def generate_premium_vector_chart_svg(labels, student_scores, class_averages):
    """
    Render the student-performance chart with matplotlib.

    Uses a thread-local cached Figure/Axes pair to avoid the ~50ms per-call
    cost of figure creation. Each thread gets its own figure - matplotlib is
    NOT thread-safe to share figures across threads, so thread-local storage
    lets us parallelize without locking or crashes.

    matplotlib's SVG output uses <use xlink:href> for text glyph rendering,
    so the chart is opaque to string substitution - every chart is a fresh
    matplotlib render. Per-call cost post-warmup is ~150ms with figure reuse.
    """
    if not labels:
        return ""

    import numpy as np

    fig, ax = _get_chart_axes(labels, class_averages)
    try:
        # Plot the student line on top of the (re-used) axes + class line.
        x = np.arange(len(labels))
        ax.plot(
            x, student_scores,
            color='#00C853', linewidth=2.5,
            marker='o', markersize=6, markerfacecolor='#00C853',
            markeredgecolor='white', markeredgewidth=1.5,
            label='Student Score', zorder=3,
        )

        # If this render exposes more data than the cached axes assumed, raise
        # the y-limit so the line doesn't get clipped.
        y_top_now = float(max(max(student_scores), float(max(class_averages) if class_averages else 0)))
        if ax.get_ylim()[1] < y_top_now * 1.15:
            ax.set_ylim(0, y_top_now * 1.15)

        svg_buffer = io.StringIO()
        fig.savefig(svg_buffer, format='svg', bbox_inches='tight', transparent=True)
        svg_string = svg_buffer.getvalue()
        svg_buffer.close()

        if svg_string.startswith('<?xml'):
            svg_string = svg_string[svg_string.index('?>') + 2:].lstrip()

        return svg_string
    except Exception:
        logger.exception("[pdf] chart render failed")
        return ""
    finally:
        # Remove the just-plotted student line so the next render starts clean.
        # We keep lines[0] (class average) and lines[1] (fill); we pop the rest.
        try:
            lines = ax.get_lines()
            if len(lines) > 2:
                for ln in lines[2:]:
                    ln.remove()
        except Exception:
            pass


def _build_chart_axes(labels):
    """
    Create a bare Figure + Axes used by every chart render.

    Cached per-thread via ``_get_chart_axes``. The class-average line +
    fill + y-limit are drawn by the caller in ``_get_chart_axes`` since
    they depend on the class_averages vector.
    """
    import matplotlib
    matplotlib.use('SVG')  # Vector backend - mandatory
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MultipleLocator

    fig, ax = plt.subplots(figsize=(7, 3))
    fig.patch.set_facecolor('none')
    ax.set_facecolor('none')

    # Font / colour rcParams - set once, persists for the figure's lifetime.
    matplotlib.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans', 'Helvetica', 'Verdana']
    matplotlib.rcParams['font.family'] = 'sans-serif'
    matplotlib.rcParams['text.color'] = '#374151'
    matplotlib.rcParams['axes.labelcolor'] = '#374151'
    matplotlib.rcParams['xtick.color'] = '#374151'
    matplotlib.rcParams['ytick.color'] = '#374151'

    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_visible(False)
    ax.spines['bottom'].set_color('#D5D5DB')
    ax.spines['bottom'].set_linewidth(1)
    ax.grid(True, axis='y', linestyle='-', linewidth=0, color='none')
    ax.grid(True, axis='x', linestyle='-', linewidth=0.5, color='#E5E7EB', zorder=1)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 100)  # placeholder; _get_chart_axes raises this per render
    ax.yaxis.set_major_locator(MultipleLocator(20))

    return fig, ax


# Module-level thread-local storage for the matplotlib figure + axes.
#
# matplotlib figures are NOT thread-safe to share across threads. To get
# parallel chart rendering without locking or crashes we give each worker
# thread its own figure on first call, then reuse it for subsequent calls.
#
# Thread-local storage avoids the ~50ms per-call figure-creation cost that
# dominated the original implementation, while staying safe under
# ThreadPoolExecutor. Each thread worker amortizes the figure cost over
# every chart it renders.
import threading as _threading
_chart_local = _threading.local()


def _get_chart_axes(labels, class_averages):
    """Return a (reusable, thread-local) Figure + Axes with class-avg pre-drawn.

    The figure is created lazily on the first call from a given thread, then
    reused for every subsequent call from that thread until the thread dies.
    The figure is recreated if the subject labels OR class-averages change
    (which only happens across classes, not within a batch of students).
    """
    import matplotlib.pyplot as plt
    import numpy as np

    fig = getattr(_chart_local, 'fig', None)
    ax = getattr(_chart_local, 'ax', None)
    cached_labels = getattr(_chart_local, 'labels', None)
    cached_avgs = getattr(_chart_local, 'avgs', None)

    if fig is None or cached_labels != labels or cached_avgs != class_averages:
        if fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass
        fig, ax = _build_chart_axes(labels)
        # Draw the class-average line ONCE into the axes.
        if class_averages:
            x = np.arange(len(labels))
            ax.plot(
                x, class_averages,
                color='#A1A7B3', linewidth=2.5,
                linestyle='-', marker='o', markersize=4, markerfacecolor='#A1A7B3',
                markeredgecolor='white', markeredgewidth=1.5, alpha=0.85,
                label='Class Average', zorder=2,
            )
            ax.fill_between(x, class_averages, alpha=0.2, color='#D1D5DB', zorder=1)
            ax.set_ylim(0, max(class_averages) * 1.15)
        _chart_local.fig = fig
        _chart_local.ax = ax
        _chart_local.labels = labels
        _chart_local.avgs = class_averages

    # Per-render x-tick labels (cheap).
    x = np.arange(len(labels))
    ax.set_xticks(x)
    ax.set_xticklabels(
        labels, rotation=0, ha='center', fontsize=10,
        fontweight='bold', color='#374151',
    )
    ax.tick_params(axis='x', which='major', labelsize=10, pad=6, colors='#374151')
    return fig, ax


def _compile_single_student_pdf(student_context, logo_base64, section_accent, base_url):
    """
    Compile a single student's report card HTML to PDF bytes.

    Uses the stripped ``report_card_print.html`` template (no ``base.html``)
    plus the unified ``report_card_print.css`` — the same CSS the browser
    loads via ``<link media="print">``. This guarantees the downloaded PDF
    is byte-equivalent to what the user sees with Ctrl+P.

    Designed to be called from a thread pool (Gunicorn gthread) or a
    ProcessPoolExecutor. Reading the CSS file from disk is cheap and avoids
    a 200-line inline CSS string per call.
    """
    from django.template.loader import render_to_string
    from weasyprint import HTML
    from django.conf import settings

    # Render the stripped template — no base.html, no sidebar/topbar/context
    # card HTML for WeasyPrint to walk and discard.
    single_html = render_to_string(
        'students/report_card_print.html',
        student_context,
        request=None,
    )

    # Embed the school logo as a data URI so WeasyPrint doesn't need network.
    if logo_base64:
        single_html = single_html.replace('src="/static/', f'src="{logo_base64}')

    # Load the unified print CSS from disk (cached after first read)
    print_css = _load_print_css()

    # Inject <base> + the unified CSS so the PDF looks identical to Ctrl+P.
    single_html = single_html.replace(
        '</head>',
        f'<base href="{base_url}"><style id="pdf-override">{print_css}</style></head>',
        1,
    )

    try:
        from weasyprint import HTML as _WeasyHTML
        return _WeasyHTML(string=single_html).write_pdf(optimize_size='images')
    except Exception:
        logger.exception("[pdf] WeasyPrint write_pdf failed")
        return None


_PRINT_CSS_CACHE = None

def _load_print_css():
    """
    Read students/static/students/css/report_card_print.css once per process
    and cache it in memory. Subsequent PDF compiles skip the disk hit.
    """
    global _PRINT_CSS_CACHE
    if _PRINT_CSS_CACHE is not None:
        return _PRINT_CSS_CACHE
    from django.conf import settings as _s
    css_path = Path(_s.BASE_DIR) / 'students' / 'static' / 'students' / 'css' / 'report_card_print.css'
    try:
        _PRINT_CSS_CACHE = css_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.error("[pdf] report_card_print.css not found at %s", css_path)
        _PRINT_CSS_CACHE = ''
    return _PRINT_CSS_CACHE


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
        from weasyprint import HTML as _WeasyHTML
        # Create WeasyPrint HTML document instance directly from string
        html_doc = _WeasyHTML(string=patched_html)

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
    Renders the real results_list.html, injects PDF overrides, and generates
    a high-quality PDF via WeasyPrint.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    is_admin_view = user_has_main_school_admin_override(request.user)

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
        published_subject_codes = get_published_subject_codes(grade, stream, year, term, exam_type, sub_section=active_sub if is_primary else None, is_admin=is_admin_view)
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
    # WeasyPrint is rendering HTML outside the normal browser page.
    template_html = _embed_logo_base64(template_html, request)

    # ── 3. Minimal PDF overlay CSS ────────────────────────────────────────────
    #
    # WeasyPrint uses print media so the template's own
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

    # Give WeasyPrint a real origin so relative media/static URLs load in PDFs.
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
    high-quality PDF. Accepts either 'context' param or direct 'grade' + 'stream' params.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from ..models import Grade, Stream, Student
    from django.db.models import CharField, Value
    from django.db.models.functions import Substr, Length
    from django.db.models import IntegerField
    from django.db.models.functions import Cast

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    view_mode = request.GET.get('view_mode', 'teacher').strip()
    if view_mode not in ('teacher', 'admin'):
        view_mode = 'teacher'

    # Section-aware accent color based on grade
    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if grade_name in ['Grade 1', 'Grade 2', 'Grade 3']:
        section_accent = section_colors['LOWER_PRIMARY']
    elif grade_name in ['Grade 4', 'Grade 5', 'Grade 6']:
        section_accent = section_colors['PRIMARY']
    else:
        section_accent = section_colors['JSS']

    is_admin_view = user_has_main_school_admin_override(request.user)

    # Section access check — teachers can only download class lists for their section
    if not is_admin_view and grade_name:
        section = get_request_school_section(request)
        from .constants import LOWER_PRIMARY_GRADE_CHOICES, PRIMARY_GRADE_CHOICES, JSS_GRADE_CHOICES
        if section == 'LOWER_PRIMARY' and grade_name not in LOWER_PRIMARY_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download class lists for your section.", status=403)
        elif section == 'PRIMARY' and grade_name not in PRIMARY_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download class lists for your section.", status=403)
        elif section == 'JSS' and grade_name not in JSS_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download class lists for your section.", status=403)

    students = []
    if grade_name and stream_name:
        qs_base = Student.all_objects.filter(
            school=school, class_name=grade_name, is_active=True
        ).filter(
            admission_no__regex=r'^[0-9]+[PJ]$'
        ).select_related('guardian').annotate(
            adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField())
        )
        if stream_name == 'Combined':
            students = list(qs_base.order_by('stream', 'adm_int'))
        else:
            students = list(qs_base.filter(stream=stream_name).order_by('adm_int'))

    template_html = render_to_string('students/class_list_printout_pdf.html', {
        'school':                 school,
        'students':              students,
        'selected_grade':        grade_name,
        'selected_stream':       stream_name,
        'current_view_mode':     view_mode,
        'is_admin_view':         is_admin_view,
        'section_accent':        section_accent,
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

  body > * {{
    margin-left: 0 !important;
    padding-left: 0 !important;
    width: 100% !important;
    max-width: 100% !important;
  }}

  .pdf-sheet {{
    padding: 0 !important;
    width: 100% !important;
  }}

  .pdf-heading {{
    text-align: center !important;
    position: relative !important;
    padding: 0 130px !important;
    min-height: 110px !important;
    margin-bottom: 10pt !important;
  }}

  .pdf-logo {{
    position: absolute !important;
    left: 0 !important;
    top: 50% !important;
    transform: translateY(-50%) !important;
    width: 110px !important;
    height: 110px !important;
    object-fit: contain !important;
  }}

  .pdf-heading-copy {{
    text-align: center !important;
  }}

  .pdf-heading-copy h2 {{
    font-family: "Times New Roman", Times, serif !important;
    font-size: 25pt !important;
    font-weight: 900 !important;
    text-transform: uppercase !important;
    color: {section_accent} !important;
    margin: 0 !important;
    letter-spacing: 0.08em !important;
  }}

  .pdf-heading-copy p {{
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12pt !important;
    color: #64748b !important;
    margin: 2px 0 0 !important;
    font-weight: 700 !important;
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
    background: #E9ECF0 !important;
    color: #1E293B !important;
    font-weight: 900 !important;
    font-size: 12pt !important;
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    text-align: left !important;
    text-transform: uppercase !important;
    line-height: 1.05 !important;
  }}

  .register-table td {{
    padding: 3pt 5pt !important;
    border: 1.5px solid #000 !important;
    color: #000 !important;
    font-weight: 800 !important;
    font-size: 12pt !important;
    line-height: 1.05 !important;
    vertical-align: middle !important;
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

  .clp-actions, .clp-card, .sub-nav-bar, .sidebar, .global-header {{
    display: none !important;
    visibility: hidden !important;
  }}

  @page {{
    size: A4 portrait;
    margin: 0.62in 0.38in 0.72in 0.5in;
  }}
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    try:
        pdf_data = _generate_pdf(patched_html, landscape=False)
    except Exception as e:
        _log_pdf_error('download_classlist_pdf', e, {
            'grade': grade_name, 'stream': stream_name,
            'view_mode': view_mode,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        slug_grade  = slugify(grade_name  or "class")
        slug_stream = slugify(stream_name or "stream")
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
    via WeasyPrint for high-quality output.
    """

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not student:
        return JsonResponse({'error': 'Student not found.'}, status=404)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        return JsonResponse({'error': 'You are not allowed to print report cards for this class stream.'}, status=403)

    is_admin_view = user_has_main_school_admin_override(request.user)

    year       = request.GET.get('year', datetime.date.today().year)
    term       = request.GET.get('term', 'Term 1')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    # Term-date fallback for closing / opening dates
    _term_closing, _term_opening = resolve_term_dates(school, int(year), term)

    student_sub_section = 'LOWER' if student.class_name in LOWER_PRIMARY_GRADE_CHOICES else ('UPPER' if student.school_section == 'PRIMARY' else None)

    published_subject_codes = get_published_subject_codes(
        student.class_name, student.stream, year, term, db_assessment,
        sub_section=student_sub_section,
        is_admin=is_admin_view,
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

    grade_summaries = ExamSummary.all_objects.filter(
        school=school,
        year=year, term=term, exam_name=db_assessment,
        school_section=student.school_section, sub_section=student.sub_section,
    )
    grade_sorted = sorted(grade_summaries, key=lambda s: (-s.total_marks, -s.total_points))
    class_leaderboard_rank = {s.student_id: rank for rank, s in enumerate(grade_sorted, start=1)}
    class_count = len(grade_sorted)
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

    grade_descriptors = resolve_scale_fast(school.pk, student.school_section, student.sub_section)

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

    # Generate server-side vector SVG chart for PDF rendering (WeasyPrint compatible)
    chart_labels = [m.subject_name for m in marks_list if not m.is_absent]
    chart_student = [m.score for m in marks_list if not m.is_absent]
    chart_class_avg = [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent]

    # Check Redis cache first before generating new chart
    chart_cache_key = f"student_chart_{student.id}_{year}_{term}"
    chart_svg = cache.get(chart_cache_key)

    if not chart_svg:
        chart_svg = generate_premium_vector_chart_svg(chart_labels, chart_student, chart_class_avg)
        if chart_svg:
            cache.set(chart_cache_key, chart_svg, timeout=86400)

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
    if not closing_date and _term_closing:
        closing_date = _term_closing
    if not opening_date and _term_opening:
        opening_date = _term_opening

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
    template_html = render_to_string('students/report_card_print.html', {
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
        'chart_svg':           chart_svg,
        'class_teacher_remark': class_teacher_remark,
        'headteacher_comment': headteacher_comment,
        'closing_date':        closing_date,
        'opening_date':        opening_date,
        'selected_year':       year,
        'selected_term':       term,
        'selected_assessment': ASSESSMENT_MAP.get(assessment, assessment),
        'today':               datetime.date.today(),
        'section_accent':      section_accent,
        'view_mode':           'pdf',
        'show_mobile_shell':   False,
        'show_header':         False,
        'show_control_panel':  False,
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
            'chart_svg': chart_svg,
            'class_teacher_remark': class_teacher_remark,
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment': headteacher_comment,
            'closing_date': closing_date,
            'opening_date': opening_date,
            'position': position, 'class_count': class_count,
        }],
    }, request=request)

    template_html = _embed_logo_base64(template_html, request)

    # Inject <base> + the unified CSS so the PDF looks identical to Ctrl+P.
    print_css = _load_print_css()
    patched_html = template_html.replace(
        '</head>',
        f'<base href="{request.build_absolute_uri("/")}"><style id="pdf-override">{print_css}</style></head>',
        1,
    )

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

        mode = request.GET.get('mode', 'attachment')
        disposition = 'inline' if mode == 'inline' else 'attachment'
        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


@login_required(login_url='login')
def individual_report_print_html(request, student_id):
    """
    GET /report/<student_id>/print-html/
    
    Returns clean print-only HTML for the popup print system.
    Same data as download_individual_report_pdf but returns HTML instead of PDF.
    The popup window loads this URL, then fires window.print() on the clean content.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from .grading_engine import prefetch_school_grading, resolve_scale_fast
    prefetch_school_grading(school)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not student:
        return JsonResponse({'error': 'Student not found.'}, status=404)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        return JsonResponse({'error': 'Not authorized.'}, status=403)

    is_admin_view = user_has_main_school_admin_override(request.user)

    year       = request.GET.get('year', datetime.date.today().year)
    term       = request.GET.get('term', 'Term 1')
    assessment = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    _term_closing, _term_opening = resolve_term_dates(school, int(year), term)

    student_sub_section = 'LOWER' if student.class_name in LOWER_PRIMARY_GRADE_CHOICES else ('UPPER' if student.school_section == 'PRIMARY' else None)

    published_subject_codes = get_published_subject_codes(
        student.class_name, student.stream, year, term, db_assessment,
        sub_section=student_sub_section,
        is_admin=is_admin_view,
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

    grade_summaries = ExamSummary.all_objects.filter(
        school=school,
        year=year, term=term, exam_name=db_assessment,
        school_section=student.school_section, sub_section=student.sub_section,
    )
    grade_sorted = sorted(grade_summaries, key=lambda s: (-s.total_marks, -s.total_points))
    class_leaderboard_rank = {s.student_id: rank for rank, s in enumerate(grade_sorted, start=1)}
    class_count = len(grade_sorted)
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

    grade_descriptors = resolve_scale_fast(school.pk, student.school_section, student.sub_section)

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

    chart_labels = [m.subject_name for m in marks_list if not m.is_absent]
    chart_student = [m.score for m in marks_list if not m.is_absent]
    chart_class_avg = [class_avg_map.get(m.subject.code, 0) for m in marks_list if not m.is_absent]

    chart_cache_key = f"student_chart_{student.id}_{year}_{term}"
    chart_svg = cache.get(chart_cache_key)
    if not chart_svg:
        chart_svg = generate_premium_vector_chart_svg(chart_labels, chart_student, chart_class_avg)
        if chart_svg:
            cache.set(chart_cache_key, chart_svg, timeout=86400)

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
            class_teacher_remark = live_ct
        elif marks_list and marks_list[0].frozen_class_teacher_comment:
            class_teacher_remark = marks_list[0].frozen_class_teacher_comment

    if school_ht_comment and overall_plv != '-':
        ht_comment_field = f"ht_comment_{overall_plv.lower()}"
        live_ht = getattr(school_ht_comment, ht_comment_field, "") or ""
        if live_ht.strip():
            headteacher_comment = live_ht
        elif marks_list and marks_list[0].frozen_headteacher_comment:
            headteacher_comment = marks_list[0].frozen_headteacher_comment

    if master_comment:
        closing_date = master_comment.closing_date
        opening_date = master_comment.opening_date
    if not closing_date and marks_list and marks_list[0].frozen_closing_date:
        closing_date = marks_list[0].frozen_closing_date
    if not opening_date and marks_list and marks_list[0].frozen_opening_date:
        opening_date = marks_list[0].frozen_opening_date
    if not closing_date and _term_closing:
        closing_date = _term_closing
    if not opening_date and _term_opening:
        opening_date = _term_opening

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

    template_html = render_to_string('students/report_card_print.html', {
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
        'chart_svg':           chart_svg,
        'class_teacher_remark': class_teacher_remark,
        'class_teacher_name':   class_teacher_name,
        'headteacher_comment': headteacher_comment,
        'closing_date':        closing_date,
        'opening_date':        opening_date,
        'selected_year':       year,
        'selected_term':       term,
        'selected_assessment': ASSESSMENT_MAP.get(assessment, assessment),
        'today':               datetime.date.today(),
        'section_accent':      section_accent,
        'view_mode':           'print',
        'show_mobile_shell':   False,
        'show_header':         False,
        'show_control_panel':  False,
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
            'chart_svg': chart_svg,
            'class_teacher_remark': class_teacher_remark,
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment': headteacher_comment,
            'closing_date': closing_date,
            'opening_date': opening_date,
            'position': position, 'class_count': class_count,
        }],
    }, request=request)

    template_html = _embed_logo_base64(template_html, request)

    # The template already loads report_card_print.css and has an auto-print
    # script that fires when view_mode=print is in the URL. We just need to
    # ensure the <base> tag is set for static file resolution.
    base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    patched_html = template_html.replace(
        '</head>',
        f'{base_tag}</head>',
        1,
    )

    return HttpResponse(patched_html, content_type='text/html; charset=utf-8')


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

    All per-student data fetching is delegated to
    ``build_report_card_context`` (students/views/helpers.py) — the same helper
    used by ``report_forms_display`` — so the printed PDF and the on-screen
    preview are guaranteed to show identical totals, ranks, PLV, and comments.

    Accepts the student list in two equivalent ways:
      * `ids=A,B,C`        - explicit student ID list (legacy)
      * `grade=X&stream=Y&exam_id=Z` - resolve all students in the
        (grade, stream, exam) combination automatically. This is what the
        dashboard's Download PDF button sends — no need to resolve IDs
        client-side first.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from .helpers import build_report_card_context

    is_admin_view = user_has_main_school_admin_override(request.user)

    grade_name  = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    exam_id     = request.GET.get('exam_id', '').strip()
    year        = request.GET.get('year', str(datetime.date.today().year))
    term        = request.GET.get('term', 'Term 1')
    assessment  = request.GET.get('assessment', 'opener')
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    # ── Resolve student IDs from the two accepted URL shapes ────────────────
    student_ids = [sid for sid in request.GET.get('ids', '').split(',') if sid]

    if not student_ids and grade_name and stream_name and exam_id:
        # Resolve via build_report_card_context's built-in student list.
        # We use it for resolution only — the actual rendering is the same
        # code path as the explicit-ids case below.
        from ..models import Exam
        try:
            _exam = Exam.all_objects.get(id=exam_id, school=school, is_deleted=False)
        except (Exam.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Exam not found.'}, status=404)
        db_assessment = _exam.name
        # Pre-fetch the student IDs that belong to this grade/stream/section.
        _resolved = build_report_card_context(
            school, grade_name, stream_name, exam_id,
            include_chart_svg=False,
            is_admin=is_admin_view,
        )
        student_ids = [s['student'].id for s in _resolved['student_marks_list']]
        if not student_ids:
            return JsonResponse({'error': 'No students found for that grade/stream/exam combination.'}, status=404)

    if not student_ids:
        return JsonResponse({'error': 'No students selected for PDF generation. Pass ids=... OR grade=...&stream=...&exam_id=...'}, status=400)

    # Sample student — needed for class/stream/section access checks
    sample = Student.all_objects.filter(id__in=student_ids, school=school).first()
    if not sample:
        return JsonResponse({'error': 'No valid students found.'}, status=404)

    if not user_can_access_class_stream(
        request.user, sample.class_name, sample.stream, require_class_teacher=True,
    ):
        return JsonResponse(
            {'error': 'You are not allowed to print report cards for this class stream.'},
            status=403,
        )

    # ── Unified data build ────────────────────────────────────────────────────
    try:
        ctx = build_report_card_context(
            school, sample.class_name, sample.stream, db_assessment,
            student_ids=student_ids,
            include_chart_svg=True,
            is_admin=is_admin_view,
        )
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found.'}, status=404)

    section_accent = ctx['section_accent']
    student_marks_list = ctx['student_marks_list']

    # ── Pre-compute logo base64 ONCE (not per student) ─────────────────────────
    logo_base64_data = ""
    try:
        school_logo = getattr(school, "logo", None)
        if school_logo:
            logo_url = school_logo.url
            logo_type = mimetypes.guess_type(logo_url)[0] or "image/png"
            with school_logo.open("rb") as logo_file:
                logo_data = base64.b64encode(logo_file.read()).decode("ascii")
            logo_base64_data = f'data:{logo_type};base64,{logo_data}'
    except Exception:
        logger.warning("Failed to pre-compute school logo base64", exc_info=True)

    # ── Wrap each per-student dict in the single-card view context ─────────────
    student_contexts = []
    for student_data in student_marks_list:
        student_contexts.append({
            'student_marks_list':   [student_data],
            'selected_year':        ctx['selected_year'],
            'selected_term':        ctx['selected_term'],
            'selected_assessment':  ctx['selected_assessment_raw'],
            'class_count':          ctx['class_count'],
            'closing_date':         ctx['closing_date'],
            'opening_date':         ctx['opening_date'],
            'section_accent':       section_accent,
            'view_mode':            'pdf',
            'show_mobile_shell':    False,
            'show_header':          False,
            'show_control_panel':   False,
        })

    if not student_contexts:
        return JsonResponse({'error': 'No students found for the selected class stream.'}, status=404)

    # Close DB connections before spawning workers to prevent pool leaks
    # (the worker thread may still hold a connection in its thread-local pool).
    from django.db import connection
    connection.close()

    # Parallel PDF compilation via ThreadPoolExecutor.
    #
    # WeasyPrint releases the GIL during Pango layout + font rasterisation,
    # so threads give genuine parallel speedup. Compared to the original
    # ProcessPoolExecutor approach this saves the ~2.5s per-worker fork+import
    # cost (which made ProcessPool SLOWER than sequential for batches under
    # ~30 students).
    #
    # The chart cache is now thread-local so each worker gets its own
    # matplotlib figure - no thread-safety crash.
    base_url = request.build_absolute_uri("/")
    compile_fn = partial(
        _compile_single_student_pdf,
        logo_base64=logo_base64_data,
        section_accent=section_accent,
        base_url=base_url,
    )

    # Sized by CPU count but capped - each WeasyPrint render uses 100-200 MB
    # of RAM so going beyond the available cores just causes thrashing.
    max_workers = min(4, max(1, (os.cpu_count() or 2)))
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pdf_results = list(executor.map(compile_fn, student_contexts))
    except Exception as e:
        logger.warning("[pdf] ThreadPoolExecutor failed, falling back to sequential: %s", str(e))
        pdf_results = [compile_fn(ctx) for ctx in student_contexts]

    # Stitch all pages into final output
    merger = PdfWriter()
    for pdf_bytes in pdf_results:
        if pdf_bytes:
            merger.append(io.BytesIO(pdf_bytes))
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


# ═══════════════════════════════════════════════════════════════════════
#  BACKGROUND PDF GENERATION — Start / Poll / Download endpoints
# ═══════════════════════════════════════════════════════════════════════

import uuid as _uuid


def _get_pdf_cache():
    from django.core.cache import caches
    return caches["pdf_generation"]


@login_required
def start_bulk_report_pdf(request):
    """
    POST /bulk-reports/generate-pdf/
    
    Starts background PDF generation via Celery. Returns a job_id that
    the frontend polls via /api/pdf-progress/<job_id>/.
    
    Accepts same params as download_bulk_report_pdf:
      grade, stream, exam_id, year, term, assessment
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    grade_name  = request.GET.get('grade', '').strip() or request.POST.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip() or request.POST.get('stream', '').strip()
    exam_id     = request.GET.get('exam_id', '').strip() or request.POST.get('exam_id', '').strip()
    year        = request.GET.get('year', str(datetime.date.today().year)).strip()
    term        = request.GET.get('term', 'Term 1').strip()
    assessment  = request.GET.get('assessment', 'opener').strip()

    if not grade_name or not stream_name or not exam_id:
        return JsonResponse({'error': 'grade, stream, and exam_id are required.'}, status=400)

    # Resolve student IDs (same logic as synchronous view)
    from .helpers import build_report_card_context
    db_assessment = ASSESSMENT_MAP.get(assessment, assessment)

    is_admin_view = user_has_main_school_admin_override(request.user)

    try:
        _exam = Exam.all_objects.get(id=exam_id, school=school, is_deleted=False)
    except (Exam.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Exam not found.'}, status=404)

    db_assessment = _exam.name

    try:
        _resolved = build_report_card_context(
            school, grade_name, stream_name, exam_id,
            include_chart_svg=False,
            is_admin=is_admin_view,
        )
        student_ids = [s['student'].id for s in _resolved['student_marks_list']]
    except Exam.DoesNotExist:
        return JsonResponse({'error': 'Exam not found.'}, status=404)

    if not student_ids:
        return JsonResponse({'error': 'No students found for that grade/stream/exam.'}, status=404)

    # Access check
    sample = Student.all_objects.filter(id__in=student_ids, school=school).first()
    if not sample:
        return JsonResponse({'error': 'No valid students found.'}, status=404)

    if not user_can_access_class_stream(
        request.user, sample.class_name, sample.stream, require_class_teacher=True,
    ):
        return JsonResponse({'error': 'Not authorized for this class stream.'}, status=403)

    # Generate unique job ID and dispatch to Celery
    job_id = _uuid.uuid4().hex[:16]

    from .tasks import generate_bulk_report_pdf
    generate_bulk_report_pdf.delay(
        job_id=job_id,
        school_id=school.id,
        grade_name=grade_name,
        stream_name=stream_name,
        exam_id=int(exam_id),
        year=year,
        term=term,
        assessment=assessment,
        student_ids=student_ids,
        user_id=request.user.id,
    )

    return JsonResponse({
        'job_id': job_id,
        'total': len(student_ids),
        'message': 'PDF generation started in background.',
    })


@login_required
def pdf_progress(request, job_id):
    """
    GET /api/pdf-progress/<job_id>/
    
    Poll endpoint for the frontend to check PDF generation progress.
    Returns compiled/total counts and status.
    """
    pdf_cache = _get_pdf_cache()

    # Check for completion first
    result = pdf_cache.get(f"pdf_result_{job_id}")
    if result:
        return JsonResponse(result)

    # Check for in-progress
    progress = pdf_cache.get(f"pdf_progress_{job_id}")
    if progress:
        return JsonResponse(progress)

    return JsonResponse({'status': 'not_found', 'message': 'Job not found or expired.'}, status=404)


@login_required
def download_generated_pdf(request, job_id):
    """
    GET /api/pdf-download/<job_id>/
    
    Download the completed PDF. Only available after generation is complete.
    Cleans up cache entries after download.
    """
    pdf_cache = _get_pdf_cache()

    result = pdf_cache.get(f"pdf_result_{job_id}")
    if not result:
        return JsonResponse({'error': 'PDF not ready or expired.'}, status=404)

    if result.get('status') == 'error':
        return JsonResponse(result, status=400)

    pdf_bytes = pdf_cache.get(f"pdf_data_{job_id}")
    if not pdf_bytes:
        return JsonResponse({'error': 'PDF data expired. Please regenerate.'}, status=404)

    filename = result.get('filename', 'report_cards.pdf')

    # Clean up cache
    pdf_cache.delete(f"pdf_data_{job_id}")
    pdf_cache.delete(f"pdf_result_{job_id}")

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response
