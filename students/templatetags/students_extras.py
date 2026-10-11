"""Custom template tags for the students app."""
from django import template
from django.template.base import TemplateSyntaxError, Variable, VariableDoesNotExist

register = template.Library()


@register.simple_tag(takes_context=True)
def school_from(context, *names):
    """First present, truthy context variable — returned as the object itself.

    `{% firstof a b as x %}` cannot be used for this: Django stores
    render_value_in_context()'s *string* in `x`, so `x.logo` / `x.name` come
    back empty. Missing names are skipped rather than raised, so the bulk-PDF
    path (no `request` in context) and the web path (no `school` key) both
    resolve instead of blowing up mid-render.
    """
    for name in names:
        try:
            value = Variable(name).resolve(context)
        except (TemplateSyntaxError, VariableDoesNotExist, KeyError):
            continue
        if value:
            return value
    return None


@register.filter(name="get_item")
def get_item(dictionary, key):
    """Dict lookup that returns '' if the key is missing."""
    if not dictionary:
        return ""
    if hasattr(dictionary, "get"):
        return dictionary.get(key, "")
    return ""


@register.filter(name="level_or_dash")
def level_or_dash(value, fallback="-"):
    """Printable grade — hides legacy 'NO CONFIG' sentinel rows."""
    from students.views.helpers import safe_level
    return safe_level(value, fallback)
