"""
Grading Configuration view for school administrators.

Allows the school admin to configure performance level scales
per section (Lower Primary, Primary, JSS) for both individual
subjects and aggregate/total marks.
"""

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render

from ..models import GradingConfig
from ..security import get_request_school, get_request_school_section, school_admin_required


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

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'save_subject_scale':
            section = request.POST.get('section', active_section)
            sub_section = request.POST.get('sub_section', '').strip() or None
            try:
                scale_data = json.loads(request.POST.get('scale_data', '[]'))
            except json.JSONDecodeError:
                scale_data = []

            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                sub_section=sub_section,
                defaults={'subject_scale': scale_data},
            )
            label = SECTION_LABELS.get(section, section)
            if sub_section:
                label = f"{label} ({sub_section.title()})"
            messages.success(request, f"Subject grading scale saved for {label}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'save_total_scale':
            section = request.POST.get('section', active_section)
            sub_section = request.POST.get('sub_section', '').strip() or None
            try:
                scale_data = json.loads(request.POST.get('scale_data', '[]'))
            except json.JSONDecodeError:
                scale_data = []

            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                sub_section=sub_section,
                defaults={'total_scale': scale_data},
            )
            label = SECTION_LABELS.get(section, section)
            if sub_section:
                label = f"{label} ({sub_section.title()})"
            messages.success(request, f"Total marks scale saved for {label}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'reset_subject_scale':
            section = request.POST.get('section', active_section)
            sub_section = request.POST.get('sub_section', '').strip() or None
            default_scale = GradingConfig.get_default_subject_scale(section)
            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                sub_section=sub_section,
                defaults={'subject_scale': default_scale},
            )
            label = SECTION_LABELS.get(section, section)
            if sub_section:
                label = f"{label} ({sub_section.title()})"
            messages.success(request, f"Subject scale reset to defaults for {label}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'reset_total_scale':
            section = request.POST.get('section', active_section)
            sub_section = request.POST.get('sub_section', '').strip() or None
            default_scale = GradingConfig.get_default_total_scale(section)
            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                sub_section=sub_section,
                defaults={'total_scale': default_scale},
            )
            label = SECTION_LABELS.get(section, section)
            if sub_section:
                label = f"{label} ({sub_section.title()})"
            messages.success(request, f"Total marks scale reset to defaults for {label}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'test_score':
            # Returns JSON: what level + points would a given score get?
            try:
                score = float(request.POST.get('score', -1))
            except (TypeError, ValueError):
                return JsonResponse({'error': 'Invalid score.'}, status=400)
            section = request.POST.get('section', active_section)
            sub_section = request.POST.get('sub_section', '').strip() or None
            # Try sub-section-specific config first
            config = None
            if sub_section:
                config = GradingConfig.all_objects.filter(
                    school=school, school_section=section, sub_section=sub_section
                ).first()
            if not config:
                config = GradingConfig.all_objects.filter(
                    school=school, school_section=section
                ).first()
            if not config or not config.subject_scale:
                return JsonResponse({'error': 'No config for this section.'}, status=404)
            level, points = config.get_subject_level(score)
            return JsonResponse({'score': score, 'level': level, 'points': points})

    # GET — load configs for all sections. Note: PRIMARY has TWO rows now
    # (LOWER and UPPER). We use .filter().first() so multiple matches don't crash.
    configs = {}
    for section_key in SECTION_MAP:
        config = GradingConfig.all_objects.filter(
            school=school, school_section=section_key
        ).first()
        if not config:
            config = GradingConfig.all_objects.create(
                school=school,
                school_section=section_key,
                subject_scale=GradingConfig.get_default_subject_scale(section_key),
                total_scale=GradingConfig.get_default_total_scale(section_key),
            )
        configs[section_key] = config

    # Also load the UPPER and LOWER primary configs (used by the Primary workspace)
    primary_upper = GradingConfig.all_objects.filter(
        school=school, school_section='PRIMARY', sub_section='UPPER'
    ).first()
    primary_lower = GradingConfig.all_objects.filter(
        school=school, school_section='PRIMARY', sub_section='LOWER'
    ).first()
    configs['PRIMARY_UPPER'] = primary_upper
    configs['PRIMARY_LOWER'] = primary_lower

    # The active_config is the one shown on the page (based on active_section)
    if active_section in ('PRIMARY', 'LOWER_PRIMARY'):
        # For primary workspaces, default to the UPPER scale (more common)
        active_config = primary_upper or primary_lower or configs.get('PRIMARY')
    else:
        active_config = configs.get(active_section, configs.get('PRIMARY'))

    # Show tabs filtered by the current workspace:
    #   JSS workspace       -> JSS only
    #   LOWER_PRIMARY       -> LOWER_PRIMARY only
    #   PRIMARY             -> LOWER_PRIMARY + PRIMARY  (one institution, two sub-scales)
    #   BOTH (school admin) -> all three
    #
    # Read directly from session because get_request_school_section() collapses
    # BOTH into a specific workspace.
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

    # In-use stats: how many marks are currently using each scale?
    from ..models import Mark
    marks_in_use = {}
    for section_key in SECTION_MAP:
        marks_in_use[section_key] = Mark.all_objects.filter(
            school=school, school_section=section_key
        ).count()

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

    return render(request, 'students/grading_configuration.html', {
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
