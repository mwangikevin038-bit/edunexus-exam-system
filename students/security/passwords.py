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
        errors.append(f"Password must be at least {min_length} characters long.")

    # Only the first three character classes count toward the strength
    # requirement; the 4th is "strong" feedback.
    needed = []
    if not any(c.isupper() for c in password):
        needed.append('an uppercase letter')
    if not any(c.islower() for c in password):
        needed.append('a lowercase letter')
    if not any(c.isdigit() for c in password):
        needed.append('a digit')
    if not any(not c.isalnum() for c in password):
        needed.append('a special character')
    if needed:
        errors.append(
            "Password must contain "
            + ', '.join(needed[:-1])
            + (' and ' + needed[-1] if len(needed) > 1 else needed[-1])
            + '.'
        )

    # 3+ repeated characters (e.g. "aaaaaa", "111111")
    import re
    if re.search(r'(.)\1{2,}', password):
        errors.append("Password must not contain 3 or more repeated characters in a row.")

    # Common / dictionary blacklists
    if password.lower() in COMMON_BLACKLIST:
        errors.append("Password is too common. Please choose a stronger password.")

    # Disallow mirroring the username or email (cheap check)
    if user is not None:
        ident = (user.username or '') + ' ' + (user.email or '')
        ident = ident.lower()
        if password.lower() in ident:
            errors.append("Password must not contain your username or email.")

    # Disallow reuse of recent passwords
    if user is not None and user.pk and password_has_been_used(user, password):
        errors.append(
            "You have used this password recently. Please choose a different one."
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
    """
    if not user or not user.email:
        return False
    try:
        from_email = 'EDUNEXUS <noreply@edunexus.system>'
        context = {
            'user': user,
            'timestamp': timezone.now(),
            'ip': request.META.get('REMOTE_ADDR') if request else None,
            'login_url': 'http://localhost:8000/login/',
        }
        html = render_to_string('email/password_changed.html', context)
        text = render_to_string('email/password_changed.txt', context)
        send_mail(
            subject='Your EDUNEXUS password was changed',
            message=text,
            from_email=from_email,
            recipient_list=[user.email],
            html_message=html,
            fail_silently=True,
        )
        return True
    except Exception:
        logger.exception("Could not send password-changed email to %s", user.email)
        return False
