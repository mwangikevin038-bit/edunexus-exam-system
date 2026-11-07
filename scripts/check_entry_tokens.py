"""Prove entry-mobile.css no longer leaks its emerald tokens into the page.

Reports the computed value of the conflicting canonical tokens (must equal
variables.css) and the sheet's private tokens (must keep their emerald values),
at both viewports.

Usage: python scripts/check_entry_tokens.py
"""
import os

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
URL = "/select-exam/"

# Test credentials live in the environment so they never enter git history.
USERNAME = os.environ.get("EDUNEXUS_TEST_USER", "")
PASSWORD = os.environ.get("EDUNEXUS_TEST_PASSWORD", "")
if not (USERNAME and PASSWORD):
    raise SystemExit(
        "Set EDUNEXUS_TEST_USER and EDUNEXUS_TEST_PASSWORD in the environment "
        "before running this script (test logins are intentionally not in git)."
    )

JS = """
() => {
  const s = getComputedStyle(document.documentElement);
  const g = n => (s.getPropertyValue(n) || '').trim();
  return {
    en: {green: g('--en-green'), bg: g('--en-bg'), text: g('--en-text'),
         radius: g('--en-radius'), surface: g('--en-surface')},
    ec: {green: g('--ec-green'), bg: g('--ec-bg'), radius: g('--ec-radius'),
         spring: g('--ec-spring')},
    avatar: (() => {
      const a = document.querySelector('.header-user-avatar, .avatar, '
        + '.admin-avatar, [class*=avatar]');
      return a ? getComputedStyle(a).backgroundColor : null;
    })(),
  };
}
"""


def login(page):
    page.goto(f"{BASE}/login/", wait_until="networkidle")
    page.fill('input[name="username"], input[type="text"]', USERNAME)
    page.fill('input[name="password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def main():
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, vp in (("desktop", {"width": 1440, "height": 900}),
                          ("mobile", {"width": 390, "height": 844})):
            page = browser.new_context(viewport=vp).new_page()
            login(page)
            page.goto(BASE + URL, wait_until="networkidle")
            got = page.evaluate(JS)
            print(f"\n== {label} ==")
            for k, v in got["en"].items():
                print(f"   --en-{k:<10} {v}")
            for k, v in got["ec"].items():
                print(f"   --ec-{k:<9} {v}")
            print(f"   avatar bg        {got['avatar']}")

            # assertions
            en_green = got["en"]["green"].lower()
            if en_green in ("#24e55e", "rgb(36, 229, 94)"):
                print(f"   FAIL --en-green still emerald: {en_green}")
                problems += 1
            elif en_green in ("#4caf50", "rgb(76, 175, 80)"):
                print("   ok   --en-green is system green")
            else:
                print(f"   FAIL --en-green unexpected: {en_green!r}")
                problems += 1

            ec_green = got["ec"]["green"].lower()
            if ec_green in ("#24e55e", "rgb(36, 229, 94)"):
                print("   ok   --ec-green still emerald (sheet unchanged)")
            else:
                print(f"   FAIL --ec-green lost its value: {ec_green!r}")
                problems += 1
            page.close()
        browser.close()
    print(f"\n{problems} problem(s)")
    return problems


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
