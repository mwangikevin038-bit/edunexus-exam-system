"""
Absolute multi-tenant isolation helpers and IDOR protection.
Every object lookup must be scoped to the authenticated user's school AND section.
"""
import logging

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.utils.functional import cached_property

from students.school_scope import get_current_school, get_current_school_section

logger = logging.getLogger("students.security.tenant")


def get_request_school(request):
    school = getattr(request, "school", None)
    if school is not None:
        return school

    user = getattr(request, "user", None)
    if user and user.is_authenticated and hasattr(user, "teacher_profile"):
        return user.teacher_profile.school

    if user and user.is_authenticated and hasattr(user, "student_profile"):
        return user.student_profile.school

    if user and user.is_authenticated and hasattr(user, "guardian_profile"):
        return user.guardian_profile.school

    return get_current_school()


def get_request_school_section(request):
    """Return the user's effective school_section, respecting workspace toggle for BOTH users."""
    section = None
    if hasattr(request, "session"):
        section = request.session.get("school_section")
        # For BOTH users, always resolve to a specific workspace (never BOTH)
        if section == "BOTH":
            workspace = request.session.get("workspace_section")
            if workspace in ("LOWER_PRIMARY", "PRIMARY", "JSS"):
                return workspace
            return "PRIMARY"
    if not section:
        section = get_current_school_section()
    return section or "BOTH"


def _is_platform_superuser(request):
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_superuser)


def _validate_section_access(record_section, user_section):
    """
    Return True if the user is allowed to access the record's section.
    'BOTH' users can access anything.
    'PRIMARY' users can only access PRIMARY records.
    'JSS' users can only access JSS records.
    """
    if user_section == "BOTH":
        return True
    return record_section == user_section


def get_school_object_or_403(model, request, *, using="objects", **lookup):
    """
    Fetch a tenant-scoped + section-scoped object or reject IDOR attempts with HTTP 403.
    Platform superusers may bypass school scoping for admin operations.
    """
    school = get_request_school(request)
    user_section = get_request_school_section(request)
    manager = getattr(model, using, model.objects)

    # Build lookup with school filter
    if school is not None:
        lookup = {**lookup, "school": school}
        qs = manager.filter(**lookup)
    elif _is_platform_superuser(request):
        qs = manager.filter(**lookup)
    else:
        logger.warning(
            "Tenant scope missing for lookup: model=%s lookup=%s user_id=%s path=%s",
            model.__name__,
            lookup,
            getattr(request.user, "pk", None),
            request.path,
        )
        raise PermissionDenied("School context is required for this operation.")

    obj = qs.first()

    # ── Section isolation check ──────────────────────────────────────────
    if obj is not None and user_section in ("PRIMARY", "JSS"):
        obj_section = getattr(obj, "school_section", None)
        if obj_section and obj_section != user_section:
            # Check if the record even exists in the other section
            unscoped = getattr(model, "all_objects", manager)
            if unscoped.filter(**{k: v for k, v in lookup.items() if k != "school"}).exists():
                logger.warning(
                    "SECTION IDOR blocked: model=%s record_section=%s user_section=%s "
                    "user_id=%s path=%s",
                    model.__name__,
                    obj_section,
                    user_section,
                    getattr(request.user, "pk", None),
                    request.path,
                )
                raise PermissionDenied("Cross-section access is forbidden.")
            raise Http404

    # ── Sub-section isolation check (LOWER_PRIMARY vs PRIMARY UPPER) ─────
    if obj is not None and user_section == "LOWER_PRIMARY":
        obj_sub = getattr(obj, "sub_section", None)
        if obj_sub is not None and obj_sub != "LOWER":
            logger.warning(
                "SUB-SECTION IDOR blocked: model=%s record_sub_section=%s user_section=%s "
                "user_id=%s path=%s",
                model.__name__,
                obj_sub,
                user_section,
                getattr(request.user, "pk", None),
                request.path,
            )
            raise PermissionDenied("Cross sub-section access is forbidden.")
    if obj is not None and user_section == "PRIMARY":
        obj_sub = getattr(obj, "sub_section", None)
        if obj_sub is not None and obj_sub != "UPPER":
            logger.warning(
                "SUB-SECTION IDOR blocked: model=%s record_sub_section=%s user_section=%s "
                "user_id=%s path=%s",
                model.__name__,
                obj_sub,
                user_section,
                getattr(request.user, "pk", None),
                request.path,
            )
            raise PermissionDenied("Cross sub-section access is forbidden.")

    if obj is not None:
        return obj

    unscoped_manager = getattr(model, "all_objects", manager)
    if unscoped_manager.filter(**{k: v for k, v in lookup.items() if k != "school"}).exists():
        logger.warning(
            "IDOR blocked: model=%s lookup=%s school_id=%s actor_id=%s ip=%s",
            model.__name__,
            lookup,
            getattr(school, "pk", None),
            getattr(request.user, "pk", None),
            _client_ip(request),
        )
        raise PermissionDenied("Cross-school access is forbidden.")

    raise Http404


def get_school_queryset(model, request, *, using="objects"):
    """Return a queryset scoped to the user's school AND section AND sub-section."""
    school = get_request_school(request)
    user_section = get_request_school_section(request)
    manager = getattr(model, using, model.objects)
    if school is None and _is_platform_superuser(request):
        return manager.all()
    if school is None:
        return manager.none()
    qs = manager.filter(school=school)
    if user_section in ("PRIMARY", "JSS"):
        qs = qs.filter(school_section=user_section)
    # Sub-section isolation for LOWER_PRIMARY vs PRIMARY UPPER
    if user_section == "LOWER_PRIMARY":
        qs = qs.filter(Q(sub_section='LOWER') | Q(sub_section__isnull=True))
    elif user_section == "PRIMARY":
        qs = qs.filter(Q(sub_section='UPPER') | Q(sub_section__isnull=True))
    return qs


def enforce_section_access(request, obj):
    """
    Raise PermissionDenied if the authenticated user cannot access the object's section.
    Use this for any direct object access outside of get_school_object_or_403.
    """
    user_section = get_request_school_section(request)
    if user_section == "BOTH":
        return
    obj_section = getattr(obj, "school_section", None)
    if obj_section and obj_section != user_section:
        logger.warning(
            "SECTION ENFORCEMENT blocked: model=%s record_section=%s user_section=%s "
            "user_id=%s path=%s",
            type(obj).__name__,
            obj_section,
            user_section,
            getattr(request.user, "pk", None),
            getattr(request, "path", "unknown"),
        )
        raise PermissionDenied("Cross-section access is forbidden.")
    # Sub-section isolation (LOWER_PRIMARY vs PRIMARY UPPER)
    obj_sub = getattr(obj, "sub_section", None)
    if user_section == "LOWER_PRIMARY" and obj_sub is not None and obj_sub != "LOWER":
        logger.warning(
            "SUB-SECTION ENFORCEMENT blocked: model=%s record_sub=%s user_section=%s "
            "user_id=%s path=%s",
            type(obj).__name__,
            obj_sub,
            user_section,
            getattr(request.user, "pk", None),
            getattr(request, "path", "unknown"),
        )
        raise PermissionDenied("Cross sub-section access is forbidden.")
    if user_section == "PRIMARY" and obj_sub is not None and obj_sub != "UPPER":
        logger.warning(
            "SUB-SECTION ENFORCEMENT blocked: model=%s record_sub=%s user_section=%s "
            "user_id=%s path=%s",
            type(obj).__name__,
            obj_sub,
            user_section,
            getattr(request.user, "pk", None),
            getattr(request, "path", "unknown"),
        )
        raise PermissionDenied("Cross sub-section access is forbidden.")


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


class SchoolScopedViewMixin:
    """Mixin for class-based views enforcing tenant isolation."""

    school_scoped_model = None

    @cached_property
    def request_school(self):
        return get_request_school(self.request)

    def get_school_object(self, **lookup):
        model = self.school_scoped_model
        if model is None:
            raise ImproperlyConfigured("SchoolScopedViewMixin requires school_scoped_model")
        return get_school_object_or_403(model, self.request, **lookup)
