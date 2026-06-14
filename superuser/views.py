import datetime
import json
import secrets
import string

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from students.models import School, SchoolAdmin, SystemBroadcast
from students.security import rate_limit


def superuser_required(view_func):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('super_login')
        if not request.user.is_superuser:
            messages.error(request, "Access denied. Superuser credentials required.")
            return redirect('super_login')
        return view_func(request, *args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


def _generate_temp_password(length=12):
    """
    Generate a secure temporary password.
    Always includes uppercase, lowercase, digits, and a symbol
    so it passes Django's password validators.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    while True:
        pwd = ''.join(secrets.choice(alphabet) for _ in range(length))
        # Ensure all character classes are represented
        if (
            any(c.isupper() for c in pwd)
            and any(c.islower() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%" for c in pwd)
        ):
            return pwd


def _create_school_admin(school, admin_email, temp_password):
    """
    Create the Django User + SchoolAdmin record for a new school.
    Username format: admin@<schoolcode>
    Returns the SchoolAdmin instance.
    """
    username = f"admin@{school.code}"

    # Defensive: if an admin already exists for this school, return it
    existing = SchoolAdmin.objects.filter(school=school).first()
    if existing:
        return existing

    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            email=admin_email,
            password=temp_password,
            first_name="School Admin",
            last_name=school.name,
        )
        school_admin = SchoolAdmin.objects.create(
            user=user,
            school=school,
            is_active=True,
            must_change_password=True,
        )

    return school_admin


def _send_admin_welcome_email(school, admin_email, temp_password):
    """
    Send the temporary credentials email to the new school admin.
    Returns True on success, False on failure.
    """
    subject = f"EduNexus — Your Admin Account for {school.name}"
    message = f"""
Dear School Administrator,

Your EduNexus school management account has been created for {school.name}.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR LOGIN CREDENTIALS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Login URL   : http://localhost:8000/login/
Username    : admin@{school.code}
Password    : {temp_password}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IMPORTANT: This is a temporary password. You will be prompted to
change it on your first login.

As the School Administrator you can:
  • Create and manage teacher accounts
  • Enroll and manage students
  • Create and publish assessments
  • Review and approve mark submissions
  • Generate report cards and results

If you did not expect this email, please contact EduNexus support immediately.

— The EduNexus Team
    """.strip()

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=None,  # Uses DEFAULT_FROM_EMAIL from settings
            recipient_list=[admin_email],
            fail_silently=False,
        )
        return True
    except Exception:
        return False


# ==============================================================================
# AUTH
# ==============================================================================

@ensure_csrf_cookie
@require_http_methods(["GET", "POST"])
@rate_limit("super_login", max_requests=8, window_seconds=60, methods=["POST"])
def super_login(request):
    if request.user.is_authenticated and request.user.is_superuser:
        return redirect('super_dashboard')

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "").strip()

        if not username or not password:
            messages.error(request, "Please enter both username and password.")
            return render(request, "superuser/login.html")

        user = authenticate(request, username=username, password=password)
        if user and user.is_superuser:
            login(request, user)
            return redirect("super_dashboard")
        if user:
            messages.error(request, "This portal is for EduNexus superusers only.")
        else:
            messages.error(request, "Invalid credentials.")

    return render(request, "superuser/login.html")


def super_logout(request):
    logout(request)
    return redirect("super_login")


# ==============================================================================
# DASHBOARD
# ==============================================================================

@superuser_required
def super_dashboard(request):
    schools = School.objects.all().order_by("-created_on")
    today = datetime.date.today()

    return render(request, "superuser/dashboard.html", {
        "schools": schools,
        "total_schools": schools.count(),
        "active_schools": schools.filter(status="active").count(),
        "trial_schools": schools.filter(status="trial").count(),
        "suspended_schools": schools.filter(status="suspended").count(),
        "basic_count": schools.filter(tier="Basic").count(),
        "premium_count": schools.filter(tier="Premium").count(),
        "enterprise_count": schools.filter(tier="Enterprise").count(),
        "expiring_soon": schools.filter(
            paid_until__isnull=False,
            paid_until__lte=today + datetime.timedelta(days=30),
            paid_until__gte=today,
        ).order_by("paid_until"),
        "broadcasts": SystemBroadcast.objects.filter(is_active=True).order_by("-created_at"),
        "today": today,
    })


# ==============================================================================
# SCHOOL MANAGEMENT
# ==============================================================================

@superuser_required
def school_list(request):
    schools = School.objects.all().order_by("-created_on")
    return render(request, "superuser/school_list.html", {"schools": schools})


@superuser_required
def school_create(request):
    if request.method == "POST":
        name       = request.POST.get("name", "").strip()
        code       = request.POST.get("code", "").strip().lower().replace(" ", "")
        tier       = request.POST.get("tier", "Basic")
        status     = request.POST.get("status", "trial")
        paid_until = request.POST.get("paid_until") or None
        phone      = request.POST.get("phone_number", "").strip()
        email      = request.POST.get("email", "").strip()
        address    = request.POST.get("address", "").strip()

        # ── Validation ────────────────────────────────────────────────────────
        if not name or not code:
            messages.error(request, "School name and school code are required.")
            return render(request, "superuser/school_form.html", {"action": "Create"})

        if not email:
            messages.error(request, "A billing/contact email is required to create the admin account.")
            return render(request, "superuser/school_form.html", {"action": "Create"})

        if School.objects.filter(code=code).exists():
            messages.error(request, f"School code '{code}' is already taken.")
            return render(request, "superuser/school_form.html", {"action": "Create"})

        # ── Create school + admin atomically ──────────────────────────────────
        try:
            with transaction.atomic():
                school = School(
                    name=name,
                    code=code,
                    tier=tier,
                    status=status,
                    paid_until=paid_until,
                    phone_number=phone,
                    email=email,
                    address=address,
                    on_trial=(status == "trial"),
                )
                if request.FILES.get("logo"):
                    school.logo = request.FILES["logo"]
                school.save()

                # Generate temp password and create the admin account
                temp_password = _generate_temp_password()
                _create_school_admin(school, email, temp_password)

        except Exception as e:
            messages.error(request, f"Failed to create school: {e}")
            return render(request, "superuser/school_form.html", {"action": "Create"})

        # ── Send credentials email ────────────────────────────────────────────
        email_sent = _send_admin_welcome_email(school, email, temp_password)

        if email_sent:
            messages.success(
                request,
                f"✓ '{name}' created. "
                f"Admin credentials sent to {email}. "
                f"Login: admin@{code}"
            )
        else:
            # Email failed — show the password on screen as a fallback
            # so you can share it manually. This is the ONLY time it's shown.
            messages.success(
                request,
                f"✓ '{name}' created successfully."
            )
            messages.warning(
                request,
                f"⚠ Email delivery failed. Share these credentials manually — "
                f"they will NOT be shown again. "
                f"Login: admin@{code} | Temp password: {temp_password}"
            )

        return redirect("super_school_list")

    return render(request, "superuser/school_form.html", {"action": "Create"})


@superuser_required
def school_edit(request, school_id):
    school = get_object_or_404(School, id=school_id)

    if request.method == "POST":
        school.name         = request.POST.get("name", school.name).strip()
        school.code         = (request.POST.get("code") or school.code).strip().lower().replace(" ", "")
        school.tier         = request.POST.get("tier", school.tier)
        school.status       = request.POST.get("status", school.status)
        school.phone_number = request.POST.get("phone_number", "").strip()
        school.email        = request.POST.get("email", "").strip()
        school.address      = request.POST.get("address", "").strip()
        paid_until          = request.POST.get("paid_until", "")
        school.paid_until   = paid_until if paid_until else None
        school.on_trial     = (school.status == "trial")
        if request.FILES.get("logo"):
            school.logo = request.FILES["logo"]
        school.save()
        messages.success(request, f"'{school.name}' updated successfully.")
        return redirect("super_school_list")

    return render(request, "superuser/school_form.html", {
        "action": "Edit",
        "school": school,
    })


@superuser_required
def school_delete(request, school_id):
    school = get_object_or_404(School, id=school_id)
    if request.method == "POST":
        name = school.name
        school.delete()
        messages.success(request, f"'{name}' has been deleted.")
        return redirect("super_school_list")
    return render(request, "superuser/school_confirm_delete.html", {"school": school})


@superuser_required
def school_toggle_status(request, school_id):
    if request.method == "POST":
        school = get_object_or_404(School, id=school_id)
        data = json.loads(request.body)
        school.status = data.get("status", school.status)
        school.save()
        return JsonResponse({"status": "ok", "new_status": school.status})
    return JsonResponse({"status": "error"}, status=400)


# ==============================================================================
# BROADCASTS
# ==============================================================================

@superuser_required
def broadcast_list(request):
    broadcasts = SystemBroadcast.objects.all().order_by("-created_at")
    return render(request, "superuser/broadcast_list.html", {"broadcasts": broadcasts})


@superuser_required
def broadcast_create(request):
    if request.method == "POST":
        title    = request.POST.get("title", "").strip()
        message  = request.POST.get("message", "").strip()
        audience = request.POST.get("target_audience", "all")
        if title and message:
            SystemBroadcast.objects.create(
                title=title,
                message=message,
                target_audience=audience,
                is_active=True,
            )
            messages.success(request, "Broadcast published to all schools.")
            return redirect("super_broadcast_list")
        messages.error(request, "Title and message are required.")
    return render(request, "superuser/broadcast_form.html")


@superuser_required
def broadcast_toggle(request, broadcast_id):
    if request.method == "POST":
        broadcast = get_object_or_404(SystemBroadcast, id=broadcast_id)
        broadcast.is_active = not broadcast.is_active
        broadcast.save()
        return JsonResponse({"is_active": broadcast.is_active})
    return JsonResponse({"error": "POST required"}, status=400)


@superuser_required
def broadcast_delete(request, broadcast_id):
    broadcast = get_object_or_404(SystemBroadcast, id=broadcast_id)
    if request.method == "POST":
        broadcast.delete()
        messages.success(request, "Broadcast deleted.")
        return redirect("super_broadcast_list")
    return render(request, "superuser/broadcast_confirm_delete.html", {"broadcast": broadcast})