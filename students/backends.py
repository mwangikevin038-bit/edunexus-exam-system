"""
Custom authentication backends for the EduNexus system.

Provides school-scoped authentication that routes users based on login format:
- phonenumber@schoolcode  → Teacher
- admin@schoolcode        → School Admin
- username only           → Superuser
"""

import logging
import datetime

from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User

logger = logging.getLogger('students.backends')


def mask_phone(phone):
    """Mask phone number for safe logging: 0728007320 -> 072***320"""
    if not phone:
        return '***'
    phone = str(phone)
    if len(phone) >= 7:
        return phone[:3] + '***' + phone[-3:]
    return '***'


class SchoolScopedAuthBackend(ModelBackend):
    """
    Authenticates teachers by phone@schoolcode.
    Authenticates school admins by admin@schoolcode.
    Validates school status before allowing login.
    Binds school_id to the session at login time.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        username = username.strip()

        
        # ── Superuser path: no @ in username ──────────────────────────────────
        if '@' not in username:
            return self._authenticate_superuser(username, password, request)

        # ── Split identifier@schoolcode ───────────────────────────────────────
        parts = username.rsplit('@', 1)
        identifier = parts[0].strip()
        school_code = parts[1].strip().lower()

        if not identifier or not school_code:
            logger.warning(f"Malformed login username: '{mask_phone(identifier)}'")
            return None

        # ── Validate school exists ────────────────────────────────────────────
        try:
            from students.models import School
            school = School.objects.get(code__iexact=school_code)
        except School.DoesNotExist:
            # Deliberate generic failure — don't reveal school codes
            logger.warning(f"Login attempt with unknown school code: '{school_code}'")
            return None


        # ── Enforce school account status ─────────────────────────────────────
        if not self._school_is_allowed(school):
            return None

        # ── Route: admin@schoolcode → School Admin ────────────────────────────
        if identifier.lower() == 'admin':
            return self._authenticate_school_admin(school, password, request)

        # ── Route: phone@schoolcode → Teacher ─────────────────────────────────
        return self._authenticate_teacher(identifier, school, school_code, password, request)

    # ──────────────────────────────────────────────────────────────────────────
    # Private auth methods
    # ──────────────────────────────────────────────────────────────────────────

    def _authenticate_school_admin(self, school, password, request):
        """Authenticate a school admin via admin@schoolcode."""
        try:
            from students.models import SchoolAdmin
            school_admin = SchoolAdmin.objects.select_related('user').get(
                school=school,
                is_active=True,
            )
        except SchoolAdmin.DoesNotExist:
            logger.warning(
                f"No active school admin for school='{school.code}'"
            )
            return None

        user = school_admin.user

        if not user.check_password(password):
            logger.warning(
                f"Wrong password for school admin user_id={user.pk} school='{school.code}'"
            )
            return None

        if not self.user_can_authenticate(user):
            logger.warning(
                f"Inactive school admin attempted login: user_id={user.pk}"
            )
            return None

        # Bind school + flag admin role to session
        if request is not None:
            request.session['school_id'] = school.pk
            request.session['school_code'] = school.code
            request.session['is_school_admin'] = True
            request.session['school_section'] = 'BOTH'
            request.session['workspace_section'] = 'PRIMARY'
            request.session.cycle_key()

        logger.info(
            f"SCHOOL ADMIN LOGIN: user_id={user.pk} school='{school.code}' ip={self._get_ip(request)}"
        )
        return user

    def _authenticate_teacher(self, phone_number, school, school_code, password, request):
        """Authenticate a teacher via phone@schoolcode."""
        try:
            from students.models import Teacher
            teacher = Teacher.all_objects.select_related('user', 'school').get(
                school=school,
                phone_number=phone_number,
                is_active=True,
            )
        except Teacher.DoesNotExist:
            logger.warning(
                f"No active teacher for phone='{mask_phone(phone_number)}' school='{school_code}'"
            )
            return None
        except Teacher.MultipleObjectsReturned:
            logger.error(
                f"Duplicate teacher record for phone='{mask_phone(phone_number)}' school='{school_code}'"
            )
            return None

        user = teacher.user

        if not user.check_password(password):
            logger.warning(
                f"Wrong password for user_id={user.pk} school='{school_code}'"
            )
            return None

        if not self.user_can_authenticate(user):
            logger.warning(
                f"Inactive/disallowed user attempted login: user_id={user.pk}"
            )
            return None

        # Bind school to session
        if request is not None:
            request.session['school_id'] = school.pk
            request.session['school_code'] = school.code
            request.session['school_section'] = teacher.school_section
            request.session.cycle_key()

        logger.info(
            f"LOGIN SUCCESS: user_id={user.pk} username='{mask_phone(phone_number)}' "
            f"school='{school_code}' ip={self._get_ip(request)}"
        )
        return user

    def _authenticate_superuser(self, username, password, request):
        """Allow Django superusers to log in without a school code."""
        try:
            user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            return None

        if (
            user.is_superuser
            and user.check_password(password)
            and self.user_can_authenticate(user)
        ):
            logger.info(
                f"SUPERUSER LOGIN: user_id={user.pk} ip={self._get_ip(request)}"
            )
            return user

        return None

    def _school_is_allowed(self, school):
        """Return True only if school is active and subscription is valid."""
        if school.status == 'suspended':
            logger.warning(
                f"Login blocked — school suspended: '{school.code}' (pk={school.pk})"
            )
            return False

        if not school.on_trial and school.paid_until:
            if school.paid_until < datetime.date.today():
                logger.warning(
                    f"Login blocked — subscription expired: '{school.code}' "
                    f"(paid_until={school.paid_until})"
                )
                return False

        return True

    def _get_ip(self, request):
        if request is None:
            return 'unknown'
        x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded:
            return x_forwarded.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', 'unknown')

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None