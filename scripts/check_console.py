"""Report console errors and failed requests on key pages.

Usage: python scripts/check_console.py [url-substring filters]
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
# Test credentials live in the environment so they never enter git history.
USERNAME = os.environ.get("EDUNEXUS_TEST_USER", "")
PASSWORD = os.environ.get("EDUNEXUS_TEST_PASSWORD", "")
if not (USERNAME and PASSWORD):
    raise SystemExit(
        "Set EDUNEXUS_TEST_USER and EDUNEXUS_TEST_PASSWORD in the environment "
        "before running this script (test logins are intentionally not in git)."
    )

PAGES = [
    ("welcome", "/"),
    ("dashboard", "/school-admin/"),
    ("teacher_dashboard", "/dashboard/"),
    ("students", "/school-admin/registration/"),
    ("classes", "/school-admin/classes/"),
    ("exams", "/school-admin/exams/"),
    ("faculty", "/school-admin/faculty/"),
    ("comments", "/school-admin/comments/"),
    ("printouts", "/printouts/"),
    ("settings", "/school-admin/school-settings/"),
    ("results", "/results/"),
    ("report_cards", "/report-cards/"),
    ("capture", "/select-exam/"),
    ("class_lists", "/class-lists/"),
    ("profile", "/profile/"),
]


def login(page):
    page.goto(f"{BASE}/login/", wait_until="networkidle")
    page.fill('input[name="username"], input[type="text"]', USERNAME)
    page.fill('input[name="password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    filters = [a for a in sys.argv[1:] if not a.startswith("--")]
    problems = 0
    issues = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.on("console", lambda m: issues.append(f"console.error: {m.text}")
                if m.type == "error" else None)
        page.on("requestfailed", lambda r: issues.append(f"failed: {r.url}"))
        page.on("response", lambda r: issues.append(f"HTTP {r.status}: {r.url}")
                if r.status >= 400 else None)
        login(page)
        for name, path in PAGES:
            if filters and not any(f in name for f in filters):
                continue
            del issues[:]
            page.goto(BASE + path, wait_until="networkidle", timeout=20000)
            page.wait_for_timeout(400)
            # Exercise the shell: open/close the mobile menu path, click nothing risky.
            page.evaluate("() => { if (window.showLoader) window.showLoader(true);"
                          " if (window.hideLoader) window.hideLoader(); }")
            page.wait_for_timeout(150)
            # de-duplicate while preserving order
            seen, unique = set(), []
            for i in issues:
                if i not in seen:
                    seen.add(i)
                    unique.append(i)
            # Static 404s for optional assets are noise only if they are ours
            if unique:
                problems += len(unique)
                print(f"  !  {name}")
                for i in unique:
                    print(f"       {i}")
            else:
                print(f"  ok {name}")
        browser.close()
    print(f"\n{problems} issue(s)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
