"""Memcached-safe cache key helpers.

Django's CacheKeyWarning fires when keys contain spaces or control
characters. KEY_FUNCTION runs before the backend validates the key, so
wiring safe_make_key into every CACHES entry fixes all keys at once.
"""
import re

from django.core.cache.backends.base import default_key_func as _default_make_key

# Spaces (0x20) and C0/C1 control characters are forbidden by memcached.
_UNSAFE = re.compile(r'[\x00-\x20\x7f]+')


def sanitize_cache_part(value):
    """Return a single key segment with no spaces/control characters."""
    text = '' if value is None else str(value)
    cleaned = _UNSAFE.sub('_', text).strip('_')
    return cleaned or '-'


def safe_make_key(key, key_prefix, version):
    """Django KEY_FUNCTION: sanitize, then apply the default prefix/version."""
    if isinstance(key, str):
        key = _UNSAFE.sub('_', key)
    return _default_make_key(key, key_prefix, version)
