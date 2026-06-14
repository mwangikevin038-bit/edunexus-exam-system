"""
App configuration for the students app.

Handles signal registration for security audit logging and group management.
"""

from django.apps import AppConfig


class StudentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "students"
    verbose_name = "Academic Management"

    def ready(self):
        from django.db.models.signals import post_migrate

        from students.security.audit import connect_audit_signals
        from students.security.roles import ensure_security_groups

        connect_audit_signals()

        def _bootstrap_security_groups(sender, **kwargs):
            if sender.name != "students":
                return
            try:
                ensure_security_groups()
            except Exception:
                pass

        post_migrate.connect(_bootstrap_security_groups, dispatch_uid="students_security_groups")
