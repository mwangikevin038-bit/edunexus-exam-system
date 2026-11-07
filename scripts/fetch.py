"""Fetch one URL after logging in and print any Django traceback."""
import http.cookiejar
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"

# Test credentials live in the environment so they never enter git history.
USERNAME = os.environ.get("EDUNEXUS_TEST_USER", "")
PASSWORD = os.environ.get("EDUNEXUS_TEST_PASSWORD", "")
if not (USERNAME and PASSWORD):
    raise SystemExit(
        "Set EDUNEXUS_TEST_USER and EDUNEXUS_TEST_PASSWORD in the environment "
        "before running this script (test logins are intentionally not in git)."
    )
url = sys.argv[1] if len(sys.argv) > 1 else "/profile/"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def open(req_or_url, note=""):
    try:
        resp = opener.open(req_or_url)
        return resp.read().decode("utf-8", "replace"), resp.status, resp.url
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"[{note or 'request'}] HTTP {exc.code} -> {exc.url}")
        return body, exc.code, exc.url


def traceback_of(body):
    if "Traceback (most recent call last)" not in body:
        return None
    start = body.index("Traceback (most recent call last)")
    end = body.index("</pre>", start) if "</pre>" in body[start:] else len(body)
    return body[start:end]


# Prime CSRF, then log in.
open(f"{BASE}/login/", "login GET")
token = next((c.value for c in jar if c.name == "csrftoken"), "")
data = urllib.parse.urlencode(
    {"username": USERNAME, "password": PASSWORD, "csrfmiddlewaretoken": token}
).encode()
req = urllib.request.Request(
    f"{BASE}/login/", data=data, headers={"Referer": f"{BASE}/login/"}
)
body, status, _ = open(req, "login POST")
print("login status:", status)
tb = traceback_of(body)
if tb:
    print("LOGIN TRACEBACK:\n" + tb)
    raise SystemExit(1)

body, status, final = open(BASE + url, "target")
print("status:", status, "->", final)
tb = traceback_of(body)
print(tb if tb else body[:400])
