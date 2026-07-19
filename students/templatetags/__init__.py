from django import template

register = template.Library()


@register.filter(name="get_item")
def get_item(dictionary, key):
    """Dict lookup that returns '' if the key is missing."""
    if not dictionary:
        return ""
    if hasattr(dictionary, "get"):
        return dictionary.get(key, "")
    return ""
