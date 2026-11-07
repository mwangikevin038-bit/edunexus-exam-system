"""Smoke-test the pages whose {% sub_nav %} branches are not reachable as the
default admin session (needs real exam / grade / stream / teacher IDs).

Usage: python scripts/check_pages.py
"""
import os
import re
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school.settings")
sys.path.insert(0, os.getcwd())

# Test credentials live in the environment so they never enter git history.
USERNAME = os.environ.get("EDUNEXUS_TEST_USER", "")
PASSWORD = os.environ.get("EDUNEXUS_TEST_PASSWORD", "")
if not (USERNAME and PASSWORD):
    raise SystemExit(
        "Set EDUNEXUS_TEST_USER and EDUNEXUS_TEST_PASSWORD in the environment "
        "before running this script (test logins are intentionally not in git)."
    )

import django  # noqa: E402

django.setup()

from django.test import Client  # noqa: E402

from students import models as m  # noqa: E402

LINK_RE = re.compile(
    r'<a href="([^"]*)" class="sub-nav-link([^"]*)">(.*?)</a>', re.S
)
LABEL_RE = re.compile(r"<[^>]+>")


def bar(html):
    out = []
    for href, cls, inner in LINK_RE.findall(html):
        label = " ".join(LABEL_RE.sub(" ", inner).split())
        out.append(("-> " if " active" in cls else "   ") + f"{label}  [{href}]")
    return out


def first(model, **filters):
    try:
        return model.objects.filter(**filters).first()
    except Exception:  # noqa: BLE001
        return None


exam = m.Exam.objects.order_by("id").first() if hasattr(m, "Exam") else None
grade = m.Grade.objects.order_by("id").first() if hasattr(m, "Grade") else None
stream = None
if grade is not None:
    try:
        stream = m.Stream.objects.filter(grade=grade).first()
    except Exception:  # noqa: BLE001
        stream = None

targets = []
if exam is not None:
    targets += [
        (f"/school-admin/exams/edit/?exam_id={exam.pk}", "edit_exam"),
        (f"/school-admin/exams/analyse/?exam_id={exam.pk}", "exams_config"),
        (f"/school-admin/exams/analyse/?exam_id={exam.pk}", "exams_config [is_admin]"),
        (f"/school-admin/exams/review-submission/?exam_id={exam.pk}", "exams"),
        (f"/school-admin/exams/publish-results/?exam_id={exam.pk}", "exams"),
    ]
if grade is not None:
    targets.append((f"/school-admin/classes/{grade.pk}/streams/", "classes"))
if grade is not None and stream is not None:
    targets.append(
        (f"/school-admin/classes/{grade.pk}/streams/{stream.name}/subjects/", "classes")
    )

print(f"exam={getattr(exam, 'pk', None)} grade={getattr(grade, 'pk', None)} "
      f"stream={getattr(stream, 'pk', None)}")

teacher_user = None
if hasattr(m, "Teacher"):
    teacher = m.Teacher.objects.select_related("user").exclude(user=None).first()
    teacher_user = getattr(teacher, "user", None)

c = Client()
if not c.login(username=USERNAME, password=PASSWORD):
    print("admin login failed")
    raise SystemExit(1)

failures = 0
for url, note in targets:
    r = c.get(url, follow=True)
    rows = bar(r.content.decode("utf-8", "replace"))
    flag = "ok  " if r.status_code == 200 else f"{r.status_code} "
    if r.status_code != 200 or not rows:
        failures += 1
        flag = "FAIL" if r.status_code == 200 else flag
    print(f"{flag} {url}   [{note}]")
    for row in rows:
        print(f"        {row}")

if teacher_user is not None:
    print(f"\n-- as teacher {teacher_user.username} --")
    for url in ("/results/", "/report-card-lookup/", "/teacher-class-comments/",
                "/teacher-report-forms/", "/printouts/merit-list/"):
        tc = Client()
        # Teacher accounts may share the test password, or set their own.
        teacher_password = os.environ.get("EDUNEXUS_TEST_TEACHER_PASSWORD", PASSWORD)
        if not tc.login(username=teacher_user.username, password=teacher_password):
            print(f"     (cannot log in {teacher_user.username})")
            break
        r = tc.get(url, follow=True)
        rows = bar(r.content.decode("utf-8", "replace"))
        if r.status_code != 200 or not rows:
            failures += 1
            print(f"FAIL {url}  status={r.status_code} links={len(rows)}")
        else:
            print(f"ok   {url}")
            for row in rows:
                print(f"        {row}")

print(f"\n{failures} problem(s)")
sys.exit(1 if failures else 0)
