"""
Password management helpers.

Centralises:
  * Strong-password validation rules (length, complexity, no repeats,
    not-in-history, not common).
  * History record-keeping.
  * "Invalidate every other session for this user" (used after a
    password change so a stolen cookie on another device can't
    continue to be used).
  * "Send me an email when my password changes" notification.

A single source of truth for the rules means the change-password form
and the password-reset confirm form always agree on what's allowed.
"""
import logging
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.models import Session
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from django.conf import settings

from ..models import PasswordHistory, School, SchoolAdmin, Teacher

logger = logging.getLogger("students.security.password")


# ─── Strong-password rules ────────────────────────────────────────────────
# Reused by StrongPasswordChangeForm and StrongSetPasswordForm.
MIN_LENGTH = 8
COMMON_BLACKLIST = {
    'password', '12345678', 'qwerty', 'admin123', 'letmein',
    'welcome1', 'iloveyou', 'monkey', 'dragon', 'sunshine',
    'princess', 'football', 'baseball', 'passw0rd', 'p@ssw0rd',
    'p@ssword', 'p@ssword1', 'changeme', 'abcd1234', '11111111',
    '123456789', 'qwerty123', 'asdfghjk', 'zxcvbnm1',
}


def password_validation_errors(password, *, user=None, min_length=MIN_LENGTH):
    """
    Return a list of human-readable error strings for ``password``.

    Does NOT raise — the caller decides whether to accumulate errors
    via a form's ``ValidationError`` or short-circuit.

    ``user`` is the User changing the password; we use it to enforce
    "don't reuse the last N" and "don't mirror the username/email".
    """
    errors = []
    if not password:
        return ['Password is required.']

    if len(password) < min_length:
        errors.append(f"Your password must be at least {min_length} characters long. You currently have {len(password)} character{'s' if len(password) != 1 else ''}.")

    # Only the first three character classes count toward the strength
    # requirement; the 4th is "strong" feedback.
    needed = []
    missing_upper = not any(c.isupper() for c in password)
    missing_lower = not any(c.islower() for c in password)
    missing_digit = not any(c.isdigit() for c in password)
    missing_special = not any(not c.isalnum() for c in password)

    if missing_upper:
        needed.append('an uppercase letter (A-Z)')
    if missing_lower:
        needed.append('a lowercase letter (a-z)')
    if missing_digit:
        needed.append('a number (0-9)')
    if missing_special:
        needed.append('a special character (!@#$%^&*)')

    if needed:
        if len(needed) == 1:
            errors.append(f"Your password must include {needed[0]}.")
        elif len(needed) == 2:
            errors.append(f"Your password must include {needed[0]} and {needed[1]}.")
        else:
            errors.append(
                "Your password must include "
                + ', '.join(needed[:-1])
                + ', and ' + needed[-1]
                + '.'
            )

    # 3+ repeated characters (e.g. "aaaaaa", "111111")
    import re
    repeated_match = re.search(r'(.)\1{2,}', password)
    if repeated_match:
        char = repeated_match.group(1)
        errors.append(
            f"Your password contains {repeated_match.group(0)} — "
            f"avoid repeating the same character 3+ times in a row."
        )

    # Common / dictionary blacklists
    if password.lower() in COMMON_BLACKLIST:
        errors.append(
            "This password is too commonly used and would be easy to guess. "
            "Try adding unique words, numbers, or symbols."
        )

    # Disallow mirroring the username or email (cheap check)
    if user is not None:
        ident = (user.username or '') + ' ' + (user.email or '')
        ident = ident.lower()
        if password.lower() in ident:
            errors.append(
                "Your password must not contain your username or email address. "
                "Choose something unrelated to your identity."
            )

    # Disallow reuse of recent passwords
    if user is not None and user.pk and password_has_been_used(user, password):
        errors.append(
            "You've used this password before. For security, please choose "
            "a completely new password you haven't used recently."
        )

    return errors


def password_has_been_used(user, raw_password):
    """
    Return True if ``raw_password`` matches any of the user's recent
    password hashes (up to ``PasswordHistory.HISTORY_DEPTH``).
    """
    if not user or not user.pk or not raw_password:
        return False
    for entry in PasswordHistory.objects.filter(user=user)[:PasswordHistory.HISTORY_DEPTH]:
        try:
            if check_password(raw_password, entry.password_hash):
                return True
        except Exception:
            # A corrupt entry should never block the change; skip it.
            continue
    return False


def record_password_history(user, raw_password):
    """
    Append a hash of the new password to the user's history and trim
    the table to the last ``HISTORY_DEPTH`` entries. Run AFTER the
    password has been successfully changed on the User.
    """
    if not user or not user.pk or not raw_password:
        return
    PasswordHistory.objects.create(
        user=user,
        password_hash=make_password(raw_password),
    )
    # Trim old entries
    keep_ids = list(
        PasswordHistory.objects.filter(user=user)
        .order_by('-created_at')
        .values_list('id', flat=True)[:PasswordHistory.HISTORY_DEPTH]
    )
    if keep_ids:
        PasswordHistory.objects.filter(user=user).exclude(id__in=keep_ids).delete()


# ─── "Log out everywhere else" ───────────────────────────────────────────
def invalidate_other_sessions(user, *, keep_session_key=None):
    """
    Delete every active Django session for ``user`` EXCEPT the one
    identified by ``keep_session_key`` (the device that just changed
    the password). Returns the number of sessions destroyed.

    Called after a successful password change or reset, so a stolen
    cookie on another device is immediately invalidated.
    """
    if not user or not user.is_authenticated:
        return 0

    # Sessions store the user id under the auth key. We use Django's
    # SessionStore to decode each one. This is the same approach
    # django.contrib.sessions.backends.db uses internally.
    from django.contrib.sessions.backends.db import SessionStore

    qs = Session.objects.filter(expire_date__gt=timezone.now())
    killed = 0
    for sess in qs:
        if sess.session_key == keep_session_key:
            continue
        try:
            data = sess.get_decoded()
        except Exception:
            # Corrupt / unreadable — drop it.
            sess.delete()
            killed += 1
            continue
        if data.get('_auth_user_id') == str(user.pk):
            sess.delete()
            killed += 1
    if killed:
        logger.info("Invalidated %d other session(s) for user %s", killed, user.username)
    return killed


# ─── "Your password changed" email ───────────────────────────────────────
def send_password_changed_email(user, *, request=None):
    """
    Send a security notification when the password is changed. Best-effort —
    any mail failure is logged but never blocks the change.

    Retries up to 2 times with exponential backoff on transient failures.
    """
    import time as _time

    if not user or not user.email:
        return False

    MAX_RETRIES = 2
    RETRY_DELAY = 1  # seconds

    for attempt in range(1 + MAX_RETRIES):
        try:
            from django.core.mail import EmailMessage
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'EDUNEXUS Portal <edunexus.system@gmail.com>')
            site_url = getattr(settings, 'SITE_URL', 'http://localhost:8000')
            context = {
                'user': user,
                'timestamp': timezone.now(),
                'ip': request.META.get('REMOTE_ADDR') if request else None,
                'login_url': f'{site_url}/login/',
            }
            html = render_to_string('email/password_changed.html', context)
            text = render_to_string('email/password_changed.txt', context)
            email = EmailMessage(
                subject='Your EDUNEXUS password was changed',
                body=html,
                from_email=from_email,
                to=[user.email],
                headers={
                    'Reply-To': from_email,
                    'Precedence': 'bulk',
                    'List-Unsubscribe': f'<{site_url}/login/>',
                    'X-Auto-Response-Suppress': 'All',
                },
            )
            email.content_subtype = 'html'
            email.send(fail_silently=True)
            logger.info("Password-changed email sent to %s", user.email)
            return True
        except Exception as exc:
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Email send failed (attempt %d/%d) for %s: %s — retrying in %ds",
                    attempt + 1, 1 + MAX_RETRIES, user.email, exc, RETRY_DELAY
                )
                _time.sleep(RETRY_DELAY)
                RETRY_DELAY *= 2  # exponential backoff
            else:
                logger.exception(
                    "Could not send password-changed email to %s after %d attempts",
                    user.email, 1 + MAX_RETRIES
                )
    return False
