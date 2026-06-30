"""
Multi-tenant school scope management.

Provides context variables and middleware for resolving the current school
based on subdomain, session, or user profile.
"""

import ipaddress
import logging
from contextvars import ContextVar
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

    When a school_section context is set (PRIMARY or JSS), queries are
    automatically filtered to that section only. 'BOTH' or unset = no section filter.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        school = get_current_school()
        if school is None:
            return qs.none()
        qs = qs.filter(school=school)

        section = get_current_school_section()
        if section in ('PRIMARY', 'JSS'):
            qs = qs.filter(school_section=section)

        return qs

    def get_for_school(self, school, **kwargs):
        return self.using(self._db).filter(school=school, **kwargs)

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

        # Resolve the tenant only from an exact subdomain match.
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

        # Non-subdomain requests: validate session school_id against user's actual school.
        if not request.school and not is_subdomain_request:
            session_school_id = request.session.get("school_id") if hasattr(request, "session") else None
            if session_school_id:
                try:
                    from students.models import School
                    school_from_session = School.objects.filter(pk=session_school_id).first()
                    if school_from_session:
                        # Validate against user's profile if authenticated
                        user = getattr(request, "user", None)
                        if user and user.is_authenticated:
                            actual_school_id = self._get_user_school_id(user)
                            if actual_school_id is not None and actual_school_id != session_school_id:
                                logger.warning(
                                    "Session school_id mismatch corrected: "
                                    "user_id=%s session_school=%s actual_school=%s",
                                    user.pk, session_school_id, actual_school_id,
                                )
                                request.session["school_id"] = actual_school_id
                                request.session.modified = True
                                request.school = School.objects.filter(pk=actual_school_id).first()
                            else:
                                request.school = school_from_session
                        else:
                            request.school = school_from_session
                except Exception:
                    request.school = None

        # Final non-subdomain fallback for already-authenticated users only.
        if not request.school and not is_subdomain_request and getattr(request, "user", None) and request.user.is_authenticated:
            try:
                # ── Check SchoolAdmin first ──────────────────────────────
                from students.models import SchoolAdmin
                school_admin = SchoolAdmin.objects.select_related("school").filter(
                    user=request.user,
                    is_active=True,
                ).first()
                if school_admin and school_admin.school_id:
                    request.school = school_admin.school

                # ── Then Teacher ──────────────────────────────────────────────
                if not request.school:
                    from students.models import Teacher
                    teacher = Teacher.all_objects.select_related("school").filter(user=request.user).first()
                    if teacher and teacher.school_id:
                        request.school = teacher.school

                # ── Then Student ──────────────────────────────────────────────
                if not request.school:
                    from students.models import Student
                    student = Student.objects.select_related("school").filter(user=request.user).first()
                    if student and student.school_id:
                        request.school = student.school

            except Exception:
                pass

        request._current_school_token = set_current_school(request.school)

        # ── Inject school_section into ContextVar for global query isolation ──
        section = None
        if hasattr(request, "session"):
            section = request.session.get("school_section")
            # For BOTH users, always resolve to a specific workspace (never BOTH)
            if section == "BOTH":
                workspace = request.session.get("workspace_section")
                if workspace in ("LOWER_PRIMARY", "PRIMARY", "JSS"):
                    section = workspace
                else:
                    # Default to PRIMARY for old sessions without workspace_section
                    section = "PRIMARY"
                    request.session["workspace_section"] = "PRIMARY"
                    request.session.modified = True
        if not section:
            section = self._get_user_school_section(request.user if hasattr(request, "user") else None)
        request._current_school_section_token = set_current_school_section(section or "BOTH")

    @staticmethod
    def _get_user_school_id(user):
        """Return the user's true school_id from their profile, or None."""
        from students.security import get_user_school_id
        return get_user_school_id(user)

    @staticmethod
    def _get_user_school_section(user):
        """Return the user's school_section from their profile, or None."""
        if not user or not user.is_authenticated or user.is_superuser:
            return 'BOTH'
        from students.models import Teacher, SchoolAdmin
        if SchoolAdmin.objects.filter(user=user, is_active=True).exists():
            return 'BOTH'
        teacher = Teacher.all_objects.filter(user=user).first()
        if teacher:
            return teacher.school_section
        return 'BOTH'

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