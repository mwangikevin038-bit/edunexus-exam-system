"""
Input sanitization helpers for forms and request payloads.
"""
import re

import bleach
from django import forms
from django.core.exceptions import ValidationError

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SCRIPT_PATTERN = re.compile(r"(?i)<\s*script|javascript:|on\w+\s*=")


def sanitize_text(value, *, max_length=None, allow_html=False):
    if value is None:
        return value
    cleaned = str(value).strip()
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    if not allow_html:
        cleaned = bleach.clean(cleaned, tags=[], attributes={}, strip=True)
    if _SCRIPT_PATTERN.search(cleaned):
        raise ValidationError("Potentially malicious input detected.")
    if max_length and len(cleaned) > max_length:
        raise ValidationError(f"Input exceeds maximum length of {max_length} characters.")
    return cleaned


class SecureFormMixin:
    """Strip XSS vectors and control characters from all CharField/TextField inputs."""

    def clean(self):
        cleaned_data = super().clean()
        for field_name, value in cleaned_data.items():
            field = self.fields.get(field_name)
            if value is None or field is None:
                continue
            if isinstance(field, (forms.CharField, forms.RegexField, forms.EmailField, forms.SlugField)):
                cleaned_data[field_name] = sanitize_text(
                    value,
                    max_length=getattr(field, "max_length", None),
                )
        return cleaned_data
