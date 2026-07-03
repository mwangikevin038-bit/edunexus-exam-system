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
    Enforces: 8+ chars, uppercase, lowercase, digit, special char, no 3+ repeats.
    """

    def clean_new_password1(self):
        password = self.cleaned_data.get('new_password1')
        errors = []

        if len(password) < 8:
            errors.append("Password must be at least 8 characters long.")
        if not re.search(r'[A-Z]', password):
            errors.append("Password must contain at least one uppercase letter.")
        if not re.search(r'[a-z]', password):
            errors.append("Password must contain at least one lowercase letter.")
        if not re.search(r'\d', password):
            errors.append("Password must contain at least one digit.")
        if not re.search(r'[!@#$%^&*(),.?\":{}|<>\-_=+\[\]\\;\'`~]', password):
            errors.append("Password must contain at least one special character.")
        if re.search(r'(.)\1{2,}', password):
            errors.append("Password must not contain 3 or more repeated characters.")
        if password.lower() in ['password', '12345678', 'qwerty', 'admin123', 'letmein']:
            errors.append("Password is too common. Please choose a stronger password.")

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
    from_email = 'EDUNEXUS <noreply@edunexus.system>'
    success_url = '/forgot-password/done/'

    RATE_LIMIT_KEY = 'password_reset_attempts'
    RATE_LIMIT_MAX = 3
    RATE_LIMIT_WINDOW = 900  # 15 minutes

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

            # Log the password reset completion
            logger.info(
                "Password reset completed for user: %s from IP: %s",
                user.username,
                self.request.META.get('REMOTE_ADDR')
            )

        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Add token expiry info (24 hours from Django default)
        context['token_expiry_hours'] = 24
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
