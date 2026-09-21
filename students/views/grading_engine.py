"""
Unified Grading Engine — production-safe source of truth for grade resolution.
Uses Django's native thread-safe cache framework to support high-concurrency environments.
"""
import logging
from django.core.cache import cache
from ..models import GradingAssignment

logger = logging.getLogger("students.grading_engine")

_GRADED_CACHE_KEYS = set()


def _make_safe_cache_key(school_id, section, sub_section, subject_id):
    """
    Helper to create a unified, production-safe cache key string.
    Replaces spaces with underscores to prevent Memcached/Redis crashes.
    """
    # Force components to strings, strip spaces, and replace internal spaces with underscores
    safe_section = str(section).strip().replace(" ", "_")
    safe_sub = str(sub_section).strip().replace(" ", "_")
    sub_id = subject_id if subject_id is not None else "TOTAL"
    
    return f"grading_scale:{school_id}:{safe_section}:{safe_sub}:{sub_id}"


def prefetch_school_grading(school):
    if not school:
        return
    
    school_id = school.pk
    assignments = GradingAssignment.objects.filter(
        school=school,
    ).select_related('grading_scale', 'subject')
    
    for assign in assignments:
        key = _make_safe_cache_key(assign.school_id, assign.school_section, assign.sub_section, assign.subject_id)
        cache.set(key, assign.grading_scale, timeout=86400)
        _GRADED_CACHE_KEYS.add(key)
        
    logger.debug("Prefetched %d grading assignments for school_id=%s into global cache", len(assignments), school.pk)


def resolve_scale_fast(school_id, section, sub_section, subject_id=None, is_total_calculation=False):
    """Return the scale DATA list (subject_scale or total_scale)."""
    if is_total_calculation:
        key = _make_safe_cache_key(school_id, section, sub_section, None)
        scale = cache.get(key)
        return scale.total_scale if scale else []

    key = _make_safe_cache_key(school_id, section, sub_section, subject_id)
    scale = cache.get(key)
    
    if not scale:
        fallback_key = _make_safe_cache_key(school_id, section, sub_section, None)
        scale = cache.get(fallback_key)
        
    return scale.subject_scale if scale else []


def get_grading_scale(school_id, section, sub_section, subject_id=None):
    """Return the raw GradingScale instance (or None)."""
    key = _make_safe_cache_key(school_id, section, sub_section, subject_id)
    scale = cache.get(key)
    
    if not scale:
        fallback_key = _make_safe_cache_key(school_id, section, sub_section, None)
        scale = cache.get(fallback_key)
        
    return scale


def clear_grading_cache():
    if _GRADED_CACHE_KEYS:
        cache.delete_many(list(_GRADED_CACHE_KEYS))
        _GRADED_CACHE_KEYS.clear()
    
    from .helpers import _subject_lookup_cache, _total_lookup_cache
    _subject_lookup_cache.clear()
    _total_lookup_cache.clear()
