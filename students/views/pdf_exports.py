"""
PDF export views for broadsheet results and class list registers.

Uses Playwright (headless Chromium) to render Django templates to PDF,
applying screen-emulated CSS overrides so the output matches the web view.
"""

import base64
import asyncio
import contextlib
import datetime
import json
import logging
import mimetypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Prefetch, Q, Sum
from django.db.models import IntegerField
from django.db.models.functions import Cast
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils.text import slugify
from playwright.sync_api import sync_playwright

from .constants import ASSESSMENT_MAP, GRADE_CHOICES, LOWER_PRIMARY_GRADE_CHOICES, LOWER_PRIMARY_SUBJECT_NAMES, LOWER_PRIMARY_SUBJECT_SHORT_MAP, ORDERED_LEVELS, PRIMARY_PERF_LEVELS, PRIMARY_SUBJECT_NAMES, PRIMARY_SUBJECT_SHORT_MAP, SUBJECT_DISPLAY_ORDER, SUBJECT_SHORT_MAP, get_streams_for_school, sort_subjects
from .reports import PRIMARY_ORDERED_LEVELS, _grading_config_for
from .exams import _get_primary_performance
from .helpers import (
    calculate_broadsheet_plv,
    calculate_primary_plv,
    calculate_report_plv,
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
# BULLETPROOF PLAYWRIGHT PDF INFRASTRUCTURE
# ==============================================================================
# DO NOT modify this section unless you are fixing a Playwright breakage.
# All 3 PDF views (broadsheet, class list, individual report) use
# _generate_pdf() as their single entry point. This ensures:
#   - Consistent retry / cleanup / error handling
#   - Concurrency limited to 2 browsers (RAM safety)
#   - Event loop policy restored for Windows compatibility
#   - Browser always closed even on error
#   - Startup verification fails fast if Chromium is missing
# ==============================================================================

# Limit concurrent Playwright browser instances to prevent RAM exhaustion.
# Each Chromium instance uses ~200-500MB. Configurable via PDF_MAX_CONCURRENT env var.
_pdf_max_concurrent = int(os.environ.get('PDF_MAX_CONCURRENT', '2'))
_pdf_semaphore = threading.Semaphore(_pdf_max_concurrent)
# Maximum seconds a request will wait for the semaphore before failing fast
_pdf_semaphore_timeout = int(os.environ.get('PDF_SEMAPHORE_TIMEOUT', '120'))

# Verified once at import time — False means Chromium is not installed.
_playwright_ok = True


_playwright_checked = False


def _verify_playwright():
    """
    Lazily verify Playwright and Chromium are installed on first PDF request.
    Sets _playwright_ok = False on failure so all subsequent calls fail fast.
    Only runs once per process.
    """
    global _playwright_ok, _playwright_checked
    if _playwright_checked:
        return
    _playwright_checked = True
    try:
        with _playwright_session(), sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        logger.info("[pdf] Playwright Chromium verified OK")
    except Exception as e:
        _playwright_ok = False
        logger.error(
            "[pdf] Playwright verification FAILED: %s\n"
            "[pdf] Run: pip install playwright && python -m playwright install chromium",
            str(e),
        )


@contextlib.contextmanager
def _playwright_session():
    """
    Context manager that creates an ISOLATED event loop for this execution.

    Instead of modifying the global asyncio event loop policy (which causes
    race conditions under concurrent requests), we create a fresh ProactorEventLoop
    for this thread only and clean it up afterward.

    Django Channels/Daphne overrides the Windows event loop policy to
    SelectorEventLoop, which does not support subprocess creation.
    Playwright needs ProactorEventLoop to spawn Chromium.
    """
    if sys.platform != "win32":
        yield
        return

    # Create an isolated event loop for this execution (never touches global policy)
    loop = asyncio.ProactorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        try:
            loop.close()
        except Exception:
            pass
        # Restore whatever the current thread had before (thread-local)
        try:
            old_loop = asyncio._get_running_loop()
        except AttributeError:
            old_loop = None
        if old_loop is None:
            # No running loop — we can safely unset
            asyncio.set_event_loop(None)


def _kill_chromium_processes():
    """
    Hard-kill all orphaned Chromium processes on Windows.
    Called after timeout or error to prevent zombie browser memory leaks.
    """
    if sys.platform != 'win32':
        return
    try:
        # Kill all chromium.exe and chrome.exe processes (Playwright's browser)
        for proc_name in ('chromium.exe', 'chrome.exe'):
            subprocess.run(
                ['taskkill', '/F', '/IM', proc_name],
                capture_output=True, timeout=5,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        logger.info("[pdf] Orphaned Chromium processes cleaned up")
    except Exception as e:
        logger.warning("[pdf] Process cleanup failed: %s", str(e))


class _DisconnectMonitor:
    """
    Thread-safe signal that bridges the Django response lifecycle with the
    background Playwright generation thread.

    Lifecycle:
      1. View creates monitor, passes to _generate_pdf() and _stream_pdf_from_bytes()
      2. _generate() periodically calls monitor.abort_if_disconnected() — if the
         client has disconnected, it raises _ClientDisconnected to abort Playwright
         immediately instead of running to completion.
      3. When StreamingHttpResponse.close() fires (client disconnect OR normal
         completion), it calls monitor.signal_disconnected() which sets the event
         and hard-kills any still-running Chromium processes.
      4. The streaming iterator calls monitor.abort_if_disconnected() before every
         chunk write to detect BrokenPipe before it happens.
    """

    def __init__(self):
        self._event = threading.Event()
        self._browser = None      # mutable reference to active Chromium browser
        self._lock = threading.Lock()

    def signal_disconnected(self):
        """Called by response.close() — signals the generation thread to abort."""
        self._event.set()
        # Hard-kill any Chromium that's still running
        browser = self._browser
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        _kill_chromium_processes()

    def abort_if_disconnected(self):
        """Called by _generate() or the streaming iterator — raises if client is gone."""
        if self._event.is_set():
            raise _ClientDisconnected("Client disconnected — aborting PDF generation")

    def is_disconnected(self):
        """Non-raising check. Returns True if the client has disconnected."""
        return self._event.is_set()

    def set_browser(self, browser):
        """Store a reference to the active Playwright browser for cleanup."""
        with self._lock:
            self._browser = browser

    def clear_browser(self):
        with self._lock:
            self._browser = None


class _ClientDisconnected(Exception):
    """Raised inside _generate() when the client disconnects mid-generation."""
    pass


def _generate_pdf(
    patched_html,
    *,
    viewport=None,
    landscape=False,
    margin=None,
    wait_for_charts=False,
    wait_for_logo_selector=None,
    timeout=90,
    retries=2,
    disconnect_monitor=None,
):
    """
    Bulletproof Playwright PDF generation — single entry point for all views.

    - Acquires semaphore to limit concurrency (max 2 browsers).
    - Runs in a ThreadPoolExecutor with strict timeout.
    - On timeout/error, HARD-KILLS all Chromium processes (no zombies).
    - On client disconnect, aborts immediately via disconnect_monitor.
    - Retries up to `retries` times on transient failures.
    - ALWAYS closes the browser, even on error.

    Returns:
        bytes: The PDF content.

    Raises:
        TimeoutError: If all attempts time out.
        RuntimeError: If Playwright is not available or all attempts fail.
        _ClientDisconnected: If the client disconnects mid-generation (not retried).
    """
    if not _playwright_ok:
        raise RuntimeError(
            "Playwright Chromium is not installed. "
            "Run: pip install playwright && python -m playwright install chromium"
        )

    _verify_playwright()

    if viewport is None:
        viewport = {"width": 1200, "height": 900}

    if margin is None:
        margin = {"top": "0.5in", "right": "0.3in", "bottom": "0.5in", "left": "0.3in"}

    last_error = None

    for attempt in range(retries + 1):
        if not _pdf_semaphore.acquire(timeout=_pdf_semaphore_timeout):
            return JsonResponse({
                'error': 'Server busy: too many PDF requests in progress. Please wait a moment and try again.',
                'retry_after': _pdf_semaphore_timeout,
            }, status=503, headers={'Retry-After': str(_pdf_semaphore_timeout)})
        try:
            result = {}
            error_holder = [None]

            def _generate():
                try:
                    with _playwright_session(), sync_playwright() as pw:
                        browser = pw.chromium.launch(
                            headless=True,
                            args=[
                                '--no-sandbox',
                                '--disable-dev-shm-usage',
                                '--disable-service-workers',
                                '--js-flags="--max-old-space-size=512"',
                            ],
                        )
                        if disconnect_monitor:
                            disconnect_monitor.set_browser(browser)
                        try:
                            # Use browser context with device_scale_factor for crisp charts
                            context = browser.new_context(device_scale_factor=2)
                            pg = context.new_page()

                            # ── Block service workers to prevent stale cache ──
                            pg.route("**/sw.js", lambda route: route.abort())
                            pg.route("**/sw.prod.js", lambda route: route.abort())

                            # Also disable service worker registration via init script
                            context.add_init_script("""
                                if (typeof navigator.serviceWorker !== 'undefined') {
                                    Object.defineProperty(navigator, 'serviceWorker', {
                                        value: undefined,
                                        writable: false,
                                    });
                                }
                            """)

                            pg.set_viewport_size(viewport)
                            pg.emulate_media(media="print")
                            pg.set_content(patched_html, wait_until="domcontentloaded")

                            # Check if client disconnected during content load
                            if disconnect_monitor:
                                disconnect_monitor.abort_if_disconnected()

                            # Wait for web fonts to load
                            try:
                                pg.wait_for_function("document.fonts.ready", timeout=5000)
                            except Exception:
                                pass  # Fonts may be unavailable — continue anyway

                            # Wait for Chart.js canvases if present
                            if wait_for_charts:
                                # Step 1: Wait for Chart.js library to load
                                try:
                                    pg.wait_for_function("() => typeof Chart !== 'undefined'", timeout=10000)
                                except Exception:
                                    # Chart.js may not have loaded from <script src> — inject directly from disk
                                    try:
                                        from django.contrib.staticfiles.finders import find as static_find
                                        chart_js_path = static_find('js/chart.umd.min.js')
                                        if chart_js_path and os.path.exists(chart_js_path):
                                            with open(chart_js_path, 'r', encoding='utf-8') as f:
                                                chart_js_source = f.read()
                                            pg.evaluate(chart_js_source)
                                            pg.wait_for_function("() => typeof Chart !== 'undefined'", timeout=5000)
                                    except Exception:
                                        pass

                                # Step 2: Wait for all Chart instances to finish rendering
                                # Uses a dual-check: JS hook flag OR Chart.getChart() DOM verification
                                try:
                                    pg.wait_for_function("""
                                        () => {
                                            // Check for explicit JS hook (preferred)
                                            if (window.allChartsRendered === true) return true;
                                            // Fallback: verify all canvas charts have data
                                            const canvases = document.querySelectorAll('canvas[id^="chart-"]');
                                            if (canvases.length === 0) return true;
                                            for (const canvas of canvases) {
                                                const chart = Chart.getChart(canvas);
                                                if (!chart || !chart.data || !chart.data.datasets || chart.data.datasets.length === 0) {
                                                    return false;
                                                }
                                            }
                                            return true;
                                        }
                                    """, timeout=15000)
                                except Exception:
                                    # Fallback: brief delay if chart detection fails
                                    pg.wait_for_timeout(500)

                            # Check if client disconnected during chart rendering
                            if disconnect_monitor:
                                disconnect_monitor.abort_if_disconnected()

                            # Wait for a specific image to finish loading (e.g. school logo)
                            if wait_for_logo_selector:
                                try:
                                    pg.wait_for_function(f"""
                                        () => {{
                                            const el = document.querySelector('{wait_for_logo_selector}');
                                            return !el || (el.complete && el.naturalWidth > 0);
                                        }}
                                    """, timeout=5000)
                                except Exception:
                                    pass

                            # Final paint delay — minimal since charts are verified
                            pg.wait_for_timeout(100)

                            # Final disconnect check before generating PDF bytes
                            if disconnect_monitor:
                                disconnect_monitor.abort_if_disconnected()

                            result['pdf'] = pg.pdf(
                                format="A4",
                                landscape=landscape,
                                print_background=True,
                                display_header_footer=False,
                                margin=margin,
                                prefer_css_page_size=True,
                            )
                        finally:
                            if disconnect_monitor:
                                disconnect_monitor.clear_browser()
                            # Explicit teardown: page → context → browser (innermost first)
                            try:
                                pg.close()
                            except Exception:
                                pass
                            try:
                                context.close()
                            except Exception:
                                pass
                            try:
                                browser.close()
                            except Exception:
                                pass
                except Exception as e:
                    error_holder[0] = e

            # ── Run with ThreadPoolExecutor + strict timeout ──
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_generate)
                try:
                    future.result(timeout=timeout)
                except FuturesTimeoutError:
                    # Timeout — hard-kill all Chromium processes
                    last_error = TimeoutError(f"PDF generation timed out after {timeout}s")
                    logger.warning(
                        "[pdf] Attempt %d/%d timed out — killing orphaned processes",
                        attempt + 1, retries + 1,
                    )
                    _kill_chromium_processes()
                    if attempt < retries:
                        time.sleep(1)
                        continue
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "[pdf] Attempt %d/%d failed: %s",
                        attempt + 1, retries + 1, str(last_error),
                    )
                    # Also kill processes on any error to prevent zombies
                    _kill_chromium_processes()
                    if attempt < retries:
                        time.sleep(1)
                        continue
                    break

            # Check if the inner function raised an error
            if error_holder[0] is not None:
                last_error = error_holder[0]
                # Client disconnect — abort immediately, do NOT retry
                if isinstance(last_error, _ClientDisconnected):
                    logger.info("[pdf] Client disconnected during generation — aborting")
                    _kill_chromium_processes()
                    raise last_error
                logger.warning(
                    "[pdf] Attempt %d/%d failed: %s",
                    attempt + 1, retries + 1, str(last_error),
                )
                # Also kill processes on any error to prevent zombies
                _kill_chromium_processes()
                if attempt < retries:
                    time.sleep(1)
                    continue
                break

            if 'pdf' not in result:
                last_error = TimeoutError(f"PDF generation timed out after {timeout}s")
                logger.warning(
                    "[pdf] Attempt %d/%d timed out", attempt + 1, retries + 1,
                )
                _kill_chromium_processes()
                if attempt < retries:
                    time.sleep(1)
                    continue
                break

            pdf_bytes = result['pdf']
            if not pdf_bytes or len(pdf_bytes) < 1000:
                last_error = ValueError(f"PDF too small ({len(pdf_bytes or b'')} bytes) — charts likely failed to render")
                logger.warning(
                    "[pdf] Attempt %d/%d produced empty/tiny PDF (%d bytes)",
                    attempt + 1, retries + 1, len(pdf_bytes or b''),
                )
                if attempt < retries:
                    time.sleep(1)
                    continue
                break

            return pdf_bytes

        finally:
            _pdf_semaphore.release()

    # All attempts exhausted
    raise RuntimeError(f"PDF generation failed after {retries + 1} attempts: {last_error}")


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


def _stream_pdf_from_bytes(pdf_bytes, filename, content_disposition='attachment',
                           disconnect_monitor=None, gateway_timeout=60):
    """
    Write PDF bytes to a temporary disk file, then return a StreamingHttpResponse
    that streams the file in 64 KB chunks.

    Guarantees:
      - Temp file is ALWAYS deleted (finally block), even on BrokenPipe / disconnect.
      - Every chunk write is preceded by a disconnect check — if the client is gone,
        we stop immediately instead of writing to a dead socket.
      - A Gateway-Timeout header tells upstream proxies to abort after `gateway_timeout`
        seconds if the response stalls.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    try:
        tmp.write(pdf_bytes)
        tmp.flush()
        tmp_path = tmp.name
    finally:
        tmp.close()

    closed = [False]

    def _cleanup_tmp():
        if closed[0]:
            return
        closed[0] = True
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    def _file_iterator():
        try:
            with open(tmp_path, 'rb') as f:
                while True:
                    # Pre-flight: abort if client disconnected
                    if disconnect_monitor:
                        disconnect_monitor.abort_if_disconnected()

                    try:
                        chunk = f.read(65536)  # 64 KB chunks
                    except (OSError, IOError) as e:
                        logger.warning("[pdf] Read error during streaming: %s", e)
                        break

                    if not chunk:
                        break

                    yield chunk
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            logger.info("[pdf] Client disconnected during streaming: %s", e)
            if disconnect_monitor:
                disconnect_monitor.signal_disconnected()
        except _ClientDisconnected:
            logger.info("[pdf] Disconnect monitor triggered during streaming")
            if disconnect_monitor:
                disconnect_monitor.signal_disconnected()
        except Exception as e:
            logger.warning("[pdf] Unexpected error during streaming: %s", e)
        finally:
            _cleanup_tmp()

    def _close_callback():
        """Called by Django when the response is closed (client disconnect or finish)."""
        _cleanup_tmp()
        if disconnect_monitor:
            disconnect_monitor.signal_disconnected()

    response = StreamingHttpResponse(_file_iterator(), content_type='application/pdf')
    response['Content-Disposition'] = f'{content_disposition}; filename="{filename}"'
    response['Content-Length'] = str(len(pdf_bytes))
    response['X-Gateway-Timeout'] = str(gateway_timeout)
    response.close = _close_callback
    return response


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
        students      = Student.all_objects.filter(school=school, class_name=grade, stream=stream).prefetch_related(marks_prefetch)
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

    # ── 4. Playwright — PRINT media so template's @media print CSS activates ──
    disconnect_monitor = _DisconnectMonitor()
    try:
        pdf_bytes = _generate_pdf(
            patched_html,
            viewport={"width": 1094, "height": 765},
            landscape=True,
            margin={"top": "0.15in", "right": "0.15in", "bottom": "0.15in", "left": "0.15in"},
            disconnect_monitor=disconnect_monitor,
        )
    except _ClientDisconnected:
        return HttpResponse(status=499)  # Client closed connection
    except Exception as e:
        _log_pdf_error('download_broadsheet_pdf', e, {
            'year': year, 'term': term, 'section': section,
            'grade': grade, 'stream': stream,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    # ── 5. Return as download or inline ────────────────────────────────────────
    slug_grade  = slugify(grade  or "class")
    slug_stream = slugify(stream or "stream")
    current_year = datetime.date.today().year
    filename    = f"{slug_grade}_{slug_stream}_Premium_Results_List_{year or current_year}.pdf"

    mode = request.GET.get('mode', 'download').strip().lower()
    disposition = 'inline' if mode == 'inline' else 'attachment'
    return _stream_pdf_from_bytes(pdf_bytes, filename, content_disposition=disposition,
                                  disconnect_monitor=disconnect_monitor)


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
    @bottom-center {{
      content: "GENERATED FROM EDUNEXUS EXAM SYSTEM @2026";
      font-family: "Times New Roman", Times, serif;
      font-size: 10pt;
      font-weight: 700;
      color: rgba(0, 0, 0, 0.55);
      text-transform: uppercase;
      letter-spacing: 0.4pt;
    }}
  }}
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'

    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    disconnect_monitor = _DisconnectMonitor()
    try:
        pdf_bytes = _generate_pdf(
            patched_html,
            viewport={"width": 794, "height": 1123},
            landscape=False,
            margin={"top": "0.62in", "right": "0.38in", "bottom": "0.72in", "left": "0.5in"},
            wait_for_logo_selector='.sheet-logo',
            disconnect_monitor=disconnect_monitor,
        )
    except _ClientDisconnected:
        return HttpResponse(status=499)
    except Exception as e:
        _log_pdf_error('download_classlist_pdf', e, {
            'grade': selected_grade, 'stream': selected_stream,
            'context': selected_key, 'view_mode': view_mode,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    slug_grade  = slugify(selected_grade  or "class")
    slug_stream = slugify(selected_stream or "stream")
    year = datetime.date.today().year
    filename = f"{slug_grade}_{slug_stream}_Class_List_{year}.pdf"

    mode = request.GET.get('mode', 'download').strip().lower()
    disposition = 'inline' if mode == 'inline' else 'attachment'
    return _stream_pdf_from_bytes(pdf_bytes, filename, content_disposition=disposition,
                                  disconnect_monitor=disconnect_monitor)


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
    )
    marks = sorted(marks, key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))
    total_marks  = sum(m.score  for m in marks if m.score)
    total_points = sum(m.points for m in marks if m.points)

    class_scores = (
        Mark.all_objects.filter(
            school=school,
            student__class_name=student.class_name, student__stream=student.stream,
            year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs,
        )
        .values('student_id').annotate(total_score=Sum('score')).order_by('-total_score')
    )
    sorted_ids  = [item['student_id'] for item in class_scores]
    class_count = len(sorted_ids)
    try:
        position = sorted_ids.index(student.id) + 1
    except ValueError:
        position = 0

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

    disconnect_monitor = _DisconnectMonitor()
    try:
        pdf_bytes = _generate_pdf(
            patched_html,
            viewport={"width": 794, "height": 1123},
            landscape=False,
            margin={"top": "0.5in", "right": "0.3in", "bottom": "0.5in", "left": "0.3in"},
            wait_for_charts=True,
            disconnect_monitor=disconnect_monitor,
        )
    except _ClientDisconnected:
        return HttpResponse(status=499)
    except Exception as e:
        _log_pdf_error('download_individual_report_pdf', e, {
            'student_id': student_id, 'year': year, 'term': term,
            'assessment': assessment,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    student_name = slugify(f"{student.first_name}_{student.last_name}" if student.first_name or student.last_name else student.admission_number)
    filename = f"{student_name}_Report_Card_{year}_{slugify(term)}.pdf"

    mode = request.GET.get('mode', 'download').strip().lower()
    disposition = 'inline' if mode == 'inline' else 'attachment'
    return _stream_pdf_from_bytes(pdf_bytes, filename, content_disposition=disposition,
                                  disconnect_monitor=disconnect_monitor)


# ==============================================================================
# download_bulk_report_pdf
# ==============================================================================

@login_required(login_url='login')
@rate_limit("report_download", max_requests=5, window_seconds=60, methods=["GET", "POST"])
def download_bulk_report_pdf(request):
    """
    Server-side bulk report card PDF via Playwright.
    Accepts the same GET parameters as bulk_report_cards view
    (ids, year, term, assessment) and renders all cards into one PDF.
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
        'marks',
        queryset=Mark.all_objects.filter(
            school=school, year=year, term=term, exam_type=db_assessment,
            subject__in=published_subjects_qs, school_section=sample.school_section,
        ),
        to_attr='cached_marks',
    )
    selected_students = selected_students_base.prefetch_related(marks_prefetch)

    class_scores = (
        Mark.all_objects.filter(
            school=school, student__class_name=sample.class_name, student__stream=sample.stream,
            year=year, term=term, exam_type=db_assessment, subject__in=published_subjects_qs,
        )
        .values('student_id').annotate(total_score=Sum('score')).order_by('-total_score')
    )
    class_leaderboard = [item['student_id'] for item in class_scores]
    total_class_count = len(class_leaderboard)

    class_subject_avgs = (
        Mark.all_objects.filter(
            school=school, student__class_name=sample.class_name, student__stream=sample.stream,
            year=year, term=term, exam_type=db_assessment, subject__in=published_subjects_qs,
        )
        .exclude(is_absent=True)
        .values('subject__code')
        .annotate(avg_score=Avg('score'))
    )
    class_avg_map = {row['subject__code']: round(row['avg_score'], 1) for row in class_subject_avgs}

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

    freeze_threshold = datetime.timedelta(days=30)
    now = datetime.datetime.now(datetime.timezone.utc)

    student_marks_list = []
    for student in selected_students:
        marks = sorted(student.cached_marks, key=lambda m: SUBJECT_DISPLAY_ORDER.get(m.subject.code, 99))
        total_marks  = sum(m.score  for m in marks if m.score)
        total_points = sum(m.points for m in marks if m.points)

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

        chart_data_json = json.dumps({
            'labels':    [m.subject_name for m in marks if not m.is_absent],
            'student':   [m.score for m in marks if not m.is_absent],
            'class_avg': [class_avg_map.get(m.subject.code, 0) for m in marks if not m.is_absent],
        })

        try:
            position = class_leaderboard.index(student.id) + 1
        except ValueError:
            position = 0

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

        student_marks_list.append({
            'student':              student,
            'marks':                marks,
            'total_marks':          total_marks,
            'total_points':         total_points,
            'overall_plv':          overall_plv,
            'mean_points':          mean_points,
            'mean_points_max':      max_points_per_subj,
            'max_total_marks':      assessed_subjects * 100,
            'max_total_points':     assessed_subjects * max_points_per_subj,
            'grade_descriptors':    grade_descriptors,
            'chart_data_json':      chart_data_json,
            'class_teacher_remark': class_teacher_remark,
            'class_teacher_name':   class_teacher_name,
            'headteacher_comment':  headteacher_comment,
            'closing_date':         closing_date,
            'opening_date':         opening_date,
            'position':             position,
            'class_count':          total_class_count,
        })

    student_marks_list.sort(key=lambda x: (x['position'] == 0, x['position']))

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

    template_html = render_to_string('students/bulk_report_cards_pdf.html', {
        'student_marks_list': student_marks_list,
        'selected_year':      year,
        'selected_term':      term,
        'selected_assessment': db_assessment,
        'class_count':        total_class_count,
        'closing_date':       master_comment.closing_date if master_comment else None,
        'opening_date':       master_comment.opening_date if master_comment else None,
        'section_accent':     section_accent,
    }, request=request)

    template_html = _embed_logo_base64(template_html, request)

    pdf_css = f"""
<style id="pdf-override">
  * {{ -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
  .rv-shell, .rv-header, .rv-hero, .rv-actions, .rv-scroll,
  .sidebar, .sidebar-overlay, .sidebar-header, .sidebar-footer, .sidebar-nav, .sidebar-user, nav, header, .mobile-topbar, .hamburger-btn,
  .global-loader-overlay, .bottom-nav, .d-print-none, .topbar,
  .btn-print, .btn-print-action, .control-panel, .button-group,
  .date-picker-group, .rv-badge,
  .mobile-menu-sheet, .mobile-menu-panel, .mobile-menu-body, .mobile-menu-header, .mobile-menu-backdrop,
  .system-footer {{ display: none !important; visibility: hidden !important; height: 0 !important; overflow: hidden !important; }}
  html, body {{ margin: 0 !important; padding: 0 !important; background: white !important; overflow: visible !important; font-family: 'Times New Roman', Times, serif !important; font-size: 12pt !important; }}
  .container-fluid {{ margin: 0 !important; padding: 0 !important; max-width: none !important; width: 100% !important; }}
  #reportCardsContainer {{ display: block !important; width: 100% !important; margin: 0 !important; padding: 0 !important; }}
  .report-card {{
    display: flex !important; page-break-after: always !important; break-after: always !important;
    margin: 0 !important; width: 7.4in !important; overflow: hidden !important; border: none !important;
    border-left: 12px solid {section_accent} !important; position: relative !important;
    font-family: 'Times New Roman', Times, serif !important; font-size: 12pt !important;
    box-sizing: border-box !important; padding: 0.12in 0.35in 0.2in !important;
  }}
  .report-card:last-child {{ page-break-after: auto !important; break-after: auto !important; }}
  .report-content {{ display: flex !important; flex-direction: column !important; flex: 1 !important; gap: 8px !important; }}
  .report-logo, .rc-logo-placeholder {{ width: 78px !important; height: 78px !important; }}
  .rc-logo-spacer {{ width: 78px !important; }}
  .rc-logo-placeholder {{ font-size: 30px !important; }}
  .rc-schoolinfo h1 {{ font-size: 16pt !important; margin: 0 0 2px !important; color: {section_accent} !important; }}
  .rc-schoolinfo .rc-tagline {{ font-size: 8pt !important; margin-bottom: 3px !important; }}
  .rc-schoolinfo .rc-address {{ font-size: 11pt !important; margin-bottom: 1px !important; }}
  .rc-schoolinfo .rc-contact-line {{ font-size: 9pt !important; }}
  .rc-header {{ gap: 12px !important; padding-bottom: 7px !important; border-bottom: 3px solid {section_accent} !important; }}
  .rc-banner {{ padding: 6px 8px !important; font-size: 11pt !important; }}
  .rc-top-grid {{ gap: 16px !important; }}
  .rc-photo-placeholder {{ width: 58px !important; height: 58px !important; font-size: 22px !important; border-radius: 8px !important; }}
  .rc-student-name {{ font-size: 14pt !important; margin-bottom: 4px !important; }}
  .rc-detail {{ font-size: 11pt !important; margin-bottom: 3px !important; }}
  .rc-chart-title {{ font-size: 10pt !important; margin-bottom: 4px !important; }}
  .rc-chart-block {{ padding: 7px !important; }}
  .rc-chart-block canvas {{ max-width: 100% !important; }}
  .rc-stats {{ gap: 8px !important; }}
  .rc-stat {{ padding: 8px 8px !important; border-top: 3px solid {section_accent} !important; }}
  .rc-stat-label {{ font-size: 9pt !important; margin-bottom: 3px !important; }}
  .rc-stat-value {{ font-size: 14pt !important; }}
  .table-scroll {{ overflow: visible !important; }}
  .rc-table td {{ padding: 4px 6px !important; font-size: 11pt !important; line-height: 1.15 !important; }}
  .rc-table thead th {{ padding: 5px 6px !important; font-size: 10pt !important; }}
  .rc-remarks-grid {{ gap: 14px !important; }}
  .rc-remark-box {{ padding: 9px 12px !important; }}
  .rc-remark-title {{ font-size: 10pt !important; margin-bottom: 4px !important; color: {section_accent} !important; }}
  .rc-remark-author {{ font-size: 10pt !important; font-weight: 700 !important; color: #000000 !important; margin-bottom: 5px !important; }}
  .rc-remark-text {{ font-size: 12pt !important; min-height: 30px !important; margin-bottom: 6px !important; line-height: 1.2 !important; }}
  .rc-signature {{ font-size: 10pt !important; padding-top: 4px !important; }}
  .rc-descriptors-title {{ font-size: 9pt !important; margin-bottom: 3px !important; }}
  .rc-descriptors-table th, .rc-descriptors-table td {{ padding: 3px 4px !important; font-size: 9pt !important; }}
  .footer-dates {{ display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 30px !important; padding-top: 8px !important; margin-top: auto !important; }}
  .date-box {{ font-size: 10pt !important; padding-bottom: 3px !important; border-bottom: 2px solid {section_accent} !important; }}
  .system-footer {{ display: none !important; }}
  .rc-print-watermark {{ display: none !important; }}
  .rc-table, .rc-table th, .rc-table td, .rc-stat, .rc-descriptors-table, .rc-descriptors-table th, .rc-descriptors-table td {{ border-color: #000 !important; }}
  @page {{ size: A4 portrait; margin: 0.12in 0.35in 0.4in 0.35in; }}
</style>
"""

    pdf_base_tag = f'<base href="{request.build_absolute_uri("/")}">'
    patched_html = _inject_pdf_css(template_html, pdf_css, pdf_base_tag)

    disconnect_monitor = _DisconnectMonitor()
    try:
        student_count = len(student_marks_list)
        per_student_timeout = max(8, 300 // max(student_count, 1))
        pdf_timeout = max(600, per_student_timeout * student_count + 120)
        pdf_bytes = _generate_pdf(
            patched_html,
            viewport={"width": 794, "height": 1123},
            landscape=False,
            margin={"top": "0.12in", "right": "0.35in", "bottom": "0.4in", "left": "0.35in"},
            wait_for_charts=True,
            timeout=pdf_timeout,
            retries=2,
            disconnect_monitor=disconnect_monitor,
        )
    except _ClientDisconnected:
        return HttpResponse(status=499)
    except Exception as e:
        _log_pdf_error('download_bulk_report_pdf', e, {
            'student_count': student_count, 'year': year, 'term': term,
            'assessment': assessment, 'class': sample.class_name,
            'stream': sample.stream,
        })
        return JsonResponse({'error': f'PDF generation failed: {str(e)}'}, status=500)

    grade_slug = slugify(sample.class_name or "class")
    stream_slug = slugify(sample.stream or "stream")
    filename = f"Bulk_Report_Cards_{grade_slug}_{stream_slug}_{year}_{slugify(term)}.pdf"

    mode = request.GET.get('mode', 'download').strip().lower()
    disposition = 'inline' if mode == 'inline' else 'attachment'
    return _stream_pdf_from_bytes(pdf_bytes, filename, content_disposition=disposition,
                                  disconnect_monitor=disconnect_monitor)
