"""Collapse the 38 copy-pasted sub-nav blocks into {% sub_nav %} calls.

Each file's replacements are listed in block order (top to bottom).
Run: python scripts/migrate_subnav.py
"""
import pathlib
import re

ROOT = pathlib.Path("students/templates")
NAV_RE = re.compile(r'[ \t]*<nav class="sub-nav-bar dm-subnav">.*?</nav>\n?', re.S)

# filename -> replacement for each block, in document order
PLAN = {
    "password_change_form.html": ['{% sub_nav "profile" %}'],
    "add_new_class.html": ['{% sub_nav "classes" %}'],
    "admin_add_student.html": ['{% sub_nav "students" %}'],
    "admin_comments.html": ['{% sub_nav "comment_sections" active="" %}'],
    "analyse_exam.html": [
        '{% if is_admin %}{% sub_nav "exams_config" %}'
        '{% else %}{% sub_nav "exams_config" only="Manage Exams" %}{% endif %}'
    ],
    "analysis_report.html": ['{% sub_nav "printouts" %}'],
    "class_lists.html": ['{% sub_nav "class_lists" %}', '{% sub_nav "printouts" %}'],
    "dashboard.html": ['{% sub_nav "overview" %}', '{% sub_nav "overview" %}'],
    "dashboard_admin.html": [
        '{% sub_nav "overview_admin" %}',
        '{% sub_nav "overview_admin" %}',
    ],
    "edit_exam.html": ['{% sub_nav "edit_exam" %}'],
    "manage_classes.html": ['{% sub_nav "classes" %}'],
    "manage_faculty.html": ['{% sub_nav "faculty" %}'],
    "manage_streams.html": ['{% sub_nav "classes" %}'],
    "manage_subjects.html": ['{% sub_nav "classes" %}'],
    "manage_exams.html": ['{% sub_nav "exams" %}'],
    "merit_list.html": [
        '{% if is_teacher_view %}{% sub_nav "results" %}'
        '{% else %}{% sub_nav "printouts" %}{% endif %}'
    ],
    "printouts_hub.html": ['{% sub_nav "printouts" %}'],
    "profile.html": ['{% sub_nav "profile" %}'],
    "publish_results_overview.html": ['{% sub_nav "exams" %}'],
    "report_card.html": [
        '{% if is_admin_view %}{% sub_nav "printouts" %}'
        '{% else %}{% sub_nav "results" active="Report Cards" %}{% endif %}'
    ],
    "report_card_lookup.html": [
        '{% if is_admin_view %}{% sub_nav "printouts" %}'
        '{% else %}{% sub_nav "results" %}{% endif %}'
    ],
    "report_card_select.html": ['{% sub_nav "results" active="Report Cards" %}'],
    "report_forms.html": ['{% sub_nav "printouts" %}'],
    "review_submission.html": ['{% sub_nav "exams" %}'],
    "results_list.html": ['{% sub_nav "results" %}'],
    "school_settings.html": ['{% sub_nav "settings" %}'],
    "score_sheet.html": ['{% sub_nav "printouts" %}'],
    "select_exam_details.html": [
        '{% sub_nav "exams" active="" %}',
        '{% sub_nav "score_capture" %}',
    ],
    "stream_subject_detail.html": ['{% sub_nav "exams" %}'],
    "teachers_list_printout.html": ['{% sub_nav "printouts" %}'],
    "teacher_class_comments.html": ['{% sub_nav "results" %}'],
    "teacher_report_forms.html": ['{% sub_nav "results" %}'],
    "teacher_search_student.html": ['{% sub_nav "class_lists" %}'],
    "term_dates.html": ['{% sub_nav "settings" %}'],
}

files = {}
for path in ROOT.rglob("*.html"):
    files.setdefault(path.name, []).append(path)


def ensure_load(text):
    if "{% load nav %}" in text:
        return text, False
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if "{% extends" in line:
            lines.insert(i + 1, "{% load nav %}\n")
            return "".join(lines), True
    lines.insert(0, "{% load nav %}\n")
    return "".join(lines), True


errors = []
replaced = 0
for name, plan in PLAN.items():
    paths = files.get(name, [])
    if len(paths) != 1:
        errors.append(f"{name}: expected 1 file, found {len(paths)}")
        continue
    path = paths[0]
    text = path.read_text(encoding="utf-8")
    blocks = list(NAV_RE.finditer(text))
    if not blocks and "{% sub_nav" in text:
        print(f"  (done) {name}")
        continue
    if len(blocks) != len(plan):
        errors.append(f"{name}: expected {len(plan)} blocks, found {len(blocks)}")
        continue

    out, cursor = [], 0
    for match, replacement in zip(blocks, plan):
        out.append(text[cursor:match.start()])
        indent = re.match(r"[ \t]*", match.group(0)).group(0)
        out.append(f"{indent}{replacement}\n")
        cursor = match.end()
        replaced += 1
    out.append(text[cursor:])
    text, loaded = ensure_load("".join(out))
    path.write_text(text, encoding="utf-8")
    print(f"  {'~load ' if loaded else '       '}{name}: {len(plan)} block(s)")

print(f"\nreplaced {replaced} blocks")
if errors:
    print("\nERRORS:")
    for err in errors:
        print(f"  {err}")
    raise SystemExit(1)
