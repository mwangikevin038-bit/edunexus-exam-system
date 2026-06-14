"""
Enterprise security framework for EduNexus multi-tenant exam system.
"""

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
)
from .ratelimit import rate_limit


def get_user_school_id(user):
    """
    Return the user's true school_id from their profile, or None.
    
    Checks school_admin_profile, teacher_profile, student_profile,
    and guardian_profile in order of priority.
    """
    if user is None:
        return None
    
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


def get_user_school_object(user):
    """
    Return the user's school object from their profile, or None.
    
    Similar to get_user_school_id but returns the School instance.
    """
    if user is None:
        return None
    
    try:
        if hasattr(user, "school_admin_profile") and user.school_admin_profile.school_id:
            return user.school_admin_profile.school
    except Exception:
        pass
    try:
        if hasattr(user, "teacher_profile") and user.teacher_profile.school_id:
            return user.teacher_profile.school
    except Exception:
        pass
    try:
        if hasattr(user, "student_profile") and user.student_profile.school_id:
            return user.student_profile.school
    except Exception:
        pass
    try:
        if hasattr(user, "guardian_profile") and user.guardian_profile.school_id:
            return user.guardian_profile.school
    except Exception:
        pass
    return None


__all__ = [
    "Role",
    "get_user_role",
    "get_user_school_id",
    "get_user_school_object",
    "role_required",
    "school_admin_required",
    "user_has_main_school_admin_override",
    "SchoolScopedViewMixin",
    "get_request_school",
    "get_request_school_section",
    "get_school_object_or_403",
    "get_school_queryset",
    "enforce_section_access",
    "tenant_read_only_required",
    "rate_limit",
]
