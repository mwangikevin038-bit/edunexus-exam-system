"""Shared sub-navigation bar.

One named preset replaces each of the ~38 copy-pasted ``<nav class="sub-nav-bar">``
blocks that were spread across the templates.

Usage::

    {% load nav %}
    {% block sub_tabs %}{% sub_nav "results" %}{% endblock %}

    {% sub_nav "results" active="Report Cards" %}   # pin the highlight
    {% sub_nav "results" active="" %}               # highlight nothing
    {% sub_nav "exams" only="Manage Exams" %}       # render a subset

Active state is resolved in this order:

1. ``active="Label"`` / ``active=""`` when passed explicitly.
2. The item whose href matches the current request path + query.
3. The first item.
"""
from functools import lru_cache

from django import template
from django.template.base import TemplateSyntaxError

register = template.Library()

PARTIAL = "students/partials/sub_nav.html"

_MANAGE_EXAMS = "{% url 'manage_exams' %}"


def _exams(grading_href):
    """The four-tab Exams bar. Grading links either to the manage tab or
    straight to the standalone grading-config page."""
    return [
        (f"{_MANAGE_EXAMS}?tab=manage", "Manage Exams", "ph-gear-six"),
        (f"{_MANAGE_EXAMS}?tab=create", "Create Exam", "ph-plus-circle"),
        (grading_href, "Grading Systems", "ph-chart-line-up"),
        (f"{_MANAGE_EXAMS}?tab=deleted", "Deleted Exams", "ph-trash"),
    ]


#: preset -> list of (href template, label, phosphor icon)
PRESETS = {
    "printouts": [("{% url 'printouts_hub' %}", "Printouts", "ph-printer")],
    "exams": _exams(f"{_MANAGE_EXAMS}?tab=grading"),
    "exams_config": _exams("{% url 'grading_configuration' %}"),
    "overview": [("{% url 'dashboard_alt' %}", "Overview", "ph-squares-four")],
    "overview_admin": [
        ("{% url 'school_admin_dashboard' %}", "Overview", "ph-squares-four")
    ],
    "classes": [
        ("{% url 'manage_classes' %}?tab=manage", "Manage Classes", "ph-gear-six"),
        ("{% url 'add_new_class' %}", "Add New Class", "ph-plus-circle"),
    ],
    "faculty": [
        ("{% url 'manage_faculty_matrix' %}?tab=manage", "Manage Teachers", "ph-users-three"),
        ("{% url 'manage_faculty_matrix' %}?tab=add", "Add New Teacher", "ph-user-plus"),
    ],
    "students": [
        ("{% url 'admin_add_student' %}?tab=directory", "Search Student", "ph-magnifying-glass"),
        ("{% url 'admin_add_student' %}?tab=add_student", "Add Student", "ph-user-plus"),
        ("{% url 'admin_add_student' %}?tab=move_student", "Move Students", "ph-arrows-left-right"),
        ("{% url 'admin_add_student' %}?tab=removed_students", "Deleted Students", "ph-trash"),
    ],
    "comment_sections": [
        ("{% url 'admin_comments' %}?section=JSS", "JSS", "ph-books"),
        ("{% url 'admin_comments' %}?section=PRIMARY", "Primary", "ph-book-open"),
    ],
    "class_lists": [
        ("{% url 'class_lists' %}", "Class Lists", "ph-list"),
        ("{% url 'teacher_search_student' %}", "Search Student", "ph-magnifying-glass"),
    ],
    "profile": [
        ("{% url 'home_alt' %}", "Update Profile", "ph-user"),
        ("{% url 'password_change' %}", "Change Password", "ph-shield-check"),
    ],
    "settings": [
        ("{% url 'school_settings' %}", "School Profile", "ph-buildings"),
        ("{% url 'term_dates' %}", "Term Dates", "ph-calendar-blank"),
    ],
    "results": [
        ("{% url 'merit_list' %}", "Results", "ph-chart-bar"),
        ("{% url 'teacher_report_forms' %}", "Report Cards", "ph-file-text"),
        ("{% url 'report_card_lookup' %}", "Find Report", "ph-file-search"),
        ("{% url 'teacher_class_comments' %}", "Class Comments", "ph-chats-circle"),
    ],
    "edit_exam": [
        ("{% url 'edit_exam' %}?exam_id={{ exam.id }}", "Edit Exam", "ph-gear-six")
    ],
    "score_capture": [
        (
            "{% if section == 'JSS' %}{% url 'select_exam' %}"
            "{% else %}{% url 'select_exam_primary' %}{% endif %}",
            "Score Capture",
            "ph-pencil-simple",
        )
    ],
}


@lru_cache(maxsize=None)
def _compile(engine, source):
    return engine.from_string(source)


def _render_href(context, source):
    """Compile ``source`` against the engine currently rendering the page.

    Using the page's own engine (rather than a fresh ``Template()``) keeps the
    ``{% url %}`` / ``{{ ... }}`` resolution identical to the markup it replaces.
    """
    current = getattr(context, "template", None)
    engine = current.engine if current is not None else None
    if engine is None:
        from django.template import Template

        return Template(source).render(context)
    return _compile(engine, source).render(context)


def _match_index(request, rows):
    """Index of the row linking to the page we are on, else ``None``."""
    if request is None:
        return None
    current = request.get_full_path()
    for index, row in enumerate(rows):
        href = row["href"]
        target = request.path + href if href.startswith("?") else href
        if target == current:
            return index
    return None


@register.inclusion_tag(PARTIAL, takes_context=True)
def sub_nav(context, preset, active=None, only=None):
    try:
        items = PRESETS[preset]
    except KeyError:
        raise TemplateSyntaxError(
            f"Unknown sub-nav preset {preset!r}. "
            f"Known presets: {', '.join(sorted(PRESETS))}"
        ) from None

    if only:
        wanted = {label.strip() for label in only.split(",") if label.strip()}
        items = [item for item in items if item[1] in wanted]
        if not items:
            raise TemplateSyntaxError(
                f"sub_nav: only={only!r} matched nothing in preset {preset!r}"
            )

    rows = [
        {"href": _render_href(context, href), "label": label, "icon": icon}
        for href, label, icon in items
    ]

    if active == "":
        active_index = None
    elif active is not None:
        active_index = next(
            (i for i, row in enumerate(rows) if row["label"] == active), None
        )
        if active_index is None:
            raise TemplateSyntaxError(
                f"sub_nav: preset {preset!r} has no item labelled {active!r}"
            )
    else:
        active_index = _match_index(context.get("request"), rows)
        if active_index is None and rows:
            active_index = 0

    for index, row in enumerate(rows):
        row["active"] = index == active_index

    return {"items": rows}
