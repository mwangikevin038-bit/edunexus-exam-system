"""
Multi-tenant school scope management.

Provides context variables and middleware for resolving the current school
based on subdomain, session, or user profile.
"""

import ipaddress
import logging
from contextvars import ContextVar
from django.core.cache import cache
from django.db import models
from django.utils.deprecation import MiddlewareMixin

_current_school = ContextVar("current_school", default=None)
_current_school_section = ContextVar("current_school_section", default=None)
logger = logging.getLogger("students.school_scope")

def set_current_school(school):
    return _current_school.set(school)

def reset_current_school(token):
    try:
        _current_school.reset(token)
    except ValueError:
        pass

def get_current_school():
    return _current_school.get()

def set_current_school_section(section):
    """Set the current school section filter ('PRIMARY', 'JSS', or 'BOTH')."""
    return _current_school_section.set(section)

def reset_current_school_section(token):
    """Reset the current school section filter."""
    try:
        _current_school_section.reset(token)
    except ValueError:
        pass

def get_current_school_section():
    """Get the current school section filter ('PRIMARY', 'JSS', or 'BOTH')."""
    return _current_school_section.get()

class SchoolScopedQuerySet(models.QuerySet):
    def for_school(self, school):
        if school is None:
            return self.none()
        return self.filter(school=school)


class SchoolScopedManager(models.Manager):
    """
    Default manager: returns only rows for the active tenant.
    When no tenant is bound, returns an empty queryset to prevent cross-school leaks.
    Use all_objects for platform admin and migration tasks.
    """

    _sub_section_cache = {}

    def get_queryset(self):
        qs = super().get_queryset()
        school = get_current_school()
        if school is None:
            return qs.none()
        qs = qs.filter(school=school)

        section = get_current_school_section()

        if section == 'JSS':
            qs = qs.filter(school_section='JSS')
        elif section == 'PRIMARY':
            if self._has_sub_section():
                qs = qs.filter(school_section='PRIMARY', sub_section='UPPER')
            else:
                qs = qs.filter(school_section='PRIMARY')
        elif section == 'LOWER_PRIMARY':
            if self._has_sub_section():
                qs = qs.filter(school_section='PRIMARY', sub_section='LOWER')
            else:
                qs = qs.filter(school_section='PRIMARY')

        return qs

    def _has_sub_section(self):
        model = self.model
        if model not in self._sub_section_cache:
            self._sub_section_cache[model] = 'sub_section' in [f.name for f in model._meta.get_fields()]
        return self._sub_section_cache[model]

    def get_for_school(self, school, **kwargs):
        return self.using(self._db).filter(school=school, **kwargs)


# ---------------------------------------------------------------------------
# School object cache — avoids repeated School.objects.get() per request
# ---------------------------------------------------------------------------
_SCHOOL_OBJ_CACHE_TTL = 3600  # 1 hour


def _get_cached_school(school_id):
    """Return a cached School instance by pk, or fetch from DB."""
    if school_id is None:
        return None
    try:
        from django.core.cache import cache
        cache_key = f"school_obj:{school_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    from students.models import School
    try:
        school = School.objects.filter(pk=school_id).first()
    except Exception:
        return None

    try:
        from django.core.cache import cache
        cache.set(cache_key, school, _SCHOOL_OBJ_CACHE_TTL)
    except Exception:
        pass

    return school


def invalidate_school_cache(school_id):
    """Call when school data changes (e.g. school settings updated)."""
    try:
        from django.core.cache import cache
        cache.delete(f"school_obj:{school_id}")
    except Exception:
        pass


class CurrentSchoolMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.school = None
        request.school_subdomain = None
        request.school_resolution_failed = False

        host = request.get_host().split(":")[0].lower()
        labels = host.split(".")
        try:
            ipaddress.ip_address(host)
            is_ip_address = True
        except ValueError:
            is_ip_address = False
        is_subdomain_request = (
            not is_ip_address
            and len(labels) > 1
            and labels[0] not in {"www", "localhost", ""}
        )

        # ── 1. Subdomain resolution (rare path) ──────────────────────────
        try:
            from students.models import School
            if is_subdomain_request:
                request.school_subdomain = labels[0]
                request.school = School.objects.filter(code__iexact=request.school_subdomain).first()
                request.school_resolution_failed = request.school is None
                if request.school_resolution_failed:
                    logger.warning(
                        "Unknown school subdomain blocked: host=%s subdomain=%s",
                        host,
                        request.school_subdomain,
                    )
        except Exception:
            request.school = None
            request.school_resolution_failed = is_subdomain_request

        # ── 2. Non-subdomain: validate session school_id (fast path) ─────
        if not request.school and not is_subdomain_request:
            session_school_id = request.session.get("school_id") if hasattr(request, "session") else None
            if session_school_id:
                try:
                    school_from_session = _get_cached_school(session_school_id)
                    if school_from_session:
                        user = getattr(request, "user", None)
                        if user and user.is_authenticated:
                            # Use cached get_user_school_id (LocMemCache, 1hr TTL)
                            from students.security import get_user_school_id
                            actual_school_id = get_user_school_id(user)
                            if actual_school_id is not None and actual_school_id != session_school_id:
                                logger.warning(
                                    "Session school_id mismatch corrected: "
                                    "user_id=%s session_school=%s actual_school=%s",
                                    user.pk, session_school_id, actual_school_id,
                                )
                                request.session["school_id"] = actual_school_id
                                request.session.modified = True
                                request.school = _get_cached_school(actual_school_id)
                            else:
                                request.school = school_from_session
                        else:
                            request.school = school_from_session
                except Exception:
                    request.school = None

        # ── 3. Final fallback: query user profile (cached via get_user_school_id) ──
        if not request.school and not is_subdomain_request and getattr(request, "user", None) and request.user.is_authenticated:
            try:
                from students.security import get_user_school_id
                school_id = get_user_school_id(request.user)
                if school_id:
                    request.school = _get_cached_school(school_id)
            except Exception:
                pass

        request._current_school_token = set_current_school(request.school)

        # ── Inject school_section into ContextVar for global query isolation ──
        user = getattr(request, "user", None)
        is_authenticated = bool(user and user.is_authenticated)
        user_authoritative = self._get_user_school_section(user) if is_authenticated else "BOTH"

        section = None
        if is_authenticated and user_authoritative == "BOTH":
            if hasattr(request, "session"):
                section = request.session.get("school_section")
                if section == "BOTH":
                    workspace = request.session.get("workspace_section")
                    if workspace in ("LOWER_PRIMARY", "PRIMARY", "JSS"):
                        section = workspace
                    else:
                        section = "PRIMARY"
                        request.session["workspace_section"] = "PRIMARY"
                        request.session.modified = True
        if not section:
            section = user_authoritative
        request._current_school_section_token = set_current_school_section(section or "BOTH")

    @staticmethod
    def _get_user_school_id(user):
        """Return the user's true school_id from their profile, or None. Cached."""
        from students.security import get_user_school_id
        return get_user_school_id(user)

    @staticmethod
    def _get_user_school_section(user):
        """Return the user's school_section from their profile, or None. Cached."""
        if not user or not user.is_authenticated or user.is_superuser:
            return 'BOTH'

        cache_key = f"user_school_section:{user.pk}"
        try:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        except Exception:
            pass

        from students.models import Teacher, SchoolAdmin
        if SchoolAdmin.objects.filter(user=user, is_active=True).exists():
            section = 'BOTH'
        else:
            teacher = Teacher.all_objects.filter(user=user).first()
            if teacher:
                if teacher.school_section == 'PRIMARY' and teacher.sub_section == 'LOWER':
                    section = 'LOWER_PRIMARY'
                else:
                    section = teacher.school_section
            else:
                section = 'BOTH'

        try:
            cache.set(cache_key, section, 3600)
        except Exception:
            pass

        return section

    def process_response(self, request, response):
        token = getattr(request, "_current_school_token", None)
        if token is not None:
            reset_current_school(token)
            request._current_school_token = None
        section_token = getattr(request, "_current_school_section_token", None)
        if section_token is not None:
            reset_current_school_section(section_token)
            request._current_school_section_token = None
        return response

    def process_exception(self, request, exception):
        token = getattr(request, "_current_school_token", None)
        if token is not None:
            reset_current_school(token)
            request._current_school_token = None
        section_token = getattr(request, "_current_school_section_token", None)
        if section_token is not None:
            reset_current_school_section(section_token)
            request._current_school_section_token = None
        return None