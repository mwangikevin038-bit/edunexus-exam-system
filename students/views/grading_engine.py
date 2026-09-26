"""
Unified Grading Engine — production-safe source of truth for grade resolution.
Uses Django's native thread-safe cache framework to support high-concurrency environments.
"""
import logging
import time
from django.core.cache import cache
from ..models import GradingAssignment

logger = logging.getLogger("students.grading_engine")

_GRADED_CACHE_KEYS = set()

# Process-local memo of fully-resolved scale data.
# Each resolve_scale_fast call used to walk candidate Redis keys one GET at a
# time; on Windows that costs ~40ms per roundtrip (delayed ACK), so a single
# snapshot build issued 1600+ GETs and blocked for over a minute. Memoizing
# the resolved result + batched get_many keeps a request to 1-2 Redis calls.
_RESOLVED_MEMO = {}
_RESOLVED_MEMO_TTL = 60.0


def _memo_get(mkey):
    hit = _RESOLVED_MEMO.get(mkey)
    if not hit:
        return None
    ts, data = hit
    if time.monotonic() - ts > _RESOLVED_MEMO_TTL:
        _RESOLVED_MEMO.pop(mkey, None)
        return None
    return data


def _memo_put(mkey, data):
    _RESOLVED_MEMO[mkey] = (time.monotonic(), data)


def _memo_clear_school(school_id):
    for k in [k for k in _RESOLVED_MEMO if k[0] == school_id]:
        _RESOLVED_MEMO.pop(k, None)


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
    
    mapping = {}
    for assign in assignments:
        key = _make_safe_cache_key(assign.school_id, assign.school_section, assign.sub_section, assign.subject_id)
        mapping[key] = assign.grading_scale
        _GRADED_CACHE_KEYS.add(key)

    # Drop stale process memo first so the next resolve reads these values
    _memo_clear_school(school_id)
    if mapping:
        try:
            cache.set_many(mapping, timeout=86400)
        except Exception:
            for k, v in mapping.items():
                cache.set(k, v, timeout=86400)
        
    logger.debug("Prefetched %d grading assignments for school_id=%s into global cache", len(assignments), school.pk)


def _candidate_keys(school_id, section, sub_section, subject_id):
    """Ordered cache keys from most specific to most generic."""
    keys = []
    if subject_id is not None:
        keys.append(_make_safe_cache_key(school_id, section, sub_section, subject_id))
    keys.append(_make_safe_cache_key(school_id, section, sub_section, None))

    # Primary has UPPER/LOWER scales but callers often pass sub_section=None.
    section_s = str(section).strip()
    sub_s = None if sub_section is None else str(sub_section).strip()
    if section_s == 'PRIMARY' and not sub_s:
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'UPPER', subject_id if subject_id is not None else None))
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'LOWER', subject_id if subject_id is not None else None))
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'UPPER', None))
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'LOWER', None))
    if section_s == 'LOWER_PRIMARY':
        # Shared/wrong keys callers sometimes use
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'LOWER', subject_id if subject_id is not None else None))
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', 'LOWER', None))
        keys.append(_make_safe_cache_key(school_id, 'LOWER_PRIMARY', 'LOWER', None))
        keys.append(_make_safe_cache_key(school_id, 'PRIMARY', None, None))

    # de-dupe preserving order
    seen = set()
    ordered = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


def _db_scale_for_keys(school_id, keys_meta):
    """DB fallback: match GradingAssignment rows in the same priority as keys_meta."""
    from ..models import GradingAssignment
    qs = list(GradingAssignment.objects.filter(school_id=school_id).select_related('grading_scale'))
    fallback = None
    for section, sub, subject_id in keys_meta:
        for assign in qs:
            if assign.school_section != section:
                continue
            if (assign.sub_section or None) != sub:
                continue
            if subject_id is not None:
                if assign.subject_id != subject_id:
                    continue
            elif assign.subject_id is not None:
                continue
            return assign.grading_scale
        if fallback is None:
            for assign in qs:
                if assign.subject_id is None and assign.school_section == section:
                    fallback = assign.grading_scale
                    break
    return fallback


def resolve_scale_fast(school_id, section, sub_section, subject_id=None, is_total_calculation=False):
    """Return the scale DATA list (subject_scale or total_scale).

    Falls back through related section/sub keys, then DB, so mark entry
    with sub_section=None still finds the school's PRIMARY UPPER/LOWER scale.
    Results are memoized per-process (60s) and multi-key lookups use a single
    cache.get_many roundtrip — sequential per-key GETs were the publish bottleneck.
    """
    memo_key = (
        school_id,
        str(section).strip() if section is not None else None,
        str(sub_section).strip() if sub_section is not None else None,
        subject_id,
        bool(is_total_calculation),
    )
    cached = _memo_get(memo_key)
    if cached is not None:
        return cached

    keys = _candidate_keys(school_id, section, sub_section, subject_id)

    if keys:
        try:
            found = cache.get_many(keys)
        except Exception:
            found = {}
        for key in keys:
            scale = found.get(key)
            if not scale:
                continue
            data = scale.total_scale if is_total_calculation else scale.subject_scale
            if data:
                _memo_put(memo_key, data)
                return data

    # Rebuild priority as (section, sub, subject) tuples for DB fallback
    keys_meta = []
    for key in keys:
        # key format: grading_scale:{school}:{section}:{sub}:{subject|TOTAL}
        try:
            parts = key.split(':')
            if len(parts) >= 5 and parts[0] == 'grading_scale':
                sec = parts[2]
                sub = None if parts[3] in ('', 'None') else parts[3]
                sid = None if parts[4] == 'TOTAL' else int(parts[4])
                keys_meta.append((sec, sub, sid))
        except (ValueError, IndexError):
            continue

    scale = _db_scale_for_keys(school_id, keys_meta)
    if scale:
        data = scale.total_scale if is_total_calculation else scale.subject_scale
        if data:
            try:
                for assign in GradingAssignment.objects.filter(school_id=school_id, grading_scale=scale):
                    k = _make_safe_cache_key(assign.school_id, assign.school_section, assign.sub_section, assign.subject_id)
                    cache.set(k, scale, timeout=86400)
            except Exception:
                pass
            _memo_put(memo_key, data)
            return data

    # Memoize the miss briefly so a missing scale doesn't hammer Redis+DB on
    # every mark. prefetch_school_grading / clear_grading_cache invalidate it.
    _memo_put(memo_key, [])
    return []


def get_grading_scale(school_id, section, sub_section, subject_id=None):
    """Return the raw GradingScale instance (or None)."""
    key = _make_safe_cache_key(school_id, section, sub_section, subject_id)
    scale = cache.get(key)
    
    if not scale:
        fallback_key = _make_safe_cache_key(school_id, section, sub_section, None)
        scale = cache.get(fallback_key)
        
    return scale


def clear_grading_cache():
    _RESOLVED_MEMO.clear()
    if _GRADED_CACHE_KEYS:
        cache.delete_many(list(_GRADED_CACHE_KEYS))
        _GRADED_CACHE_KEYS.clear()
    
    from .helpers import _subject_lookup_cache, _total_lookup_cache
    _subject_lookup_cache.clear()
    _total_lookup_cache.clear()
