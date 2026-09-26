"""
Playwright PDF Engine — Headless Chrome PDF renderer for pixel-perfect output.

Renders Django templates to PDF using the same HTML/CSS/JS engine as the browser,
guaranteeing 100% visual fidelity with the on-screen view.

Usage:
    from students.pdf_engine import render_html_to_pdf, render_url_to_pdf

    # From HTML string
    pdf_bytes = render_html_to_pdf(html_string, landscape=False)

    # From URL (requires Django server running)
    pdf_bytes = render_url_to_pdf("http://localhost:8000/report/1/print-html/")
"""

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger('pdf_engine')

# ── Singleton Browser Manager ──────────────────────────────────────────────
# Chromium is expensive to launch (~2-3s cold start). We keep a single browser
# process alive and create lightweight contexts per render. Each context is an
# isolated "incognito" tab that shares the browser process.

_browser = None
_browser_lock = threading.Lock()
_loop = None
_loop_thread = None


def _get_event_loop():
    """Get or create a dedicated event loop running in a background thread."""
    global _loop, _loop_thread
    if _loop is not None and _loop.is_running():
        return _loop

    if sys.platform == 'win32':
        # Playwright spawns Chromium via asyncio subprocesses, which on
        # Windows require a Proactor loop. Django's runtime imports install
        # the Selector policy (no subprocess support), so build a Proactor
        # loop explicitly WITHOUT mutating the global policy.
        _loop = asyncio.WindowsProactorEventLoopPolicy().new_event_loop()
    else:
        _loop = asyncio.new_event_loop()
    _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
    _loop_thread.start()
    return _loop


async def _ensure_browser():
    """Launch Chromium if not already running."""
    global _browser
    if _browser is not None and _browser.is_connected():
        return _browser

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch(
        headless=True,
        args=[
            '--disable-gpu',
            '--disable-dev-shm-usage',
            '--no-sandbox',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-default-apps',
            '--disable-sync',
            '--disable-translate',
            '--metrics-recording-only',
            '--mute-audio',
            '--no-first-run',
        ],
    )
    logger.info("[pdf_engine] Chromium launched (headless)")
    return _browser


def _static_css_path(filename):
    """Resolve a static CSS file against the project root (settings.BASE_DIR)."""
    try:
        from django.conf import settings
        base = str(settings.BASE_DIR)
    except Exception:
        base = None
    if not base:
        base = os.environ.get('DJANGO_BASE_DIR') or str(Path(__file__).resolve().parent.parent)
    return Path(base) / 'static' / 'css' / filename


def _load_print_css():
    """Load the shared print CSS from disk (cached after first read)."""
    css_path = _static_css_path('print-shared.css')
    try:
        return css_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.error("[pdf_engine] print-shared.css not found at %s", css_path)
        return ''


def _load_weasyprint_css():
    """Load the WeasyPrint-compatible CSS from disk (cached after first read)."""
    css_path = _static_css_path('print-weasyprint.css')
    try:
        return css_path.read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.error("[pdf_engine] print-weasyprint.css not found at %s", css_path)
        return ''


_PRINT_CSS_CACHE = None
_WEASY_CSS_CACHE = None


def get_print_css():
    global _PRINT_CSS_CACHE
    if _PRINT_CSS_CACHE is None:
        _PRINT_CSS_CACHE = _load_print_css()
    return _PRINT_CSS_CACHE


def get_weasyprint_css():
    global _WEASY_CSS_CACHE
    if _WEASY_CSS_CACHE is None:
        _WEASY_CSS_CACHE = _load_weasyprint_css()
    return _WEASY_CSS_CACHE


# ── Public API ─────────────────────────────────────────────────────────────

async def _render_html_to_pdf_async(
    html_string,
    *,
    landscape=False,
    margins=None,
    base_url=None,
    timeout_ms=30000,
    scale=0.75,
    footer_template=None,
):
    """
    Render an HTML string to PDF bytes using headless Chromium.

    Args:
        html_string: Complete HTML document string.
        landscape: If True, use A4 landscape orientation.
        margins: Optional dict with top/bottom/left/right margin strings (e.g. {"top": "10mm"}).
        base_url: Optional base URL for resolving relative resource paths.
        timeout_ms: Timeout in milliseconds for page rendering.
        scale: Page scale factor (1.0 = browser-view parity, 0.75 = fit more).
        footer_template: Optional Chrome header/footer HTML fragment (uses
            .pageNumber / .totalNumber placeholders). When set, the footer is
            rendered into the bottom margin area.

    Returns:
        PDF bytes, or None on failure.
    """
    browser = await _ensure_browser()

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        device_scale_factor=2,  # 2x for crisp text rendering
    )

    try:
        page = await context.new_page()

        # Set base URL for resolving relative paths (static files, images)
        if base_url:
            await page.set_content(
                html_string,
                wait_until='networkidle',
                timeout=timeout_ms,
            )
        else:
            await page.set_content(
                html_string,
                wait_until='networkidle',
                timeout=timeout_ms,
            )

        # Brief settle delay — matplotlib SVGs render synchronously via
        # set_content, but networkidle + a short pause ensures fonts and
        # any deferred layout are flushed before PDF capture.
        try:
            await page.wait_for_timeout(100)
        except Exception:
            pass

        # Generate PDF
        pdf_kwargs = {
            'format': 'A4',
            'print_background': True,
            'prefer_css_page_size': True,
            'scale': scale,
        }

        if landscape:
            pdf_kwargs['landscape'] = True

        if footer_template:
            # Chrome renders header/footer into the margin area — the bottom
            # margin must be >= ~14mm for the footer text to fit.
            pdf_kwargs['display_header_footer'] = True
            pdf_kwargs['footer_template'] = footer_template

        if margins:
            pdf_kwargs['margin'] = margins
        else:
            # Default margins match our @page CSS. When a footer is shown,
            # give it enough room (>= 14mm) so it never clips.
            pdf_kwargs['margin'] = {
                'top': '5mm',
                'bottom': '16mm' if footer_template else '8mm',
                'left': '5mm',
                'right': '5mm',
            }

        pdf_bytes = await page.pdf(**pdf_kwargs)
        return pdf_bytes

    except Exception as e:
        logger.error("[pdf_engine] PDF render failed: %s", str(e), exc_info=True)
        return None
    finally:
        await context.close()


def render_html_to_pdf(
    html_string,
    *,
    landscape=False,
    margins=None,
    base_url=None,
    timeout_ms=30000,
    scale=0.75,
    footer_template=None,
):
    """
    Synchronous wrapper: render HTML string to PDF bytes.

    Blocks the current thread until the async render completes.
    Uses a dedicated event loop in a background thread to avoid
    blocking the main thread.
    """
    loop = _get_event_loop()
    future = asyncio.run_coroutine_threadsafe(
        _render_html_to_pdf_async(
            html_string,
            landscape=landscape,
            margins=margins,
            base_url=base_url,
            timeout_ms=timeout_ms,
            scale=scale,
            footer_template=footer_template,
        ),
        loop,
    )
    try:
        return future.result(timeout=(timeout_ms / 1000) + 10)
    except Exception as e:
        logger.error("[pdf_engine] render_html_to_pdf timed out or failed: %s", str(e))
        return None


async def _render_url_to_pdf_async(
    url,
    *,
    landscape=False,
    margins=None,
    timeout_ms=30000,
):
    """
    Render a live URL to PDF using headless Chromium.
    The URL must be reachable from the Chromium process (localhost).
    """
    browser = await _ensure_browser()
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        device_scale_factor=2,
    )

    try:
        page = await context.new_page()
        await page.goto(url, wait_until='networkidle', timeout=timeout_ms)

        # Wait for Chart.js
        try:
            await page.wait_for_timeout(800)
        except Exception:
            pass

        pdf_kwargs = {
            'format': 'A4',
            'print_background': True,
            'prefer_css_page_size': True,
            'scale': 0.75,
        }
        if landscape:
            pdf_kwargs['landscape'] = True
        if margins:
            pdf_kwargs['margin'] = margins
        else:
            pdf_kwargs['margin'] = {
                'top': '5mm',
                'bottom': '8mm',
                'left': '5mm',
                'right': '5mm',
            }

        return await page.pdf(**pdf_kwargs)

    except Exception as e:
        logger.error("[pdf_engine] URL render failed: %s", str(e), exc_info=True)
        return None
    finally:
        await context.close()


def render_url_to_pdf(url, *, landscape=False, margins=None, timeout_ms=30000):
    """Synchronous wrapper for render_url_to_pdf_async."""
    loop = _get_event_loop()
    future = asyncio.run_coroutine_threadsafe(
        _render_url_to_pdf_async(url, landscape=landscape, margins=margins, timeout_ms=timeout_ms),
        loop,
    )
    try:
        return future.result(timeout=(timeout_ms / 1000) + 10)
    except Exception as e:
        logger.error("[pdf_engine] render_url_to_pdf failed: %s", str(e))
        return None


def warm_up():
    """
    Pre-warm Chromium browser on Django startup.
    Call this from AppConfig.ready() or in a startup signal.
    """
    try:
        loop = _get_event_loop()
        future = asyncio.run_coroutine_threadsafe(_ensure_browser(), loop)
        future.result(timeout=15)
        logger.info("[pdf_engine] Browser warmed up successfully")
    except Exception as e:
        logger.warning("[pdf_engine] Browser warm-up failed (will retry on first render): %s", str(e))


def shutdown():
    """Clean up browser on Django shutdown."""
    global _browser
    if _browser is not None:
        try:
            loop = _get_event_loop()
            future = asyncio.run_coroutine_threadsafe(_browser.close(), loop)
            future.result(timeout=5)
        except Exception:
            pass
        _browser = None
    if _loop is not None:
        _loop.call_soon_threadsafe(_loop.stop)
