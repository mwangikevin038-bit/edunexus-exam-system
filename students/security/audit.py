"""
Tamper-proof asynchronous security audit logging for sensitive mutations.
"""
import logging
import threading

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.forms.models import model_to_dict

logger = logging.getLogger("students.security.audit")

AUDITED_MODELS = {}
_pre_save_cache = threading.local()


def register_audit_model(model, fields=None):
    AUDITED_MODELS[model] = fields


def _client_ip_from_request():
    try:
        from django.contrib.auth.middleware import get_user
        from django.utils.functional import SimpleLazyObject

        request = getattr(_pre_save_cache, "request", None)
        if request is None:
            return "system"
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")
    except Exception:
        return "system"


def bind_request_for_audit(request):
    _pre_save_cache.request = request


def _serialize_value(value):
    if value is None:
        return None
    if hasattr(value, "pk"):
        return str(value.pk)
    return str(value)


def _diff_instances(old_data, new_data, tracked_fields):
    changes = {}
    keys = tracked_fields or set(new_data.keys())
    for field in keys:
        old_val = _serialize_value(old_data.get(field))
        new_val = _serialize_value(new_data.get(field))
        if old_val != new_val:
            changes[field] = {"old": old_val, "new": new_val}
    return changes


def _write_audit_log_async(payload):
    def _persist():
        from students.models import SecurityAuditLog

        SecurityAuditLog.objects.create(**payload)

    transaction.on_commit(lambda: threading.Thread(target=_persist, daemon=True).start())


def _audit_action(instance, action, changes=None):
    from students.models import SecurityAuditLog

    request = getattr(_pre_save_cache, "request", None)
    actor = None
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        actor = request.user
    elif hasattr(instance, "_audit_actor_id"):
        actor = User.objects.filter(pk=instance._audit_actor_id).first()

    model_name = f"{instance._meta.app_label}.{instance._meta.model_name}"
    tracked_fields = AUDITED_MODELS.get(instance.__class__)

    payload = {
        "actor": actor,
        "actor_id_snapshot": actor.pk if actor else None,
        "client_ip": _client_ip_from_request() if request else getattr(instance, "_audit_ip", "system"),
        "action": action,
        "target_model": model_name,
        "target_id": str(instance.pk) if instance.pk else "pending",
        "target_fields": list(changes.keys()) if changes else [],
        "old_values": {k: v["old"] for k, v in (changes or {}).items()},
        "new_values": {k: v["new"] for k, v in (changes or {}).items()},
        "school_id_snapshot": getattr(instance, "school_id", None),
    }

    if action == "delete":
        payload["old_values"] = model_to_dict(instance, fields=tracked_fields) if tracked_fields else {}
        payload["new_values"] = {}

    _write_audit_log_async(payload)
    logger.info(
        "AUDIT %s %s id=%s actor=%s ip=%s fields=%s",
        action.upper(),
        model_name,
        instance.pk,
        payload["actor_id_snapshot"],
        payload["client_ip"],
        payload["target_fields"],
    )


def connect_audit_signals():
    from students.models import Exam, Mark

    register_audit_model(
        Mark,
        fields=[
            "student",
            "subject",
            "score",
            "raw_score",
            "maximum_marks",
            "is_absent",
            "term",
            "year",
            "exam_type",
            "performance_level",
            "points",
            "integrity_checksum",
        ],
    )
    register_audit_model(
        Exam,
        fields=["name", "term", "year", "status", "integrity_checksum"],
    )
    register_audit_model(
        User,
        fields=["username", "email", "first_name", "last_name", "is_active", "is_staff", "is_superuser"],
    )

    @receiver(pre_save, sender=Mark)
    @receiver(pre_save, sender=Exam)
    @receiver(pre_save, sender=User)
    def capture_pre_save_state(sender, instance, **kwargs):
        if not instance.pk:
            instance._audit_is_create = True
            return
        tracked = AUDITED_MODELS.get(sender)
        try:
            previous = sender.objects.get(pk=instance.pk)
            instance._audit_previous = model_to_dict(previous, fields=tracked)
        except sender.DoesNotExist:
            instance._audit_previous = {}

    @receiver(post_save, sender=Mark)
    @receiver(post_save, sender=Exam)
    @receiver(post_save, sender=User)
    def audit_post_save(sender, instance, created, **kwargs):
        tracked = AUDITED_MODELS.get(sender)
        if created:
            current = model_to_dict(instance, fields=tracked)
            changes = {field: {"old": None, "new": _serialize_value(current.get(field))} for field in current}
            _audit_action(instance, "create", changes)
            return

        previous = getattr(instance, "_audit_previous", {})
        current = model_to_dict(instance, fields=tracked)
        changes = _diff_instances(previous, current, tracked)
        if changes:
            _audit_action(instance, "update", changes)

    @receiver(post_delete, sender=Mark)
    @receiver(post_delete, sender=Exam)
    @receiver(post_delete, sender=User)
    def audit_post_delete(sender, instance, **kwargs):
        _audit_action(instance, "delete")
