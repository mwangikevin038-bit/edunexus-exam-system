"""
Security middleware: tenant enforcement, audit request binding, response headers.
"""
import logging

from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden

from students.security import get_user_school_id, get_user_school_object
from students.security.audit import bind_request_for_audit
from students.security.ratelimit import is_rate_limited

logger = logging.getLogger("students.security.middleware")

# Paths that are public (no tenant required) or superuser-only.
# Everything else requires a resolved school tenant for non-superusers.
PUBLIC_PREFIXES = ("/login/", "/logout/", "/forgot-password/", "/reset/", "/super/", "/admin/", "/static/", "/media/")
RATE_LIMITED_PATHS = {
    "/": ("login", 8, 60),
    "/super/": ("super_login", 8, 60),
    "/select-exam/": ("mark_entry", 30, 60),
    "/results/download-pdf/": ("report_download", 10, 60),
    "/class-lists/download-pdf/": ("report_download", 10, 60),
    "/bulk-reports/": ("report_download", 10, 60),
}


class SecurityAuditMiddleware:
    """Attach request context for asynchronous audit signal handlers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        bind_request_for_audit(request)
        return self.get_response(request)


class TenantIsolationMiddleware:
    """
    Reject authenticated school users when tenant context cannot be resolved.
    Prevents cross-tenant data leakage on misconfigured hosts or session drift.

    Every authenticated non-superuser request that is not a public path
    MUST have a valid school context. If not, the request is blocked.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or user.is_superuser:
            return self.get_response(request)

        path = request.path

        # Allow public paths through without tenant check
        if any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
            return self.get_response(request)

        # Allow password-change without tenant check (uses session-only auth)
        if path.startswith("/password-change/"):
            return self.get_response(request)

        # Block if subdomain resolution failed (e.g., unknown school subdomain)
        if getattr(request, "school_resolution_failed", False):
            logger.warning(
                "Tenant isolation blocked request: user_id=%s host=%s path=%s",
                user.pk,
                request.get_host(),
                path,
            )
            return HttpResponseForbidden("Invalid school tenant for this host.")

        # Block if no school context could be resolved for any non-public path
        if getattr(request, "school", None) is None:
            # Attempt recovery from user profile before blocking
            recovered = self._try_recover_school(user)
            if recovered:
                request.school = recovered
            else:
                logger.warning(
                    "Missing school context blocked: user_id=%s path=%s",
                    user.pk,
                    path,
                )
                return HttpResponseForbidden("School context is required for this operation.")

        return self.get_response(request)

    @staticmethod
    def _try_recover_school(user):
        """Attempt to recover school context from the user's profile."""
        return get_user_school_object(user)


class SessionSchoolValidator:
    """
    Re-validate that the session's school_id still matches the user's actual school.
    Prevents stale or tampered session school_id from causing cross-tenant access.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and not user.is_superuser:
            session_school_id = request.session.get("school_id")
            if session_school_id is not None:
                actual_school_id = self._get_actual_school_id(user)
                if actual_school_id is not None and session_school_id != actual_school_id:
                    logger.warning(
                        "Session school_id mismatch corrected: user_id=%s session_school=%s actual_school=%s path=%s",
                        user.pk,
                        session_school_id,
                        actual_school_id,
                        request.path,
                    )
                    request.session["school_id"] = actual_school_id
                    request.session.modified = True
        return self.get_response(request)

    @staticmethod
    def _get_actual_school_id(user):
        """Return the user's true school_id from their profile, or None."""
        return get_user_school_id(user)


class EndpointRateLimitMiddleware:
    """Path-based rate limiting for login, mark entry, and report downloads."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        config = RATE_LIMITED_PATHS.get(request.path)
        if config and request.method in {"GET", "POST"}:
            group, max_requests, window = config
            if is_rate_limited(request, group, max_requests, window):
                return HttpResponseForbidden("Rate limit exceeded. Try again later.")
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Supplement Django SecurityMiddleware with explicit defensive headers."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.is_secure():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class ForcePasswordChangeMiddleware:
    """
    Block all access until the user changes their password.
    Checks session 'force_password_change' flag on every request.
    Only allows through: password-change page, logout, static/media, and login.
    """

    EXEMPT_PREFIXES = ("/password-change/", "/logout/", "/static/", "/media/", "/login/", "/super/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            if request.session.get("force_password_change"):
                path = request.path
                if not any(path.startswith(prefix) for prefix in self.EXEMPT_PREFIXES):
                    # Only add warning if not already on a password-change redirect
                    # to prevent message stacking on repeated blocked requests
                    from django.shortcuts import redirect
                    from django.contrib import messages
                    storage = messages.get_messages(request)
                    has_password_warning = any(
                        'change your password' in str(m).lower()
                        for m in storage
                    )
                    if not has_password_warning:
                        messages.warning(request, "You must change your password before continuing.")
                    return redirect("password_change")
        return self.get_response(request)


class CloseOldConnectionsMiddleware:
    """
    Close stale DB connections at the end of every request so the
    per-keystroke auto-save endpoints + async audit logger don't
    accumulate idle connections until Postgres' max_connections is
    exhausted. Trivial cost (one function call), big payoff.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_response(self, request, response):
        try:
            from django.db import close_old_connections
            close_old_connections()
        except Exception:
            pass
        return response

    def process_exception(self, request, exception):
        try:
            from django.db import close_old_connections
            close_old_connections()
        except Exception:
            pass
