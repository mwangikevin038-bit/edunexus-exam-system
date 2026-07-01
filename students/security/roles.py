"""
Granular role-based access control (RBAC) for EduNexus.
Roles: Superuser, School Admin, Teacher, Student, Parent.
"""
import functools
import logging

from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

logger = logging.getLogger("students.security.roles")


class Role:
    SUPERUSER = "superuser"
    SCHOOL_ADMIN = "school_admin"
    TEACHER = "teacher"
    STUDENT = "student"
    PARENT = "parent"

    GROUP_MAP = {
        SCHOOL_ADMIN: "Main School Admin",
        TEACHER: "Teacher",
        STUDENT: "Student",
        PARENT: "Parent",
    }


def ensure_security_groups():
    """Create canonical RBAC groups if they do not exist."""
    for group_name in Role.GROUP_MAP.values():
        Group.objects.get_or_create(name=group_name)


def get_user_role(user):
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return Role.SUPERUSER
    # ── NEW: Check SchoolAdmin model (admin@schoolcode logins) ───────────────
    if hasattr(user, "school_admin_profile"):
        return Role.SCHOOL_ADMIN
    # ── Group check (explicit school admin group membership) ──────────────────
    if user.groups.filter(name=Role.GROUP_MAP[Role.SCHOOL_ADMIN]).exists():
        return Role.SCHOOL_ADMIN
    if hasattr(user, "teacher_profile"):
        return Role.TEACHER
    if hasattr(user, "student_profile"):
        return Role.STUDENT
    if hasattr(user, "guardian_profile"):
        return Role.PARENT
    return None


def user_has_main_school_admin_override(user):
    return get_user_role(user) in {Role.SUPERUSER, Role.SCHOOL_ADMIN}


def user_is_read_only_portal(user):
    """Students and parents may only read their own records."""
    return get_user_role(user) in {Role.STUDENT, Role.PARENT}


def user_can_mutate_marks(user):
    role = get_user_role(user)
    return role in {Role.SUPERUSER, Role.SCHOOL_ADMIN, Role.TEACHER}


def school_admin_required(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not user_has_main_school_admin_override(request.user):
            messages.error(request, "Only the school admin can access that page.")
            return redirect("dashboard_alt")
        return view_func(request, *args, **kwargs)

    return wrapper


def role_required(*allowed_roles, redirect_url="dashboard_alt", message=None):
    def decorator(view_func):
        @functools.wraps(view_func)
        def wrapper(request, *args, **kwargs):
            role = get_user_role(request.user)
            if role not in allowed_roles:
                logger.warning(
                    "RBAC denied: user_id=%s role=%s path=%s",
                    getattr(request.user, "pk", None),
                    role,
                    request.path,
                )
                if message:
                    messages.error(request, message)
                    return redirect(redirect_url)
                raise PermissionDenied("You do not have permission to access this resource.")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def tenant_read_only_required(view_func):
    """Block write operations for student/parent portal accounts."""

    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.method not in ("GET", "HEAD", "OPTIONS") and user_is_read_only_portal(request.user):
            logger.warning(
                "Read-only RBAC write blocked: user_id=%s path=%s method=%s",
                request.user.pk,
                request.path,
                request.method,
            )
            raise PermissionDenied("Read-only accounts cannot modify records.")
        return view_func(request, *args, **kwargs)

    return wrapper