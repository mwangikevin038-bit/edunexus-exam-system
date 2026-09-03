"""
Custom password reset views with enhanced security.

Provides:
- Strong password validation on reset flow
- Rate limiting for password reset requests
- Token invalidation after use
- Session tracking for reset flow
"""

import re
import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import (
    PasswordResetView,
    PasswordResetDoneView,
    PasswordResetConfirmView,
    PasswordResetCompleteView,
)
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils.crypto import get_random_string

logger = logging.getLogger("students.security.password_reset")


class StrongPasswordResetForm(PasswordResetForm):
    """Password reset form with the same strong validation as StrongPasswordChangeForm."""

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if not email:
            raise ValidationError("Please enter your email address.")
        return email.lower().strip()


class StrongSetPasswordForm(SetPasswordForm):
    """
    Set password form with strong password validation.
    Uses centralized validation from students.security.passwords.
    """

    def clean_new_password1(self):
        from ..security.passwords import password_validation_errors
        password = self.cleaned_data.get('new_password1')
        errors = password_validation_errors(password, user=getattr(self, 'user', None))
        if errors:
            raise ValidationError(errors)
        return password


class RateLimitedPasswordResetView(PasswordResetView):
    """
    Password reset view with rate limiting and strong form.
    Limits: 3 reset requests per email per 15 minutes.
    """

    form_class = StrongPasswordResetForm
    template_name = 'password_reset_form.html'
    email_template_name = 'email/password_reset_email.txt'
    html_email_template_name = 'email/password_reset_email.html'
    subject_template_name = 'email/password_reset_subject.txt'
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'EDUNEXUS Portal <edunexus.system@gmail.com>')
    success_url = '/forgot-password/done/'

    RATE_LIMIT_KEY = 'password_reset_attempts'
    RATE_LIMIT_MAX = 3
    RATE_LIMIT_WINDOW = 900  # 15 minutes

    def send_mail(self, subject_template_name, email_template_name,
                  context, from_email, to_email,
                  html_email_template_name=None):
        import time as _time
        MAX_RETRIES = 2
        RETRY_DELAY = 1

        for attempt in range(1 + MAX_RETRIES):
            try:
                from django.core.mail import EmailMessage
                from django.template.loader import render_to_string
                subject = render_to_string(subject_template_name, context)
                subject = ''.join(subject.splitlines())
                html_body = render_to_string(html_email_template_name or email_template_name, context)
                text_body = render_to_string(email_template_name, context)
                site_url = getattr(settings, 'SITE_URL', 'http://localhost:8000')
                email = EmailMessage(
                    subject=subject,
                    body=html_body,
                    from_email=from_email,
                    to=[to_email],
                    headers={
                        'Reply-To': from_email,
                        'Precedence': 'bulk',
                        'List-Unsubscribe': f'<{site_url}/login/>',
                        'X-Auto-Response-Suppress': 'All',
                    },
                )
                email.content_subtype = 'html'
                email.send(fail_silently=True)
                logger.info("Password reset email sent to %s", to_email)
                return
            except Exception as exc:
                if attempt < MAX_RETRIES:
                    logger.warning(
                        "Password reset email failed (attempt %d/%d) for %s: %s — retrying",
                        attempt + 1, 1 + MAX_RETRIES, to_email, exc
                    )
                    _time.sleep(RETRY_DELAY)
                    RETRY_DELAY *= 2
                else:
                    logger.exception(
                        "Could not send password reset email to %s after %d attempts",
                        to_email, 1 + MAX_RETRIES
                    )

    def form_valid(self, form):
        email = form.cleaned_data.get('email', '').lower().strip()

        # Check rate limit per email in session
        rate_data = self.request.session.get(self.RATE_LIMIT_KEY, {})
        import time
        now = time.time()

        if email in rate_data:
            attempts, window_start = rate_data[email]
            if now - window_start < self.RATE_LIMIT_WINDOW:
                if attempts >= self.RATE_LIMIT_MAX:
                    remaining = int(self.RATE_LIMIT_WINDOW - (now - window_start))
                    minutes = remaining // 60
                    seconds = remaining % 60
                    messages.warning(
                        self.request,
                        f"Too many reset requests. Please try again in {minutes}m {seconds}s."
                    )
                    return redirect('password_reset')
            else:
                rate_data[email] = (0, now)

        # Increment attempt counter
        attempts, window_start = rate_data.get(email, (0, now))
        rate_data[email] = (attempts + 1, window_start)
        self.request.session[self.RATE_LIMIT_KEY] = rate_data
        self.request.session.modified = True

        # Log the reset attempt
        logger.info("Password reset requested for email: %s from IP: %s", email, self.request.META.get('REMOTE_ADDR'))

        return super().form_valid(form)


class SecurePasswordResetConfirmView(PasswordResetConfirmView):
    """
    Password reset confirm view with:
    - Strong password validation
    - Token invalidation after use
    - Session tracking
    """

    template_name = 'password_reset_confirm.html'
    form_class = StrongSetPasswordForm
    success_url = '/reset/done/'
    post_reset_login = False

    def form_valid(self, form):
        # Store that this user completed password reset in the session
        response = super().form_valid(form)

        # Invalidate the token by saving the user (Django's token generator checks password hash)
        user = form.user
        if user:
            # Force token invalidation by updating the user's last_login
            from django.utils import timezone
            user.last_login = timezone.now()
            user.save(update_fields=['last_login'])

            # Clear must_change_password on Teacher profile to prevent forced-change loop
            from ..models import Teacher, SchoolAdmin
            try:
                teacher = Teacher.all_objects.get(user=user)
                if teacher.must_change_password:
                    teacher.must_change_password = False
                    teacher.save(update_fields=['must_change_password'])
            except Teacher.DoesNotExist:
                pass
            try:
                admin = SchoolAdmin.objects.get(user=user)
                if admin.must_change_password:
                    admin.must_change_password = False
                    admin.save(update_fields=['must_change_password'])
            except SchoolAdmin.DoesNotExist:
                pass

            # Invalidate other sessions (security)
            from ..security.passwords import invalidate_other_sessions, send_password_changed_email
            invalidate_other_sessions(user)

            # Send notification email (best-effort)
            send_password_changed_email(user, request=self.request)

            # Log the password reset completion
            logger.info(
                "Password reset completed for user: %s from IP: %s",
                user.username,
                self.request.META.get('REMOTE_ADDR')
            )

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Calculate actual token expiry from Django's settings
        # Django default is PASSWORD_RESET_TIMEOUT_DAYS = 1 (or PASSWORD_RESET_TIMEOUT in seconds)
        from django.conf import settings
        timeout_seconds = getattr(settings, 'PASSWORD_RESET_TIMEOUT', None)
        if timeout_seconds is None:
            timeout_days = getattr(settings, 'PASSWORD_RESET_TIMEOUT_DAYS', 1)
            timeout_seconds = timeout_days * 86400
        context['token_expiry_hours'] = max(1, timeout_seconds // 3600)
        return context


class SecurePasswordResetDoneView(PasswordResetDoneView):
    """Password reset done view with custom template."""
    template_name = 'password_reset_done.html'


class SecurePasswordResetCompleteView(PasswordResetCompleteView):
    """Password reset complete view with custom template."""
    template_name = 'password_reset_complete.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['login_url'] = '/login/'
        return context
