"""
Data protection layer for EduNexus.
Provides:
1. Backup-before-delete for marks
2. Full audit trail for mark and student changes
3. Role-based write protection for critical operations
"""
import json
import logging

from django.contrib.auth.models import User
from django.db import transaction

logger = logging.getLogger("students.security.protection")


def backup_marks_before_delete(queryset, reason, request=None, actor=None):
    """
    Create a snapshot of all marks in the queryset BEFORE they are deleted.
    Must be called INSIDE a transaction.atomic() block, BEFORE the .delete() call.
    
    Returns the MarkBackup instance.
    """
    from students.models import MarkBackup
    from students.school_scope import get_current_school

    marks = list(queryset.values(
        'id', 'student_id', 'subject_id', 'score', 'raw_score',
        'maximum_marks', 'is_absent', 'performance_level', 'points',
        'term', 'year', 'exam_type', 'primary_raw_score',
        'primary_performance_point', 'primary_descriptor',
        'school_section', 'sub_section', 'school_id',
    ))

    if not marks:
        return None

    school = get_current_school()
    actor_user = actor
    client_ip = None

    if request and hasattr(request, 'user') and request.user.is_authenticated:
        actor_user = request.user
    if request:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.META.get("REMOTE_ADDR")

    backup = MarkBackup(
        school_id=school.pk if school else marks[0].get('school_id'),
        snapshot_reason=reason,
        snapshot_data=marks,
        mark_count=len(marks),
        actor=actor_user,
        client_ip=client_ip,
    )
    backup.save()

    logger.warning(
        "BACKUP CREATED: %d marks backed up before deletion. reason=%s actor=%s",
        len(marks), reason, actor_user.pk if actor_user else "system",
    )

    return backup


def log_mark_audit(mark, action, request=None, old_values=None):
    """
    Write to MarkAuditLog for every mark create/update/delete.
    This is the detailed per-mark audit trail.
    """
    from students.models import MarkAuditLog

    actor = None
    client_ip = None

    if request and hasattr(request, 'user') and request.user.is_authenticated:
        actor = request.user
    if request:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.META.get("REMOTE_ADDR")

    if action == "delete" and old_values:
        MarkAuditLog.objects.create(
            actor=actor,
            school_id=mark.school_id if hasattr(mark, 'school_id') else None,
            action=action,
            student_id=old_values.get('student_id', mark.student_id if hasattr(mark, 'student_id') else None),
            subject_id=old_values.get('subject_id', mark.subject_id if hasattr(mark, 'subject_id') else None),
            term=old_values.get('term', ''),
            year=old_values.get('year', 0),
            exam_type=old_values.get('exam_type', ''),
            old_raw_score=old_values.get('raw_score'),
            old_score=old_values.get('score'),
            old_is_absent=old_values.get('is_absent'),
            client_ip=client_ip,
        )
    elif action == "create":
        MarkAuditLog.objects.create(
            actor=actor,
            school_id=mark.school_id if hasattr(mark, 'school_id') else None,
            action=action,
            student_id=mark.student_id,
            subject_id=mark.subject_id,
            term=mark.term,
            year=mark.year,
            exam_type=mark.exam_type or '',
            new_raw_score=mark.raw_score,
            new_score=mark.score,
            new_is_absent=mark.is_absent,
            new_performance_level=mark.performance_level,
            client_ip=client_ip,
        )
    elif action == "update":
        MarkAuditLog.objects.create(
            actor=actor,
            school_id=mark.school_id if hasattr(mark, 'school_id') else None,
            action=action,
            student_id=mark.student_id,
            subject_id=mark.subject_id,
            term=mark.term,
            year=mark.year,
            exam_type=mark.exam_type or '',
            old_raw_score=old_values.get('raw_score') if old_values else None,
            old_score=old_values.get('score') if old_values else None,
            old_is_absent=old_values.get('is_absent') if old_values else None,
            new_raw_score=mark.raw_score,
            new_score=mark.score,
            new_is_absent=mark.is_absent,
            new_performance_level=mark.performance_level,
            client_ip=client_ip,
        )


def log_student_stream_change(student, old_stream, new_stream, request=None):
    """
    Log when a student's stream is changed. This is critical for tracking misplacements.
    """
    from students.models import SecurityAuditLog
    from students.security.integrity import compute_audit_record_hash

    actor = None
    client_ip = None

    if request and hasattr(request, 'user') and request.user.is_authenticated:
        actor = request.user
    if request:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.META.get("REMOTE_ADDR")

    old_values = {"stream": old_stream, "class_name": student.class_name}
    new_values = {"stream": new_stream, "class_name": student.class_name}

    SecurityAuditLog.objects.create(
        actor=actor,
        actor_id_snapshot=actor.pk if actor else None,
        client_ip=client_ip,
        action="update",
        target_model="students.student",
        target_id=str(student.pk),
        target_fields=["stream"],
        old_values=old_values,
        new_values=new_values,
        school_id_snapshot=student.school_id if hasattr(student, 'school_id') else None,
    )

    logger.warning(
        "STUDENT STREAM CHANGE: %s %s moved from %s to %s by %s",
        student.admission_no, student.name, old_stream, new_stream,
        actor.pk if actor else "system",
    )


def require_admin_for_destructive(view_func):
    """
    Decorator that blocks destructive operations (delete, bulk update) 
    unless the user is a school admin or superuser.
    """
    import functools
    from django.contrib import messages
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import redirect
    from students.security.roles import user_has_main_school_admin_override

    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if not user_has_main_school_admin_override(request.user):
                logger.warning(
                    "RBAC BLOCK: non-admin user %s attempted destructive operation on %s",
                    request.user.pk, request.path,
                )
                messages.error(request, "Only the school admin can perform this operation.")
                return redirect("dashboard_alt")
        return view_func(request, *args, **kwargs)
    return wrapper


def backup_marks_for_student_stream_move(student_ids, old_stream, reason, request=None):
    """
    Backup all marks for students being moved between streams.
    """
    from students.models import Mark, MarkBackup
    from students.school_scope import get_current_school

    marks_qs = Mark.all_objects.filter(student_id__in=student_ids)
    marks = list(marks_qs.values(
        'id', 'student_id', 'subject_id', 'score', 'raw_score',
        'maximum_marks', 'is_absent', 'performance_level', 'points',
        'term', 'year', 'exam_type', 'primary_raw_score',
        'primary_performance_point', 'primary_descriptor',
        'school_section', 'sub_section', 'school_id',
    ))

    if not marks:
        return None

    school = get_current_school()
    actor = None
    client_ip = None
    if request and hasattr(request, 'user') and request.user.is_authenticated:
        actor = request.user
    if request:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.META.get("REMOTE_ADDR")

    backup = MarkBackup(
        school_id=school.pk if school else marks[0].get('school_id'),
        snapshot_reason=f"{reason}: {len(student_ids)} students moved from {old_stream}",
        snapshot_data=marks,
        mark_count=len(marks),
        actor=actor,
        client_ip=client_ip,
    )
    backup.save()
    return backup
