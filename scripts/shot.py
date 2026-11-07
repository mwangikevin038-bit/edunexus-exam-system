"""
Capture desktop + mobile screenshots of key pages for visual regression checks.

Usage:
    python scripts/shot.py                     # all pages, both viewports
    python scripts/shot.py dashboard exams     # only matching pages
    python scripts/shot.py --desktop-only      # skip mobile
"""
import os
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OUT = pathlib.Path(__file__).resolve().parent.parent / "shots"

# Test credentials live in the environment so they never enter git history.
#   PowerShell: $env:EDUNEXUS_TEST_USER='...'; $env:EDUNEXUS_TEST_PASSWORD='...'
#   bash:       export EDUNEXUS_TEST_USER=... EDUNEXUS_TEST_PASSWORD=...
USERNAME = os.environ.get("EDUNEXUS_TEST_USER", "")
PASSWORD = os.environ.get("EDUNEXUS_TEST_PASSWORD", "")
if not (USERNAME and PASSWORD):
    raise SystemExit(
        "Set EDUNEXUS_TEST_USER and EDUNEXUS_TEST_PASSWORD in the environment "
        "before running this script (test logins are intentionally not in git)."
    )

# Rendered in a fresh, unauthenticated context (an authed visit redirects).
PUBLIC = {"login"}

# name -> path
PAGES = {
    "welcome": "/",
    "login": "/login/",
    "dashboard": "/school-admin/",
    "teacher_dashboard": "/dashboard/",
    "students": "/school-admin/registration/",
    "classes": "/school-admin/classes/",
    "exams": "/school-admin/exams/",
    "faculty": "/school-admin/faculty/",
    "comments": "/school-admin/comments/",
    "printouts": "/printouts/",
    "settings": "/school-admin/school-settings/",
    "results": "/results/",
    "report_cards": "/report-cards/",
    "capture": "/select-exam/",
    "class_lists": "/class-lists/",
}

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


def login(page):
    page.goto(f"{BASE}/login/", wait_until="networkidle")
    page.fill('input[name="username"], input[type="text"]', USERNAME)
    page.fill('input[name="password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    filters = [a for a in sys.argv[1:] if not a.startswith("--")]
    desktop_only = "--desktop-only" in sys.argv
    mobile_only = "--mobile-only" in sys.argv
    viewports = VIEWPORTS
    if desktop_only:
        viewports = {"desktop": VIEWPORTS["desktop"]}
    if mobile_only:
        viewports = {"mobile": VIEWPORTS["mobile"]}

    OUT.mkdir(exist_ok=True)
    pages = {k: v for k, v in PAGES.items() if not filters or any(f in k for f in filters)}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for vp_name, vp in viewports.items():
            # Public pages (login) must render without a session — an
            # authenticated GET /login/ redirects to the dashboard.
            pub = {k: v for k, v in pages.items() if k in PUBLIC}
            if pub:
                ctx = browser.new_context(viewport=vp, device_scale_factor=1)
                page = ctx.new_page()
                capture(page, pub, vp_name)
                ctx.close()
            authed = {k: v for k, v in pages.items() if k not in PUBLIC}
            if authed:
                ctx = browser.new_context(viewport=vp, device_scale_factor=1)
                page = ctx.new_page()
                login(page)
                capture(page, authed, vp_name)
                ctx.close()
        browser.close()
    print(f"\nSaved to {OUT}")


def capture(page, pages, vp_name):
    for name, path in pages.items():
        try:
            page.goto(BASE + path, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(600)
            dest = OUT / f"{name}_{vp_name}.png"
            page.screenshot(path=str(dest))
            print(f"  ok  {dest.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR {name} [{vp_name}]: {exc}")


if __name__ == "__main__":
    main()
