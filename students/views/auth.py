"""
Authentication views for the EduNexus student management system.

Handles user login, logout, welcome page, and forced password changes.
"""

from django.contrib import messages
from django.contrib.messages import get_messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from .helpers import get_teacher_for_user
from ..security import rate_limit, user_has_main_school_admin_override
from ..models import Teacher


def welcome_page(request):
    """Renders the system's main welcome landing screen."""
    return render(request, 'students/welcome.html')


def logout_view(request):
    """Destroys the current user session securely and clears any pending messages."""
    # Exhaust all messages so they don't leak to the login page
    for _ in get_messages(request):
        pass
    logout(request)
    return redirect('welcome_page')


@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
@rate_limit("login", max_requests=8, window_seconds=60, methods=["POST"])
def login_view(request):
    """
    Login using phonenumber@schoolcode format.
    Rate limiting is handled by django-axes middleware automatically.
    """
    if request.user.is_authenticated:
        # Already logged in — redirect appropriately
        if request.user.is_superuser:
            from django.conf import settings as django_settings
            return redirect(f'/super/{django_settings.SUPERUSER_SECRET_TOKEN}/signin/')
        if user_has_main_school_admin_override(request.user):
            return redirect('school_admin_dashboard')
        return redirect('dashboard_alt')

    # Clear stale messages on fresh login page load (GET)
    if request.method == 'GET':
        storage = get_messages(request)
        list(storage)

    if request.method == 'POST':
        username_input = request.POST.get('username', '').strip()
        password_input = request.POST.get('password', '').strip()

        # Basic input validation
        if not username_input or not password_input:
            messages.error(request, "Please enter both your username and password.")
            return render(request, 'students/login.html')

        # ── BLOCK SUPERUSER LOGINS FROM THIS PORTAL ──────────────────────────
        # Superusers must use the dedicated admin portal at /admin-portal/
        # This prevents brute-force attacks on the superuser account through
        # the public-facing school login page.
        if '@' not in username_input:
            superuser_exists = User.objects.filter(
                username=username_input,
                is_superuser=True,
            ).exists()
            if superuser_exists:
                messages.error(
                    request,
                    "Superuser accounts cannot log in through this portal. "
                    "Use the dedicated admin portal."
                )
                return render(request, 'students/login.html')
            messages.error(
                request,
                "Use the format: phonenumber@schoolcode (e.g. 0712345678@baringohigh)"
            )
            return render(request, 'students/login.html')

        # Also block if the username contains @ but resolves to a superuser
        user_obj = User.objects.filter(username=username_input, is_superuser=True).first()
        if user_obj:
            messages.error(
                request,
                "Superuser accounts cannot log in through this portal. "
                "Use the dedicated admin portal."
            )
            return render(request, 'students/login.html')

        user = authenticate(request, username=username_input, password=password_input)

        if user is not None:
            login(request, user)

            # Double-lock: reject superuser even if they somehow got past the check above
            if user.is_superuser:
                logout(request)
                messages.error(
                    request,
                    "Superuser accounts cannot log in through this portal. "
                    "Use the dedicated admin portal."
                )
                return render(request, 'students/login.html')

            if not request.session.get('school_id'):
                logout(request)
                messages.error(request, "Your school session could not be verified. Please sign in again.")
                return render(request, 'students/login.html')

            if user_has_main_school_admin_override(user):
                return redirect('school_admin_dashboard')

            # Check if teacher must change password on first login
            try:
                teacher_profile = user.teacher_profile
                if teacher_profile.must_change_password:
                    request.session['force_password_change'] = True
                    messages.warning(request, "You must change your password before continuing. Please set a new strong password.")
                    return redirect('password_change')
            except Teacher.DoesNotExist:
                pass

            return redirect('dashboard_alt')

        # Authentication failed — axes handles lockout automatically
        messages.error(request, "Invalid credentials. Please check your phone number, school code, and password.")
        return render(request, 'students/login.html')

    return render(request, 'students/login.html')


@login_required(login_url='login')
@require_http_methods(["POST"])
def switch_workspace(request):
    """
    Toggle the admin's active workspace between Primary and JSS.
    Only accessible to users with school_section='BOTH'.
    Sets session['workspace_section'] which drives the ContextVar filtering.
    """
    user_section = request.session.get('school_section')
    if user_section != 'BOTH':
        messages.error(request, "You do not have access to switch workspaces.")
        return redirect('school_admin_dashboard')

    target = request.POST.get('section', '').strip().upper()
    if target not in ('PRIMARY', 'JSS'):
        messages.error(request, "Invalid workspace section.")
        return redirect('school_admin_dashboard')

    request.session['workspace_section'] = target
    request.session.modified = True

    label_map = {'PRIMARY': 'Upper Primary', 'JSS': 'Junior Secondary'}
    messages.success(request, f"Switched to {label_map[target]} workspace.")

    # Redirect back to the page the user was on
    referer = request.META.get('HTTP_REFERER')
    if referer and referer.startswith(request.build_absolute_uri('/')):
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(referer)
    return redirect('school_admin_dashboard')


@login_required(login_url='login')
def custom_password_change(request):
    """Custom password change view that enforces strong passwords and updates must_change_password flag."""
    from ..forms import StrongPasswordChangeForm

    force_password_change = request.session.get('force_password_change', False)

    if request.method == 'POST':
        form = StrongPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            form.save()
            # Clear the force_password_change session flag
            request.session.pop('force_password_change', None)
            # Update must_change_password on Teacher profile if it exists
            try:
                teacher = Teacher.objects.get(user=request.user)
                if teacher.must_change_password:
                    teacher.must_change_password = False
                    teacher.save(update_fields=['must_change_password'])
            except Teacher.DoesNotExist:
                pass
            messages.success(request, "Your password has been changed successfully.")
            return redirect('home_alt')
    else:
        form = StrongPasswordChangeForm(user=request.user)

    return render(request, 'password_change_form.html', {
        'form': form,
        'force_password_change': force_password_change,
    })