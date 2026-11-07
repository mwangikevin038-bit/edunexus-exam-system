"""Render every {% sub_nav %} variant through Django's template engine.

Covers code paths a browser session cannot reach (teacher branches, only=/active=
combinations) and proves every preset's {% url %} resolves.

Usage: python scripts/check_presets.py
"""
import os
import re
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school.settings")
sys.path.insert(0, os.getcwd())

import django  # noqa: E402

django.setup()

from django.template import Context, Template  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from students.templatetags.nav import PRESETS  # noqa: E402

request = RequestFactory().get("/")
LINK = re.compile(r'<a href="([^"]*)" class="sub-nav-link([^"]*)">\s*'
                  r'<i class="ph [^"]*"></i>\s*([^<]+?)\s*</a>', re.S)


class Exam:
    id = 42


def render(source, **extra):
    ctx = Context({"request": request, "section": "JSS", "exam": Exam(), **extra})
    return Template("{% load nav %}" + source).render(ctx)


def rows(html):
    return [(href.strip(), "active" in cls, label.strip())
            for href, cls, label in LINK.findall(html)]


failures = []

# 1. Default rendering + every explicit active label + active="" + only=
for name, items in PRESETS.items():
    try:
        got = rows(render(f'{{% sub_nav "{name}" %}}'))
    except Exception as exc:  # noqa: BLE001
        failures.append(f'{name}: default render failed: {exc}')
        continue

    want = [label for _, label, _ in items]
    if [r[2] for r in got] != want:
        failures.append(f"{name}: labels { [r[2] for r in got] } != {want}")
    if got and not all(r[1] for r in got[:1]):
        failures.append(f"{name}: first item not active by default")
    if any(r[1] for r in got[1:]):
        failures.append(f"{name}: non-first item active by default")

    for _, label, _ in items:
        try:
            got2 = rows(render(f'{{% sub_nav "{name}" active="{label}" %}}'))
        except Exception as exc:  # noqa: BLE001
            failures.append(f'{name}: active="{label}" failed: {exc}')
            continue
        act = [r[2] for r in got2 if r[1]]
        if act != [label]:
            failures.append(f'{name}: active="{label}" -> {act}')

        try:
            got3 = rows(render(f'{{% sub_nav "{name}" only="{label}" %}}'))
        except Exception as exc:  # noqa: BLE001
            failures.append(f'{name}: only="{label}" failed: {exc}')
            continue
        if [r[2] for r in got3] != [label]:
            failures.append(f'{name}: only="{label}" -> {[r[2] for r in got3]}')

    try:
        got4 = rows(render(f'{{% sub_nav "{name}" active="" %}}'))
    except Exception as exc:  # noqa: BLE001
        failures.append(f'{name}: active="" failed: {exc}')
        continue
    if any(r[1] for r in got4):
        failures.append(f'{name}: active="" still highlighted {[r[2] for r in got4 if r[1]]}')

    # every href must be absolute and non-empty
    for href, _, label in got:
        if not href.startswith("/") or href.rstrip("/").endswith("?"):
            failures.append(f'{name}/{label}: bad href {href!r}')

# 2. Errors the tag is supposed to raise
for bad, why in [
    ('{% sub_nav "nope" %}', "unknown preset"),
    ('{% sub_nav "results" active="Nope" %}', "unknown active"),
    ('{% sub_nav "results" only="Nope" %}', "empty only"),
]:
    try:
        render(bad)
        failures.append(f"expected an error for {why}: {bad}")
    except Exception:  # noqa: BLE001
        pass

# 3. score_capture href depends on section
for section, want in (("JSS", "/select-exam/"), ("PRIMARY", "/select-exam-primary/")):
    html = Template("{% load nav %}{% sub_nav \"score_capture\" %}").render(
        Context({"request": request, "section": section})
    )
    href = rows(html)[0][0]
    if href != want:
        failures.append(f"score_capture section={section} -> {href} != {want}")

# 4. edit_exam href carries the exam id
html = render('{% sub_nav "edit_exam" %}')
if rows(html)[0][0] != "/school-admin/exams/edit/?exam_id=42":
    failures.append(f"edit_exam href -> {rows(html)[0][0]!r}")

if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print(f"ok: {len(PRESETS)} presets x active/only variants rendered cleanly")
