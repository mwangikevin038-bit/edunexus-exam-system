"""
Cryptographic integrity checksums for marks and exam records.
Detects direct database tampering outside the application layer.
"""
import hashlib
import hmac
import logging

from django.conf import settings

logger = logging.getLogger("students.security.integrity")


def _integrity_key():
    material = getattr(settings, "DATA_INTEGRITY_KEY", None) or settings.SECRET_KEY
    return hashlib.sha256(f"edunexus-integrity::{material}".encode("utf-8")).digest()


def compute_mark_checksum(mark):
    payload = "|".join(
        str(part)
        for part in (
            mark.school_id or "",
            mark.student_id or "",
            mark.subject or "",
            mark.score,
            mark.raw_score if mark.raw_score is not None else "",
            mark.maximum_marks,
            int(bool(mark.is_absent)),
            mark.term or "",
            mark.year or "",
            mark.exam_type or "",
            mark.performance_level or "",
            mark.points if mark.points is not None else "",
        )
    )
    return hmac.new(_integrity_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_exam_checksum(exam):
    payload = "|".join(
        str(part)
        for part in (
            exam.school_id or "",
            exam.name or "",
            exam.term or "",
            exam.year or "",
            exam.status or "",
        )
    )
    return hmac.new(_integrity_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_mark_checksum(mark):
    if not mark.integrity_checksum:
        logger.warning(
            "Mark integrity checksum missing — verification bypassed: "
            "mark_id=%s student_id=%s school_id=%s",
            mark.pk, mark.student_id, mark.school_id,
        )
        return True
    return hmac.compare_digest(mark.integrity_checksum, compute_mark_checksum(mark))


def verify_exam_checksum(exam):
    if not exam.integrity_checksum:
        logger.warning(
            "Exam integrity checksum missing — verification bypassed: "
            "exam_id=%s school_id=%s",
            exam.pk, exam.school_id,
        )
        return True
    return hmac.compare_digest(exam.integrity_checksum, compute_exam_checksum(exam))


def compute_audit_record_hash(payload: str) -> str:
    return hmac.new(_integrity_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
