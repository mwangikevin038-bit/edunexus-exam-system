"""
Enterprise security framework for EduNexus multi-tenant exam system.
"""

import logging
from django.core.cache import cache

from .roles import (
    Role,
    get_user_role,
    role_required,
    school_admin_required,
    tenant_read_only_required,
    user_has_main_school_admin_override,
)
from .tenant import (
    SchoolScopedViewMixin,
    get_request_school,
    get_request_school_section,
    get_school_object_or_403,
    get_school_queryset,
    enforce_section_access,
    get_user_authoritative_section,
    assert_user_in_section,
    assert_class_in_workspace,
)
from .ratelimit import rate_limit

logger = logging.getLogger("students.security")

_SCHOOL_ID_CACHE_TTL = 3600  # 1 hour


def get_user_school_id(user):
    """
    Return the user's true school_id from their profile, or None.
    Cached in LocMemCache for 1 hour to avoid repeated DB queries.
    Falls back to DB if cache is unavailable.
    """
    if user is None:
        return None

    cache_key = f"user_school_id:{user.pk}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("Cache read failed for %s, falling back to DB", cache_key)

    school_id = _resolve_user_school_id(user)

    try:
        cache.set(cache_key, school_id, _SCHOOL_ID_CACHE_TTL)
    except Exception:
        logger.debug("Cache write failed for %s", cache_key)

    return school_id


def _resolve_user_school_id(user):
    """Resolve school_id from user profile via DB. Never caches."""
    try:
        if hasattr(user, "school_admin_profile") and user.school_admin_profile.school_id:
            return user.school_admin_profile.school_id
    except Exception:
        pass
    try:
        if hasattr(user, "teacher_profile") and user.teacher_profile.school_id:
            return user.teacher_profile.school_id
    except Exception:
        pass
    try:
        if hasattr(user, "student_profile") and user.student_profile.school_id:
            return user.student_profile.school_id
    except Exception:
        pass
    try:
        if hasattr(user, "guardian_profile") and user.guardian_profile.school_id:
            return user.guardian_profile.school_id
    except Exception:
        pass
    return None


def invalidate_user_school_cache(user_pk):
    """Call when a user's school assignment changes (e.g. role reassignment)."""
    try:
        cache.delete(f"user_school_id:{user_pk}")
    except Exception:
        pass


def get_user_school_object(user):
    """
    Return the user's school object from their profile, or None.
    Uses cached school_id to avoid redundant DB lookups.
    """
    if user is None:
        return None

    school_id = get_user_school_id(user)
    if school_id is None:
        return None

    from students.models import School
    try:
        return School.objects.get(pk=school_id)
    except School.DoesNotExist:
        return None


__all__ = [
    "Role",
    "get_user_role",
    "get_user_school_id",
    "get_user_school_object",
    "invalidate_user_school_cache",
    "role_required",
    "school_admin_required",
    "user_has_main_school_admin_override",
    "SchoolScopedViewMixin",
    "get_request_school",
    "get_request_school_section",
    "get_school_object_or_403",
    "get_school_queryset",
    "enforce_section_access",
    "get_user_authoritative_section",
    "assert_user_in_section",
    "assert_class_in_workspace",
    "tenant_read_only_required",
    "rate_limit",
]
