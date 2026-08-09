"""
Unified Grading Engine — single source of truth for grade resolution.

Cache key: (school_id, school_section, sub_section, subject_id_or_None)

Lookup chain:
  - subject_id=None (totals): use general fallback row → return scale.total_scale
  - subject_id set: try subject row first, fallback to general → return scale.subject_scale

Usage:
    from .grading_engine import prefetch_school_grading, resolve_scale_fast, get_grading_scale

    prefetch_school_grading(school)
    scale_data = resolve_scale_fast(school.pk, section, sub_section, subject_id)
    scale_obj  = get_grading_scale(school.pk, section, sub_section, subject_id)
"""
import logging

from ..models import GradingAssignment

logger = logging.getLogger("students.grading_engine")

# Key: (school_id, school_section, sub_section, subject_id_or_None)
# Value: GradingScale instance
_global_grading_cache = {}


def prefetch_school_grading(school):
    _global_grading_cache.clear()
    if not school:
        return
    assignments = GradingAssignment.objects.filter(
        school=school,
    ).select_related('grading_scale', 'subject')
    for assign in assignments:
        key = (assign.school_id, assign.school_section, assign.sub_section, assign.subject_id)
        _global_grading_cache[key] = assign.grading_scale
    logger.debug("Prefetched %d grading assignments for school_id=%s", len(assignments), school.pk)


def resolve_scale_fast(school_id, section, sub_section, subject_id=None, is_total_calculation=False):
    """Return the scale DATA list (subject_scale or total_scale)."""
    if is_total_calculation:
        scale = _global_grading_cache.get((school_id, section, sub_section, None))
        return scale.total_scale if scale else []

    scale = _global_grading_cache.get((school_id, section, sub_section, subject_id))
    if not scale:
        scale = _global_grading_cache.get((school_id, section, sub_section, None))
    return scale.subject_scale if scale else []


def get_grading_scale(school_id, section, sub_section, subject_id=None):
    """Return the raw GradingScale instance (or None).

    Use this when you need to access both .subject_scale and .total_scale,
    or when you need to inspect the scale object directly.
    """
    scale = _global_grading_cache.get((school_id, section, sub_section, subject_id))
    if not scale:
        scale = _global_grading_cache.get((school_id, section, sub_section, None))
    return scale


def clear_grading_cache():
    _global_grading_cache.clear()
