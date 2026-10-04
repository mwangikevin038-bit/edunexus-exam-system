"""
PDF export views for broadsheet results and class list registers.

Hybrid rendering engine:
  - Playwright (headless Chrome) for user-triggered downloads — pixel-perfect
  - WeasyPrint for background/Celery bulk tasks — fast, lightweight
"""

import base64
import datetime
import io
import logging
import mimetypes
import os
import traceback

from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST
from pathlib import Path

try:
    from ..pdf_engine import render_html_to_pdf as _playwright_render, get_print_css as _get_playwright_css
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# A real rendered page is always >100KB; a blank page is ~0.7-2KB. Anything
# below this threshold is treated as a failed/empty render, never shipped.
_MIN_VALID_PDF_BYTES = 2048

from .constants import JSS_GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, LOWER_PRIMARY_SUBJECT_SHORT_MAP, ORDERED_LEVELS, PRIMARY_PERF_LEVELS, PRIMARY_GRADE_CHOICES, PRIMARY_SUBJECT_SHORT_MAP, SUBJECT_SHORT_MAP, sort_subjects
from .reports import _build_individual_report_context
from .exams import _get_primary_performance
from .helpers import (
    calculate_broadsheet_plv,
    calculate_primary_plv,
    dedup_marks_latest_by_code,
    get_performance_level,
    get_published_contexts_for_user,
    get_published_subject_codes,
    get_selected_context,
    safe_pdf_filename,
    user_can_access_class_stream,
)
from ..models import Exam, Mark, Student, SubjectAssignment
from ..security import get_request_school, get_request_school_section, get_school_object_or_403, rate_limit, user_has_main_school_admin_override

logger = logging.getLogger('pdf_export')


# ── Playwright PDF Helper ──────────────────────────────────────────────────

def _build_playwright_html(template_html, request, *, landscape=False):
    """
    Wrap a Django template's rendered HTML into a complete document
    ready for Playwright PDF rendering.

    Injects:
      - <base> tag for static file resolution
      - Shared print CSS from static/css/print-shared.css
      - Logo base64 embedding
    """
    from ..pdf_engine import get_print_css

    html = template_html

    # Embed school logo as data URI
    html = _embed_logo_base64(html, request)

    # Load shared print CSS
    print_css = get_print_css()

    # Inject <base> + print CSS before </head>
    base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    html = html.replace(
        '</head>',
        f'{base_tag}<style id="pdf-override">{print_css}</style></head>',
        1,
    )

    if landscape:
        # The injected print-shared.css declares A4 portrait last in the
        # cascade; with prefer_css_page_size Chromium would honour that.
        # Re-assert landscape AFTER everything else (matches broadsheet.css).
        html = html.replace(
            '</head>',
            '<style id="pdf-landscape">@page { size: A4 landscape; '
            'margin: 8mm 10mm 18mm 10mm; }</style></head>',
            1,
        )

    return html


def _smooth_xy(x, y, samples=16):
    """Catmull-Rom spline through every data point (matches Chart.js
    tension smoothing so print curves look like the browser chart).

    Endpoints are duplicated for the tangent estimate, so the curve passes
    exactly through the first and last marks - no extrapolation drift.
    """
    import numpy as np
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 3:
        return x, y
    px = np.concatenate(([2 * x[0] - x[1]], x, [2 * x[-1] - x[-2]]))
    py = np.concatenate(([2 * y[0] - y[1]], y, [2 * y[-1] - y[-2]]))
    t = np.linspace(0.0, 1.0, samples)
    mt = 1.0 - t
    out_x, out_y = [], []
    for i in range(1, n):
        x0, y0 = px[i - 1], py[i - 1]
        x1, y1 = px[i], py[i]
        x2, y2 = px[i + 1], py[i + 1]
        x3, y3 = px[i + 2], py[i + 2]
        c1x, c1y = x1 + (x2 - x0) / 6.0, y1 + (y2 - y0) / 6.0
        c2x, c2y = x2 - (x3 - x1) / 6.0, y2 - (y3 - y1) / 6.0
        bx = mt ** 3 * x1 + 3 * mt ** 2 * t * c1x + 3 * mt * t ** 2 * c2x + t ** 3 * x2
        by = mt ** 3 * y1 + 3 * mt ** 2 * t * c1y + 3 * mt * t ** 2 * c2y + t ** 3 * y2
        if out_x:
            bx, by = bx[1:], by[1:]
        out_x.append(bx)
        out_y.append(by)
    return np.concatenate(out_x), np.concatenate(out_y)


def generate_premium_vector_chart_svg(labels, student_scores, class_averages,
                                      student_name=None, class_name=None):
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
        # Clear leftovers from the previous render on this reused axes:
        # value labels (ax.texts) and the student line from last time.
        for _t in list(ax.texts):
            _t.remove()
        for _ln in list(ax.get_lines()):
            if _ln.get_label() != 'Class Average':
                _ln.remove()

        x = np.arange(len(labels))
        sx, sy = _smooth_xy(x, student_scores)

        # Premium fill: soft green wash under the student line (print reads
        # better with a filled silhouette; browser keeps class-only fill).
        # Tagged with a gid so the finally-block can strip them - otherwise
        # they pile up on the reused axes and leak one student's band into
        # the next student's chart.
        if class_averages:
            cx, cy = _smooth_xy(x, class_averages)
        else:
            cy = sy
        _band1 = ax.fill_between(sx, sy, cy, where=(sy >= cy), color='#00C853',
                                 alpha=0.10, interpolate=True, zorder=2, linewidth=0)
        _band2 = ax.fill_between(sx, sy, cy, where=(sy < cy), color='#A1A7B3',
                                 alpha=0.14, interpolate=True, zorder=2, linewidth=0)
        for _c in (_band1, _band2):
            _c.set_gid('rc-student-band')

        # Student line - smooth spline, thick, round joins. Markers are drawn
        # separately on the RAW points: passing the smoothed arrays into a
        # marker-bearing plot would stamp a marker at every spline sample
        # (~120 dots) and read as a dotted chain on paper.
        ax.plot(
            sx, sy,
            color='#00C853', linewidth=3.2,
            solid_capstyle='round', solid_joinstyle='round',
            zorder=5,
        )
        ax.plot(
            x, student_scores, linestyle='none',
            marker='o', markersize=7, markerfacecolor='#00C853',
            markeredgecolor='white', markeredgewidth=1.2,
            label='Student Score', zorder=5.5,
        )

        # Marks are percentages — the axis is fixed 0-100. Only widen past
        # 100 for anomalous data so nothing can ever be clipped.
        y_top_now = float(max(max(student_scores), float(max(class_averages) if class_averages else 0)))
        target_top = max(100.0, y_top_now * 1.05)
        if ax.get_ylim()[1] != target_top:
            ax.set_ylim(0, target_top)

        # Bold value labels on every student point - print has no tooltips,
        # so the numbers must live on the chart itself. A white halo keeps
        # them legible where a rising/falling curve passes behind the text.
        import matplotlib.patheffects as _pe
        for xi, yi in zip(x, student_scores):
            if yi is None:
                continue
            if yi >= 88:
                _lab = ax.text(xi, float(yi) - 3.6, f'{yi:g}%', ha='center', va='top',
                               fontsize=10, fontweight='bold', color='#0F7B2E', zorder=6)
            else:
                _lab = ax.text(xi, float(yi) + 3.6, f'{yi:g}%', ha='center', va='bottom',
                               fontsize=10, fontweight='bold', color='#0F7B2E', zorder=6)
            _lab.set_path_effects([_pe.withStroke(linewidth=3.5, foreground='white')])

        # Legend above the plot, right-aligned — mirrors the Chart.js legend
        # on the browser view (● Student  ● Class).
        from matplotlib.lines import Line2D
        legend_handles = [
            Line2D([0], [0], color='#00C853', linewidth=3.2, marker='o', markersize=7,
                   markerfacecolor='#00C853', markeredgecolor='white', markeredgewidth=1.2),
            Line2D([0], [0], color='#A1A7B3', linewidth=2.6, marker='o', markersize=5.5,
                   markerfacecolor='#A1A7B3', markeredgecolor='white', markeredgewidth=1.0,
                   alpha=0.9),
        ]
        legend_labels = [student_name or 'Student', class_name or 'Class Average']
        leg = ax.legend(
            legend_handles, legend_labels,
            loc='lower right', bbox_to_anchor=(1.0, 1.012),
            ncol=2, frameon=False, fontsize=10.5,
            handletextpad=0.4, columnspacing=1.6, borderaxespad=0,
            labelspacing=0.3,
        )
        for txt in leg.get_texts():
            txt.set_color('#374151')
            txt.set_fontweight('bold')

        svg_buffer = io.StringIO()
        fig.savefig(svg_buffer, format='svg', bbox_inches='tight',
                    pad_inches=0.03, transparent=True)
        svg_string = svg_buffer.getvalue()
        svg_buffer.close()

        if svg_string.startswith('<?xml'):
            svg_string = svg_string[svg_string.index('?>') + 2:].lstrip()

        return svg_string
    except Exception:
        logger.exception("[pdf] chart render failed")
        return ""
    finally:
        # Remove per-render student artifacts so the next render starts
        # clean. The class-average line/fill (label 'Class Average' or drawn
        # at build time) persists.
        try:
            for _ln in list(ax.get_lines()):
                if _ln.get_label() != 'Class Average':
                    _ln.remove()
            for _t in list(ax.texts):
                _t.remove()
            for _c in list(ax.collections):
                if _c.get_gid() == 'rc-student-band':
                    _c.remove()
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
    from matplotlib.ticker import MultipleLocator, PercentFormatter

    # Font / colour rcParams MUST be set BEFORE figure creation: the Axes
    # caches tick defaults (size/colour) at construction time, so setting
    # them afterwards left y-ticks at the 3.5pt black default (the stray
    # dash next to "100%" and a +2pt wide viewBox).
    matplotlib.rcParams['font.sans-serif'] = ['Arial', 'DejaVu Sans', 'Helvetica', 'Verdana']
    matplotlib.rcParams['font.family'] = 'sans-serif'
    matplotlib.rcParams['text.color'] = '#374151'
    matplotlib.rcParams['axes.labelcolor'] = '#374151'
    matplotlib.rcParams['xtick.color'] = '#374151'
    matplotlib.rcParams['ytick.color'] = '#374151'
    # Tick label sizes are in viewBox units; the card prints this SVG at
    # ~106mm width (scale ≈ 0.68), so 10u ≈ 6.8pt on paper - the floor for
    # legible print typography. (7u printed ≈ 4.9pt and read as blurry.)
    matplotlib.rcParams['ytick.labelsize'] = 10
    matplotlib.rcParams['ytick.major.size'] = 0
    matplotlib.rcParams['ytick.major.pad'] = 5
    matplotlib.rcParams['xtick.labelsize'] = 10
    matplotlib.rcParams['xtick.major.size'] = 0
    matplotlib.rcParams['xtick.major.pad'] = 5

    fig, ax = plt.subplots(figsize=(7, 3.0))
    fig.patch.set_facecolor('none')
    ax.set_facecolor('none')

    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_visible(False)
    ax.spines['bottom'].set_visible(False)
    ax.grid(True, axis='y', linestyle='-', linewidth=0.7, color='#E5E7EB')
    ax.grid(True, axis='x', linestyle='-', linewidth=0.5, color='#EEF0F3', zorder=1)
    ax.set_axisbelow(True)
    ax.set_ylim(0, 100)  # Marks are percentages — fixed 0-100% axis
    # Breathing room at both ends so edge markers/labels never clip.
    ax.set_xlim(-0.6, max(len(labels) - 0.4, 0.6))
    ax.yaxis.set_major_locator(MultipleLocator(20))
    ax.yaxis.set_major_formatter(PercentFormatter(xmax=100))

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
        # Draw the class-average line ONCE into the axes (smoothed to match
        # the browser's tension curves; cleaned up per-render by label).
        if class_averages:
            x = np.arange(len(labels))
            cx, cy = _smooth_xy(x, class_averages)
            ax.plot(
                cx, cy,
                color='#A1A7B3', linewidth=2.6,
                solid_capstyle='round', solid_joinstyle='round',
                alpha=0.9, label='Class Average', zorder=4,
            )
            ax.plot(
                x, class_averages, linestyle='none', marker='o', markersize=5.5,
                markerfacecolor='#A1A7B3',
                markeredgecolor='white', markeredgewidth=1.0, alpha=0.9,
                label='Class Average', zorder=4.5,
            )
            ax.fill_between(x, class_averages, alpha=0.2, color='#D1D5DB', zorder=1)
            ax.set_ylim(0, 100)  # Fixed percentage axis — mathematically correct
        _chart_local.fig = fig
        _chart_local.ax = ax
        _chart_local.labels = labels
        _chart_local.avgs = class_averages

    # Per-render x-tick labels (cheap) - bold, print-scaled like the browser.
    x = np.arange(len(labels))
    ax.set_xticks(x)
    ax.set_xticklabels(
        labels, rotation=0, ha='center', fontsize=10,
        fontweight='bold', color='#374151',
    )
    ax.tick_params(axis='x', which='major', labelsize=10, pad=5, length=0, colors='#374151')
    # Y ticks: force no tick marks + print colour/size regardless of when
    # the figure was constructed (belt and braces over rcParams).
    ax.tick_params(axis='y', which='major', labelsize=10, pad=5, length=0, colors='#374151')
    # Y tick labels: bold like the browser's 600-weight % labels.
    try:
        for _t in ax.get_yticklabels():
            _t.set_fontweight('bold')
            _t.set_fontsize(10)
            _t.set_color('#374151')
    except Exception:
        pass
    return fig, ax


def _compile_single_student_pdf(student_context, logo_base64, section_accent, base_url):
    """
    Compile a single student's report card HTML to PDF bytes.

    Uses WeasyPrint with the WeasyPrint-compatible CSS for background/bulk tasks.
    This is the fast path used by Celery tasks where speed > perfect fidelity.
    """
    from django.template.loader import render_to_string

    # Render the stripped template — no base.html, no sidebar/topbar/context
    single_html = render_to_string(
        'students/report_card_print.html',
        student_context,
        request=None,
    )

    # Embed the school logo as a data URI so WeasyPrint doesn't need network.
    if logo_base64:
        single_html = single_html.replace('src="/static/', f'src="{logo_base64}')

    # Load the WeasyPrint-compatible CSS from disk (cached after first read)
    print_css = _load_weasyprint_css()

    # Inject <base> + the WeasyPrint CSS
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
_WEASY_CSS_CACHE = None

def _load_print_css():
    """
    Read static/css/print-shared.css once per process and cache it.
    Used by Playwright-based rendering.
    """
    global _PRINT_CSS_CACHE
    if _PRINT_CSS_CACHE is not None:
        return _PRINT_CSS_CACHE
    from django.conf import settings as _s
    css_path = Path(_s.BASE_DIR) / 'static' / 'css' / 'print-shared.css'
    try:
        _PRINT_CSS_CACHE = css_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.error("[pdf] print-shared.css not found at %s", css_path)
        _PRINT_CSS_CACHE = ''
    return _PRINT_CSS_CACHE


def _load_weasyprint_css():
    """
    Read static/css/print-weasyprint.css once per process and cache it.
    Used by WeasyPrint-based rendering (Celery bulk tasks).
    """
    global _WEASY_CSS_CACHE
    if _WEASY_CSS_CACHE is not None:
        return _WEASY_CSS_CACHE
    from django.conf import settings as _s
    css_path = Path(_s.BASE_DIR) / 'static' / 'css' / 'print-weasyprint.css'
    try:
        _WEASY_CSS_CACHE = css_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.error("[pdf] print-weasyprint.css not found at %s", css_path)
        _WEASY_CSS_CACHE = ''
    return _WEASY_CSS_CACHE


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

def _generate_pdf(patched_html, *, landscape=False, margin=None, engine='auto',
                  scale=0.75, margins=None, **kwargs):
    """
    Generate PDF from HTML string. Tries Playwright first, falls back to WeasyPrint.

    Watermark + page numbers come solely from the shared CSS @page margin boxes
    (print-shared.css / broadsheet.css) — the same rules the browser print
    popup uses. Do NOT add a Chromium footer_template on top of them: modern
    Chromium renders the CSS margin boxes too, producing duplicate footers.

    Args:
        patched_html: Complete HTML document string.
        landscape: If True, use A4 landscape.
        margin: Optional single-value margin applied to all four sides.
        engine: 'playwright' | 'weasyprint' | 'auto' (default: try Playwright first).
        scale: Playwright page scale (1.0 = browser-view parity).
        margins: Optional dict with top/right/bottom/left margin strings.
    """
    # Env override — low-memory servers can force one engine, e.g.
    # EDUNEXUS_PDF_ENGINE=weasyprint skips launching Chromium entirely.
    engine = (os.environ.get('EDUNEXUS_PDF_ENGINE') or engine).strip().lower()

    # ── Try Playwright (pixel-perfect rendering) ──
    if engine in ('auto', 'playwright') and _HAS_PLAYWRIGHT:
        last_error = None
        for attempt in (1, 2):
            try:
                use_margins = margins
                if use_margins is None and margin:
                    use_margins = {'top': margin, 'bottom': margin, 'left': margin, 'right': margin}
                pdf_bytes = _playwright_render(
                    patched_html,
                    landscape=landscape,
                    margins=use_margins,
                    timeout_ms=30000,
                    scale=scale,
                )
            except Exception as e:
                last_error = str(e)
                if engine == 'playwright':
                    logger.error(f"[pdf] Playwright generation failed: {last_error}")
                    return {'pdf': None, 'error': last_error}
                logger.warning(f"[pdf] Playwright failed, falling back to WeasyPrint: {last_error}")
                break

            if pdf_bytes and len(pdf_bytes) >= _MIN_VALID_PDF_BYTES:
                return {'pdf': pdf_bytes}

            # A real page renders to well over 100KB; <2KB means an empty
            # document (observed once as a 761-byte blank page). Retry once,
            # then fall through to the fallback engine.
            last_error = f"blank PDF ({len(pdf_bytes or b'')} bytes)"
            logger.warning("[pdf] Playwright produced a %s — attempt %d/2", last_error, attempt)
        else:
            if engine == 'playwright':
                return {'pdf': None, 'error': last_error}

    # ── Fallback: WeasyPrint ──
    try:
        from weasyprint import HTML as _WeasyHTML
        html_doc = _WeasyHTML(string=patched_html)
        pdf_bytes = html_doc.write_pdf(optimize_size='images')
        if not pdf_bytes or len(pdf_bytes) < _MIN_VALID_PDF_BYTES:
            logger.error("[pdf] WeasyPrint produced a blank PDF (%s bytes)", len(pdf_bytes or b''))
            return {'pdf': None, 'error': last_error or 'PDF generation produced an empty document'}
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


_STANDALONE_CSS_CACHE = {}


def _build_standalone_print_html(fragment_html, request, title='EduNexus Document'):
    """Wrap a rendered template fragment in a complete HTML document.

    Uses the exact CSS composition of the browser print popup
    (EDUNEXUSPrint.openPrintWindow): print-shared.css first, then
    broadsheet.css — guaranteeing on-screen/print/PDF parity.
    """
    shared_css = _get_playwright_css() if _HAS_PLAYWRIGHT else ''
    broadsheet_css = _STANDALONE_CSS_CACHE.get('broadsheet')
    if broadsheet_css is None:
        try:
            from django.contrib.staticfiles import finders
            found = finders.find('css/broadsheet.css')
            broadsheet_css = Path(found).read_text(encoding='utf-8') if found else ''
        except Exception:
            logger.warning("broadsheet.css not found for standalone PDF", exc_info=True)
            broadsheet_css = ''
        _STANDALONE_CSS_CACHE['broadsheet'] = broadsheet_css

    html = _embed_logo_base64(fragment_html, request)
    base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<title>{title}</title>{base_tag}'
        f'<style id="pdf-override">{shared_css}\n{broadsheet_css}</style>'
        '</head><body>'
        f'{html}'
        '</body></html>'
    )


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
        # ── Try snapshot first (zero DB queries) ──────────────────────────
        from ..models import ExamResultSnapshot, Exam
        _exam_obj = Exam.all_objects.filter(
            school=school, name=exam_type, term=term, year=year,
        ).first()
        _snap = ExamResultSnapshot.get_latest(
            school, term, year, exam_type, grade, stream,
        ) if _exam_obj else None

        if _snap and _snap.report_card_data and _snap.broadsheet_data:
            # Read everything from snapshot
            _merged_subjects = _snap.broadsheet_data
            published_subject_codes = list(_merged_subjects.keys())
            published_subject_count = len(published_subject_codes)

            from ..models import Subject
            published_subjects_qs = Subject.all_objects.filter(school=school, code__in=published_subject_codes)
            subject_label_map = {
                s.code: (subject_map.get(s.code) or s.name or s.code)
                for s in published_subjects_qs
            }
            published_subjects = sort_subjects([
                (code, subject_label_map.get(code, subject_map.get(code, code)))
                for code in published_subject_codes
            ])

            # Build analysis data from snapshot subject_data
            for code, short in published_subjects:
                data = _merged_subjects.get(code, {})
                analysis_data[short] = {
                    'entries': data.get('student_count', 0),
                    'total_score': data.get('total_score', 0),
                    'mean_score': data.get('mean_score', data.get('class_average', 0)),
                    'distribution': data.get('distribution', {lvl: 0 for lvl in active_levels}),
                    'teacher_name': data.get('teacher_name', '—'),
                }

            # Build broadsheet rows from snapshot student_data
            _student_data = _snap.report_card_data
            students = Student.all_objects.filter(
                school=school, class_name=grade, stream=stream, is_active=True,
            ).order_by('admission_no')
            student_count = students.count()

            for student in students:
                s_data = _student_data.get(str(student.id)) or _student_data.get(student.id, {})
                if not s_data:
                    continue
                marks = s_data.get('marks', {})
                row_scores = []
                for code, short in published_subjects:
                    m = marks.get(code)
                    if m:
                        row_scores.append({'score': m['score'], 'level': m['level']})
                    else:
                        row_scores.append({'score': '-', 'level': '-'})
                broadsheet.append({
                    'student': student,
                    'scores': row_scores,
                    'tps': s_data.get('total_points', 0),
                    'total': s_data.get('total_marks', 0),
                    'plv': s_data.get('overall_plv', '-'),
                })

            broadsheet.sort(key=lambda x: (-x['total'], -x['tps']))
            analysis_rows = [
                {'short': short, **analysis_data[short]} for code, short in published_subjects
            ]
        else:
            # ── Fallback: live computation from Mark queries ──────────────
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

            _teacher_map = {}
            for a in SubjectAssignment.all_objects.filter(
                school=school, class_name=grade, stream=stream, is_active=True
            ).select_related('teacher_profile__user', 'subject'):
                code = a.subject.code if a.subject else None
                if code:
                    short = subject_label_map.get(code, subject_map.get(code, code))
                    analysis_data.setdefault(short, {
                        'entries': 0, 'total_score': 0, 'mean_score': 0.0,
                        'distribution': {lvl: 0 for lvl in active_levels},
                        'teacher_name': '—',
                    })
                    name = a.teacher_profile.get_full_title() if a.teacher_profile else ''
                    if name and name not in _teacher_map.setdefault(short, []):
                        _teacher_map[short].append(name)
            for short, names in _teacher_map.items():
                if names:
                    analysis_data[short]['teacher_name'] = ', '.join(names)

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
                for mark in dedup_marks_latest_by_code(student.cached_marks):
                    marks_dict[mark.subject.code] = mark
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
                            level, points = _get_primary_performance(m.score, school=school, section=section, sub_section=active_sub if is_primary else None, subject_id=m.subject_id) if is_primary else get_performance_level(
                                m.score, sub_section=active_sub,
                                subject_id=m.subject_id, school=school, section=section,
                            )
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
                    'plv':     calculate_primary_plv(total_marks, assessed_subjects, sub_section=active_sub if is_primary else None, school=school, section=section) if is_primary else calculate_broadsheet_plv(total_marks, total_points, sub_section=active_sub if is_primary else None, school=school, section=section),
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
    if grade in LOWER_PRIMARY_GRADE_CHOICES:
        section_accent = section_colors['LOWER_PRIMARY']
    elif grade in PRIMARY_GRADE_CHOICES:
        section_accent = section_colors['PRIMARY']
    elif grade in JSS_GRADE_CHOICES:
        section_accent = section_colors['JSS']
    else:
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

    # ── 3. Build Playwright-ready HTML ────────────────────────────────────────
    patched_html = _build_playwright_html(template_html, request, landscape=True)

    # ── 4. Generate PDF ──
    try:
        pdf_data = _generate_pdf(
            patched_html, landscape=True, engine='auto', scale=1.0,
            margins={'top': '8mm', 'right': '10mm', 'bottom': '18mm', 'left': '10mm'},
        )
    except Exception as e:
        _log_pdf_error('download_broadsheet_pdf', e, {
            'year': year, 'term': term, 'section': section,
            'grade': grade, 'stream': stream,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    # ── 5. Return as download or inline ────────────────────────────────────────
    if pdf_data.get('pdf'):
        filename = safe_pdf_filename('Results_List', grade, stream, exam_type, year)

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_merit_list_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_merit_list_pdf(request):
    """
    Premium merit-list PDF — same data and markup as the on-screen merit list
    (broadsheet_snippet.html), rendered with the browser print CSS composition,
    A4 landscape at scale 1.0, paginated with watermark + page numbers.
    """
    from ..models import Exam
    from .grading_engine import prefetch_school_grading
    from .reports import build_broadsheet_for_merit_list

    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    grade = request.GET.get('grade', '').strip()
    stream = request.GET.get('stream', '').strip()
    exam_id = request.GET.get('exam_id', '').strip()
    if not (grade and exam_id):
        return JsonResponse({'error': 'grade and exam_id are required.'}, status=400)

    is_admin = user_has_main_school_admin_override(request.user)
    section = get_request_school_section(request)
    prefetch_school_grading(school)

    # ── Section access for teachers (mirrors merit_list) ──────────────────────
    if not is_admin:
        if section == 'LOWER_PRIMARY':
            allowed_grades = LOWER_PRIMARY_GRADE_CHOICES
        elif section == 'PRIMARY':
            allowed_grades = PRIMARY_GRADE_CHOICES
        else:
            allowed_grades = JSS_GRADE_CHOICES
        if grade not in allowed_grades:
            return JsonResponse({'error': 'You do not have access to that grade section.'}, status=403)

    try:
        exam_object = Exam.all_objects.get(id=exam_id, school=school, is_deleted=False)
    except (Exam.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Selected exam not found.'}, status=404)

    if not is_admin:
        exam_section = exam_object.school_section or 'JSS'
        exam_sub = exam_object.sub_section
        if section == 'LOWER_PRIMARY' and not (exam_section == 'PRIMARY' and exam_sub == 'LOWER'):
            return JsonResponse({'error': 'Access denied for this exam section.'}, status=403)
        elif section == 'PRIMARY' and not (exam_section == 'PRIMARY' and exam_sub == 'UPPER'):
            return JsonResponse({'error': 'Access denied for this exam section.'}, status=403)
        elif section == 'JSS' and exam_section != 'JSS':
            return JsonResponse({'error': 'Access denied for this exam section.'}, status=403)

    # ── Build the same context as the merit list page ─────────────────────────
    try:
        context = build_broadsheet_for_merit_list(request, school, grade, stream, exam_object)
    except Exception as e:
        _log_pdf_error('download_merit_list_pdf', e, {
            'grade': grade, 'stream': stream, 'exam_id': exam_id,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    context['show_table'] = True
    context['selected_grade'] = grade
    context['selected_stream'] = stream
    context['selected_exam'] = exam_object.name

    # Grade-based accent/key (mirrors merit_list + both builder paths)
    section_colors = {
        'JSS':           '#305CDE',
        'PRIMARY':       '#00674F',
        'LOWER_PRIMARY': '#B45309',
    }
    if grade in LOWER_PRIMARY_GRADE_CHOICES:
        context['section_accent'] = section_colors['LOWER_PRIMARY']
        context['section_key'] = 'lower'
    elif grade in PRIMARY_GRADE_CHOICES:
        context['section_accent'] = section_colors['PRIMARY']
        context['section_key'] = 'primary'
    else:
        context['section_accent'] = section_colors['JSS']
        context['section_key'] = 'jss'

    try:
        from django.template.loader import render_to_string
        snippet = render_to_string(
            'students/partials/broadsheet_snippet.html', context, request=request,
        )
        patched_html = _build_standalone_print_html(
            snippet, request,
            title=f'{grade} {stream} Merit List - {exam_object.name}',
        )
        pdf_data = _generate_pdf(
            patched_html, landscape=True, engine='auto', scale=1.0,
            margins={'top': '8mm', 'right': '10mm', 'bottom': '18mm', 'left': '10mm'},
        )
    except Exception as e:
        _log_pdf_error('download_merit_list_pdf', e, {
            'grade': grade, 'stream': stream, 'exam_id': exam_id,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if not pdf_data.get('pdf'):
        return HttpResponse("Error generating report", status=500)

    filename = safe_pdf_filename(
        'Merit_List', grade, stream, exam_object.name, exam_object.year,
    )
    response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


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

    from ..models import Student
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

    # ── Build Playwright-ready HTML ──
    patched_html = _build_playwright_html(template_html, request, landscape=False)

    try:
        pdf_data = _generate_pdf(
            patched_html, landscape=False, engine='auto', scale=1.0,
            margins={'top': '5mm', 'right': '5mm', 'bottom': '8mm', 'left': '5mm'},
        )
    except Exception as e:
        _log_pdf_error('download_classlist_pdf', e, {
            'grade': grade_name, 'stream': stream_name,
            'view_mode': view_mode,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        year = datetime.date.today().year
        filename = safe_pdf_filename('Class_List', grade_name, stream_name, year)

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_score_sheet_pdf
# ==============================================================================
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_score_sheet_pdf(request):
    """
    Renders the Score Sheet (mark entry grid) and converts it to a
    premium PDF matching the class list / merit list standard.
    Params: grade (required), stream (optional), subject_id (optional).
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from ..models import Student, Subject, SubjectAssignment
    from django.db.models import IntegerField
    from django.db.models.functions import Substr, Length, Cast
    from .constants import RELIGION_SUBJECTS, RELIGION_TAG

    grade_name = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    subject_id = request.GET.get('subject_id', '').strip()

    if not grade_name:
        return JsonResponse({'error': 'grade parameter is required.'}, status=400)

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

    # Section access check — teachers can only download for their section
    is_admin_view = user_has_main_school_admin_override(request.user)
    if not is_admin_view and grade_name:
        section = get_request_school_section(request)
        from .constants import LOWER_PRIMARY_GRADE_CHOICES, PRIMARY_GRADE_CHOICES, JSS_GRADE_CHOICES
        if section == 'LOWER_PRIMARY' and grade_name not in LOWER_PRIMARY_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download score sheets for your section.", status=403)
        elif section == 'PRIMARY' and grade_name not in PRIMARY_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download score sheets for your section.", status=403)
        elif section == 'JSS' and grade_name not in JSS_GRADE_CHOICES:
            return HttpResponse("Access denied: you can only download score sheets for your section.", status=403)

    # ── Students — mirrors api_class_list (incl. religion-aware filtering) ──
    students_qs = Student.all_objects.filter(
        school=school, class_name=grade_name, is_active=True
    )
    if stream_name:
        students_qs = students_qs.filter(stream=stream_name)

    religion_tag = None
    if subject_id:
        try:
            subject_obj = Subject.all_objects.get(id=int(subject_id), school=school)
            if subject_obj.code in RELIGION_SUBJECTS:
                religion_tag = RELIGION_TAG.get(subject_obj.code, '')
        except (Subject.DoesNotExist, ValueError, TypeError):
            pass

    if religion_tag:
        tagged = students_qs.filter(religion=religion_tag)
        if tagged.exists():
            students_qs = tagged

    students_qs = (
        students_qs
        .annotate(adm_int=Cast(Substr('admission_no', 1, Length('admission_no') - 1), IntegerField()))
        .order_by('adm_int')
    )
    student_list = []
    for idx, s in enumerate(students_qs, start=1):
        student_list.append({
            'index': idx,
            'admission_no': s.admission_no or '',
            'name': s.name or '',
            'stream': s.stream or '',
        })

    # ── Subject label + assigned teacher (same data the page shows) ──
    subject_label = ''
    teacher_name = ''
    if subject_id:
        try:
            subject_obj = Subject.all_objects.get(id=int(subject_id), school=school)
            subject_label = subject_obj.name or ''
        except (Subject.DoesNotExist, ValueError, TypeError):
            subject_label = ''
        assignment = SubjectAssignment.all_objects.filter(
            school=school, class_name=grade_name, subject_id=subject_id, is_active=True
        )
        if stream_name:
            assignment = assignment.filter(stream=stream_name)
        assignment = assignment.select_related('teacher_profile__user').first()
        if assignment and assignment.teacher_profile and assignment.teacher_profile.user:
            teacher_name = (assignment.teacher_profile.user.get_full_name()
                            or assignment.teacher_profile.user.username)

    template_html = render_to_string('students/score_sheet_pdf.html', {
        'school':           school,
        'students':         student_list,
        'selected_grade':   grade_name,
        'selected_stream':  stream_name,
        'section_accent':   section_accent,
        'subject_label':    subject_label,
        'teacher_name':     teacher_name,
    }, request=request)

    patched_html = _build_playwright_html(template_html, request, landscape=False)

    try:
        pdf_data = _generate_pdf(
            patched_html, landscape=False, engine='auto', scale=1.0,
            margins={'top': '5mm', 'right': '5mm', 'bottom': '8mm', 'left': '5mm'},
        )
    except Exception as e:
        _log_pdf_error('download_score_sheet_pdf', e, {
            'grade': grade_name, 'stream': stream_name, 'subject_id': subject_id,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        year = datetime.date.today().year
        filename = safe_pdf_filename('Score_Sheet', grade_name, stream_name or subject_label, year)

        response = HttpResponse(pdf_data['pdf'], content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return HttpResponse("Error generating report", status=500)


# ==============================================================================
# download_individual_report_pdf / individual_report_print_html
# ==============================================================================

# Auto-print IIFE served with print-html (?view_mode=print triggers print()).
# Identical to the script embedded in report_card_print.html.
AUTO_PRINT_JS = '''<script>
(function() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('view_mode') !== 'print') { return; }

    var CLOSED = false;

    function safeClose() {
        if (CLOSED) return;
        CLOSED = true;
        try { window.close(); } catch(e) {}
    }

    function firePrint() {
        try {
            window.focus();
            window.print();
            var called = false;
            function afterPrintDone() {
                if (called) return;
                called = true;
                setTimeout(safeClose, 500);
            }
            window.addEventListener('afterprint', afterPrintDone);
            try {
                var mql = window.matchMedia('print');
                if (mql && mql.addEventListener) {
                    mql.addEventListener('change', function(e) { if (!e.matches) afterPrintDone(); });
                } else if (mql && mql.addListener) {
                    mql.addListener(function(e) { if (!e.matches) afterPrintDone(); });
                }
            } catch(e2) {}
        } catch (e) {
            console.error('Auto-print failed:', e);
        }
    }

    function start() {
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                setTimeout(firePrint, 600);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
        start();
    }

    setTimeout(safeClose, 20000);
})();
</script>
'''


def _build_report_popup_html(request, context, *, title, disable_chart_animation=False, auto_print=False):
    """
    Compose the exact document EDUNEXUSPrint's print popup writes for report
    cards (static/js/edunexus_print.js -> writePopup):

        print-shared.css + #pagePrintCSS rules + portrait @page overrides
        body = #reportCardsContainer > .rc-card-scroll > report card

    Same data context, same CSS recipe and the same Chart.js canvas as the
    on-screen card, so the downloaded PDF is identical to the print preview.
    """
    import re
    from django.template import Template as _InlineTemplate, RequestContext
    from django.utils.html import escape as html_escape

    # The browser wraps this fragment in `{% for student_data in student_marks_list %}`
    # (which also defines forloop used by the template) — render through the
    # same loop so the markup is identical.
    card_html = _InlineTemplate(
        '{% for student_data in student_marks_list %}{% include "students/report_card_content.html" %}{% endfor %}'
    ).render(RequestContext(request, context))
    card_html = _embed_logo_base64(card_html, request)

    # #pagePrintCSS payload — the stylesheet the popup reads off the page.
    raw = render_to_string('students/partials/report_card_print_css.html')
    m = re.search(r'<script[^>]*id="pagePrintCSS"[^>]*>(.*?)</script>', raw, re.S)
    page_css = m.group(1) if m else ''

    # Portrait block appended by writePopup for report-card selectors.
    portrait_overrides = (
        '\n@page { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }\n'
        '@page landscape { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }\n'
        '@media print {\n'
        '  @page { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }\n'
        '  .report-card { page-break-inside: avoid !important; break-inside: avoid !important; }\n'
        '  .report-card + .report-card { page-break-before: always !important; break-before: page !important; }\n'
        '  .rc-descriptors, .rc-descriptors-table, .footer-dates, .rc-remarks-grid {\n'
        '    page-break-inside: avoid !important; break-inside: avoid !important;\n'
        '  }\n'
        '}\n'
    )

    # Chart.js animates the line for ~1s; a PDF captured mid-animation would
    # show a half-drawn chart. Freeze it for PDF rendering only.
    anim_js = (
        '<script>try{if(window.Chart){Chart.defaults.animation=false;}}catch(e){}</script>\n'
        if disable_chart_animation else ''
    )

    shared_css = _load_print_css()
    base = request.build_absolute_uri('/')
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n'
        f'<title>{html_escape(title)}</title>\n'
        f'<base href="{base}">\n'
        '<script src="/static/js/chart.min.js"></script>\n'
        f'{anim_js}'
        f'<style id="pdf-override">{shared_css}\n{page_css}{portrait_overrides}</style>\n'
        '</head>\n<body>\n'
        '<div id="reportCardsContainer"><div class="rc-card-scroll">\n'
        f'{card_html}\n'
        '</div></div>\n'
        f'{AUTO_PRINT_JS if auto_print else ""}'
        '</body>\n</html>\n'
    )


@login_required(login_url='login')
@rate_limit("report_download", max_requests=10, window_seconds=60, methods=["GET", "POST"])
def download_individual_report_pdf(request, student_id):
    """
    Server-side PDF for individual report cards.

    Data comes from _build_individual_report_context — the exact context the
    on-screen card and the print popup render (cached ExamSummary position,
    PLV, totals, frozen comments). Layout comes from _build_report_popup_html,
    which reproduces the popup's CSS recipe. The downloaded PDF therefore
    matches the print preview and the browser view.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from .grading_engine import prefetch_school_grading
    prefetch_school_grading(school)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not student:
        return JsonResponse({'error': 'Student not found.'}, status=404)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        return JsonResponse({'error': 'You are not allowed to print report cards for this class stream.'}, status=403)

    is_admin_view = user_has_main_school_admin_override(request.user)
    context = _build_individual_report_context(request, school, student, is_admin_view)

    html = _build_report_popup_html(
        request, context,
        title=f'{student.name} Report Card',
        disable_chart_animation=True,
    )

    try:
        pdf_data = _generate_pdf(html, landscape=False, engine='auto', scale=1.0)
    except Exception as e:
        _log_pdf_error('download_individual_report_pdf', e, {
            'student_id': student_id, 'year': request.GET.get('year'),
            'term': request.GET.get('term'), 'assessment': request.GET.get('assessment'),
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    if pdf_data.get('pdf'):
        year = context.get('selected_year') or datetime.date.today().year
        term = context.get('selected_term') or 'Term 1'
        filename = safe_pdf_filename('Report_Card', student.name, year, term)

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
    Same data context and CSS recipe as download_individual_report_pdf but
    returns HTML instead of PDF. When the URL carries ?view_mode=print the
    page fires window.print() on load.
    """
    school = get_request_school(request)
    if not school:
        return JsonResponse({'error': 'School context is required.'}, status=400)

    from .grading_engine import prefetch_school_grading
    prefetch_school_grading(school)

    student = get_school_object_or_403(Student, request, using="all_objects", id=student_id)
    if not student:
        return JsonResponse({'error': 'Student not found.'}, status=404)
    if not user_can_access_class_stream(request.user, student.class_name, student.stream, require_class_teacher=True):
        return JsonResponse({'error': 'Not authorized.'}, status=403)

    is_admin_view = user_has_main_school_admin_override(request.user)
    context = _build_individual_report_context(request, school, student, is_admin_view)

    html = _build_report_popup_html(
        request, context,
        title=f'{student.name} Report Card',
        auto_print=True,
    )
    return HttpResponse(html, content_type='text/html; charset=utf-8')


# ==============================================================================
# download_bulk_report_pdf — Parallel PDF Stitching Engine
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=5, window_seconds=60, methods=["GET", "POST"])
def download_bulk_report_pdf(request):
    """
    Redirect to the async Celery-based bulk PDF generator.
    The synchronous version was removed to prevent OOM crashes when
    multiple teachers generate PDFs simultaneously.

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

    grade_name  = request.GET.get('grade', '').strip()
    stream_name = request.GET.get('stream', '').strip()
    exam_id     = request.GET.get('exam_id', '').strip()
    year        = request.GET.get('year', str(datetime.date.today().year))
    term        = request.GET.get('term', '').strip()
    ids_param   = request.GET.get('ids', '').strip()

    if not exam_id or not grade_name:
        return JsonResponse({'error': 'exam_id and grade are required.'}, status=400)

    # Build the redirect URL to the async endpoint
    from django.urls import reverse
    async_url = reverse('start_bulk_report_pdf')
    params = f'?grade={grade_name}&stream={stream_name}&exam_id={exam_id}&year={year}&term={term}'
    if ids_param:
        params += f'&ids={ids_param}'

    return HttpResponseRedirect(async_url + params)


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
    from .helpers import build_report_card_context_from_snapshot

    is_admin_view = user_has_main_school_admin_override(request.user)

    try:
        _exam = Exam.all_objects.get(id=exam_id, school=school, is_deleted=False)
    except (Exam.DoesNotExist, ValueError):
        return JsonResponse({'error': 'Exam not found.'}, status=404)

    try:
        _resolved = build_report_card_context_from_snapshot(
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

    from ..tasks import generate_bulk_report_pdf
    try:
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
    except Exception as broker_exc:
        # Broker down — don't 500; tell the client to retry shortly.
        logger.warning("generate_bulk_report_pdf broker unavailable: %s", broker_exc)
        return JsonResponse(
            {'error': 'PDF service is temporarily unavailable. Please retry in a moment.'},
            status=503,
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

    filename = result.get('filename') or safe_pdf_filename('Report_Cards')

    # Clean up cache
    pdf_cache.delete(f"pdf_data_{job_id}")
    pdf_cache.delete(f"pdf_result_{job_id}")

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_POST
def cancel_bulk_pdf(request, job_id):
    """
    POST /api/pdf-cancel/<job_id>/

    Sets a cancellation flag in the pdf_generation cache. The Celery worker
    checks ``pdf_cancel_<job_id>`` between chunks (see ``tasks.py``) and aborts
    the bulk-PDF job on the next iteration, sending a ``cancelled`` status
    back through the cache + websocket.
    """
    pdf_cache = _get_pdf_cache()

    # 24h TTL matches the longest realistic bulk job. The worker deletes the key
    # itself after consuming it (see tasks.py).
    pdf_cache.set(f"pdf_cancel_{job_id}", "1", timeout=60 * 60 * 24)

    return JsonResponse({
        'status': 'cancelling',
        'job_id': job_id,
        'message': 'Cancellation requested. The job will stop after the current chunk.',
    })
