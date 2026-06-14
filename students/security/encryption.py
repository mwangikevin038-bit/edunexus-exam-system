"""
Field-level encryption for sensitive PII using Fernet (AES-128-CBC + HMAC).
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger("students.security.encryption")

_PREFIX = "enc::"
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet

    raw_key = getattr(settings, "FIELD_ENCRYPTION_KEY", None) or settings.SECRET_KEY
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    _fernet = Fernet(base64.urlsafe_b64encode(digest))
    return _fernet


def encrypt_value(value):
    if value in (None, ""):
        return value
    token = _get_fernet().encrypt(str(value).encode("utf-8")).decode("utf-8")
    return f"{_PREFIX}{token}"


def decrypt_value(value):
    if value in (None, ""):
        return value
    if not str(value).startswith(_PREFIX):
        return value
    token = str(value)[len(_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt field value — possible tampering or key rotation issue.")
        return ""


class EncryptedCharField(models.CharField):
    """Transparent Fernet encryption for CharField values at rest."""

    def from_db_value(self, value, expression, connection):
        return decrypt_value(value)

    def to_python(self, value):
        if value is None:
            return value
        return decrypt_value(value)

    def get_prep_value(self, value):
        if value in (None, ""):
            return value
        if str(value).startswith(_PREFIX):
            return value
        return encrypt_value(value)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, "students.security.encryption.EncryptedCharField", args, kwargs
