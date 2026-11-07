"""Load every page that uses {% sub_nav %} and report the rendered bar.

Usage: python scripts/verify_subnav.py
"""
import os
import pathlib
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

# url -> note (which template's bar we expect to see)
TARGETS = [
    ("/profile/", "profile"),
    ("/password-change/", "profile"),
    ("/school-admin/classes/", "classes"),
    ("/school-admin/add-new-class/", "classes"),
    ("/school-admin/registration/", "students"),
    ("/school-admin/comments/?section=JSS", "comment_sections"),
    ("/printouts/analysis-report/", "printouts"),
    ("/class-lists/", "class_lists"),
    ("/dashboard/", "overview"),
    ("/school-admin/", "overview_admin"),
    ("/school-admin/faculty/", "faculty"),
    ("/school-admin/exams/", "exams"),
    ("/school-admin/exams/?tab=create", "exams"),
    ("/school-admin/exams/?tab=grading", "exams"),
    ("/school-admin/exams/?tab=deleted", "exams"),
    ("/printouts/merit-list/", "results|printouts"),
    ("/printouts/", "printouts"),
    ("/school-admin/exams/publish-results/", "exams"),
    ("/report-cards/", "results"),
    ("/report-card-lookup/", "results"),
    ("/results/", "results"),
    ("/school-admin/school-settings/", "settings"),
    ("/school-admin/term-dates/", "settings"),
    ("/printouts/score-sheet/", "printouts"),
    ("/select-exam/", "exams|score_capture"),
    ("/printouts/teachers-list/", "printouts"),
    ("/teacher-class-comments/", "results"),
    ("/teacher-report-forms/", "results"),
    ("/class-lists/search/", "class_lists"),
    ("/printouts/report-forms/", "printouts"),
    ("/printouts/report-forms/display/", "printouts"),
]


def login(page):
    page.goto(f"{BASE}/login/", wait_until="networkidle")
    page.fill('input[name="username"], input[type="text"]', USERNAME)
    page.fill('input[name="password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        login(page)
        for url, note in TARGETS:
            try:
                resp = page.goto(BASE + url, wait_until="networkidle", timeout=20000)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR  {url}\n        {exc}")
                failures += 1
                continue
            status = resp.status if resp else 0
            if status != 200:
                print(f"  {status}  {url}")
                failures += 1
                continue
            if page.locator("text=Traceback (most recent call last)").count():
                print(f"  500? {url}  (traceback on page)")
                failures += 1
                continue
            items = page.eval_on_selector_all(
                ".sub-nav-link",
                "els => els.map(e => (e.classList.contains('active') ? '-> ' : '   ') +"
                " e.textContent.replace(/\\s+/g, ' ').trim())",
            )
            if not items:
                print(f"  NONE {url}   [{note}]")
                failures += 1
                continue
            print(f"  ok   {url}   [{note}]")
            for it in items:
                print(f"           {it}")
        browser.close()
    print(f"\n{failures} problem(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
