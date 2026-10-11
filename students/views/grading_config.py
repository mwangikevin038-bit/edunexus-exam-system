"""
Grading Configuration view for school administrators.

Allows the school admin to configure performance level scales
per section (Lower Primary, Primary, JSS) for both individual
subjects and aggregate/total marks.

Two stores exist for these scales:

  * ``GradingScale`` + ``GradingAssignment`` — the LIVE store read by
    ``grading_engine.resolve_scale_fast`` on every mark grade and report card.
  * ``GradingConfig`` — the legacy store still read by the
    ``check_grading_config`` and ``recalculate_primary_grading`` commands.

Every save writes BOTH, keyed identically, so the page, the runtime grader
and the management commands can never disagree. The page reads the live
store first: what you see is what the grader will use.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render

from ..models import GradingAssignment, GradingConfig, GradingScale, Mark
from ..security import (
    get_request_school,
    get_request_school_section,
    school_admin_required,
)

SECTION_MAP = {
    'LOWER_PRIMARY': 'LOWER_PRIMARY',
    'PRIMARY': 'PRIMARY',
    'JSS': 'JSS',
}

SECTION_LABELS = {
    'LOWER_PRIMARY': 'Lower Primary (Grades 1-3)',
    'PRIMARY': 'Primary (Grades 4-6)',
    'JSS': 'Junior Secondary (Grades 7-9)',
}

# Section hierarchy: defines which sections are grouped together
# When a section is selected, only its group members are shown as tabs
SECTION_GROUPS = {
    'LOWER_PRIMARY': ['LOWER_PRIMARY', 'PRIMARY'],
    'PRIMARY': ['LOWER_PRIMARY', 'PRIMARY'],
    'JSS': ['JSS'],
}


def _runtime_key(section, sub_section=None):
    """Map a page tab (+ posted sub-section) onto the runtime assignment key.

    grading_engine looks scales up by (school_section, sub_section) and its
    fallback chain only ever reaches these three keys:
        JSS            -> ('JSS', None)
        Lower Primary  -> ('PRIMARY', 'LOWER')
        Upper Primary  -> ('PRIMARY', 'UPPER')
    """
    if section == 'JSS':
        return 'JSS', None
    if section == 'LOWER_PRIMARY':
        return 'PRIMARY', 'LOWER'
    if section == 'PRIMARY':
        return 'PRIMARY', sub_section if sub_section in ('UPPER', 'LOWER') else 'UPPER'
    return section, sub_section


def _resolve_config(school, section, sub_section=None):
    """Return (config, is_default) for a tab — live store first.

    ``config`` is a ``GradingScale`` or ``GradingConfig`` (both expose
    ``subject_scale`` / ``total_scale`` / ``get_subject_level``), or ``None``
    when neither store holds data for this section.
    """
    rt_sec, rt_sub = _runtime_key(section, sub_section)

    assignment = GradingAssignment.objects.filter(
        school=school, school_section=rt_sec, sub_section=rt_sub, subject__isnull=True,
    ).select_related('grading_scale').first()
    if assignment:
        scale = assignment.grading_scale
        if scale.subject_scale or scale.total_scale:
            return scale, False

    config = GradingConfig.all_objects.filter(
        school=school, school_section=rt_sec, sub_section=rt_sub,
    ).first()
    if not config:
        config = GradingConfig.all_objects.filter(
            school=school, school_section=section,
        ).first()
    if config and (config.subject_scale or config.total_scale):
        return config, False

    return None, True


class _DefaultScale:
    """Read-only view of the built-in defaults, for sections with no stored scale.

    Replaces the old write-on-GET row creation — the page now shows defaults
    without mutating the database.
    """

    def __init__(self, section):
        self.school_section = section
        self.sub_section = None
        self.subject_scale = GradingConfig.get_default_subject_scale(section)
        self.total_scale = GradingConfig.get_default_total_scale(section)


def _parse_scale(raw):
    try:
        data = json.loads(raw or '[]')
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _upsert_grading_config(school, rt_sec, rt_sub, label, subject_scale, total_scale):
    """Mirror the change into the legacy store, keyed like the live store."""
    fields = {}
    if subject_scale is not None:
        fields['subject_scale'] = subject_scale
    if total_scale is not None:
        fields['total_scale'] = total_scale
    if not fields:
        return

    row = GradingConfig.all_objects.filter(
        school=school, school_section=rt_sec, sub_section=rt_sub,
    ).first()
    if row:
        for attr, value in fields.items():
            setattr(row, attr, value)
        row.save()
        return

    GradingConfig.all_objects.create(
        school=school,
        name=label,
        school_section=rt_sec,
        sub_section=rt_sub,
        **fields,
    )


def _upsert_runtime_scale(school, section, rt_sec, rt_sub, label, subject_scale, total_scale):
    """Write the change into the store grading_engine actually reads."""
    assignment = GradingAssignment.objects.filter(
        school=school, school_section=rt_sec, sub_section=rt_sub, subject__isnull=True,
    ).select_related('grading_scale').first()

    if assignment is None:
        # Seed whichever scale the admin did not just edit from the legacy
        # store (already written above) so the tab never renders one empty
        # half after a first save.
        mirror = GradingConfig.all_objects.filter(
            school=school, school_section=rt_sec, sub_section=rt_sub,
        ).first()
        if subject_scale is None:
            subject_scale = (
                (mirror.subject_scale if mirror else None)
                or GradingConfig.get_default_subject_scale(section)
            )
        if total_scale is None:
            total_scale = (
                (mirror.total_scale if mirror else None)
                or GradingConfig.get_default_total_scale(section)
            )
        scale = GradingScale.objects.create(
            school=school,
            name=label,
            subject_scale=subject_scale,
            total_scale=total_scale,
        )
        GradingAssignment.objects.update_or_create(
            school=school,
            school_section=rt_sec,
            sub_section=rt_sub,
            subject=None,
            defaults={'grading_scale': scale},
        )
        return scale

    scale = assignment.grading_scale
    patch = {}
    if subject_scale is not None and scale.subject_scale != subject_scale:
        patch['subject_scale'] = subject_scale
    if total_scale is not None and scale.total_scale != total_scale:
        patch['total_scale'] = total_scale
    if not patch:
        return scale

    if scale.assignments.count() > 1:
        # The scale is shared with other assignments — give this section its
        # own copy instead of silently editing somebody else's brackets.
        fresh = {'subject_scale': scale.subject_scale, 'total_scale': scale.total_scale}
        fresh.update(patch)
        scale = GradingScale.objects.create(school=school, name=scale.name, **fresh)
        assignment.grading_scale = scale
        assignment.save(update_fields=['grading_scale'])
        return scale

    for attr, value in patch.items():
        setattr(scale, attr, value)
    scale.save(update_fields=list(patch))
    return scale


def _invalidate_grading_caches(school):
    """Drop every cache that would otherwise keep serving the old brackets."""
    from .grading_engine import clear_grading_cache, prefetch_school_grading
    from .helpers import invalidate_report_forms_cache

    clear_grading_cache()
    prefetch_school_grading(school)
    # invalidate_report_forms_cache never raises.
    invalidate_report_forms_cache(school.pk)


def _persist_scale(school, section, sub_section, label, subject_scale=None, total_scale=None):
    """Write the scale to both stores and refresh the caches."""
    rt_sec, rt_sub = _runtime_key(section, sub_section)
    _upsert_grading_config(school, rt_sec, rt_sub, label, subject_scale, total_scale)
    _upsert_runtime_scale(school, section, rt_sec, rt_sub, label, subject_scale, total_scale)
    _invalidate_grading_caches(school)
    # Grades live on the Mark row and in the ExamSummary snapshot, both
    # written once at capture/publish time. Refreshing the scale cache alone
    # left every already-printed grade on the old brackets, and the school-wide
    # merit list kept serving the levels it had already rendered.
    from .helpers import (
        invalidate_school_grade_caches,
        regrade_exam_summaries_for_school,
        regrade_marks_for_school,
    )
    invalidate_school_grade_caches(school.pk)
    regrade_marks_for_school(school, section=section)
    regrade_exam_summaries_for_school(school)


def _marks_in_use(school, section_key):
    """How many marks this tab's scale would grade."""
    if section_key == 'JSS':
        query = Q(school_section='JSS')
    elif section_key == 'LOWER_PRIMARY':
        query = Q(school_section='PRIMARY', sub_section='LOWER')
    else:
        query = Q(school_section='PRIMARY') & (
            Q(sub_section='UPPER') | Q(sub_section__isnull=True)
        )
    return Mark.all_objects.filter(query, school=school).count()


@login_required(login_url='login')
@school_admin_required
def grading_configuration(request):
    """
    Grading Configuration page for school admin.
    Displays and allows editing of performance level scales
    for each section (Lower Primary, Primary, JSS).
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    active_section = request.GET.get('section')
    if not active_section or active_section not in SECTION_MAP:
        ws = get_request_school_section(request)
        active_section = ws if ws in SECTION_MAP else 'PRIMARY'

    # Determine which sections this user can access
    is_both_admin = request.session.get('school_section') == 'BOTH'
    current_ws = get_request_school_section(request)
    if is_both_admin:
        available_sections = list(SECTION_MAP.keys())
    elif current_ws == 'JSS':
        available_sections = ['JSS']
    elif current_ws == 'LOWER_PRIMARY':
        available_sections = ['LOWER_PRIMARY']
    elif current_ws == 'PRIMARY':
        available_sections = ['LOWER_PRIMARY', 'PRIMARY']
    else:
        available_sections = list(SECTION_MAP.keys())

    if request.method == 'POST':
        action = request.POST.get('action')

        if action in ('save_subject_scale', 'save_total_scale',
                      'reset_subject_scale', 'reset_total_scale'):
            section = request.POST.get('section', active_section)
            if section not in available_sections:
                messages.error(request, "You do not have access to that section.")
                return redirect(request.path)
            sub_section = request.POST.get('sub_section', '').strip() or None

            is_subject = action in ('save_subject_scale', 'reset_subject_scale')
            if action in ('save_subject_scale', 'save_total_scale'):
                data = _parse_scale(request.POST.get('scale_data', '[]'))
            elif action == 'reset_subject_scale':
                data = GradingConfig.get_default_subject_scale(section)
            else:
                data = GradingConfig.get_default_total_scale(section)

            label = SECTION_LABELS.get(section, section)
            _persist_scale(
                school, section, sub_section, label,
                subject_scale=data if is_subject else None,
                total_scale=None if is_subject else data,
            )

            kind = 'Subject' if is_subject else 'Total marks'
            if action.startswith('save'):
                messages.success(request, f"{kind} grading scale saved for {label}.")
            else:
                messages.success(request, f"{kind} scale reset to defaults for {label}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'test_score':
            # Returns JSON: what level + points would a given score get?
            try:
                score = float(request.POST.get('score', -1))
            except (TypeError, ValueError):
                return JsonResponse({'error': 'Invalid score.'}, status=400)
            section = request.POST.get('section', active_section)
            if section not in available_sections:
                return JsonResponse({'error': 'Access denied.'}, status=403)
            sub_section = request.POST.get('sub_section', '').strip() or None
            config, _is_default = _resolve_config(school, section, sub_section)
            if config is None:
                return JsonResponse({'error': 'No config for this section.'}, status=404)
            level, points = config.get_subject_level(score)
            return JsonResponse({'score': score, 'level': level, 'points': points})

    # GET — resolve each tab from the live store (falling back to the legacy
    # store, then to the built-in defaults). Never writes to the database.
    configs = {}
    for section_key in SECTION_MAP:
        config, _is_default = _resolve_config(school, section_key)
        configs[section_key] = config if config is not None else _DefaultScale(section_key)
    primary_upper, _ = _resolve_config(school, 'PRIMARY')
    primary_lower, _ = _resolve_config(school, 'LOWER_PRIMARY')

    if active_section == 'LOWER_PRIMARY':
        active_config = configs['LOWER_PRIMARY']
    elif active_section == 'PRIMARY':
        active_config = configs['PRIMARY']
    else:
        active_config = configs.get(active_section, configs['PRIMARY'])

    # Show tabs filtered by the current workspace:
    #   JSS workspace       -> JSS only
    #   LOWER_PRIMARY       -> LOWER_PRIMARY only
    #   PRIMARY             -> LOWER_PRIMARY + PRIMARY  (one institution, two sub-scales)
    # In-use stats: how many marks are currently using each scale?
    marks_in_use = {}
    for section_key in SECTION_MAP:
        marks_in_use[section_key] = (
            _marks_in_use(school, section_key) if section_key in available_sections else 0
        )

    # Sanity-check the active config: detect overlapping ranges
    def _detect_overlaps(scale, key_min, key_max):
        """Return list of overlapping (i, j) index pairs."""
        overlaps = []
        for i, a in enumerate(scale):
            for j, b in enumerate(scale):
                if j <= i:
                    continue
                if a[key_min] <= b[key_max] and b[key_min] <= a[key_max]:
                    overlaps.append((i, j))
        return overlaps
    subject_overlaps = _detect_overlaps(active_config.subject_scale or [], 'min_score', 'max_score')
    total_overlaps   = _detect_overlaps(active_config.total_scale or [],   'min_marks', 'max_marks')

    response = render(request, 'students/grading_configuration.html', {
        'configs': configs,
        'primary_upper': primary_upper,
        'primary_lower': primary_lower,
        'active_section': active_section,
        'active_config': active_config,
        'section_labels': SECTION_LABELS,
        'available_sections': available_sections,
        'current_workspace': current_ws,
        'marks_in_use': marks_in_use,
        'subject_overlaps': subject_overlaps,
        'total_overlaps': total_overlaps,
    })
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response
