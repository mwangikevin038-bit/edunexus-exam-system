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
            try:
                scale_data = json.loads(request.POST.get('scale_data', '[]'))
            except json.JSONDecodeError:
                scale_data = []

            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                defaults={'subject_scale': scale_data},
            )
            messages.success(request, f"Subject grading scale saved for {SECTION_LABELS.get(section, section)}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'save_total_scale':
            section = request.POST.get('section', active_section)
            try:
                scale_data = json.loads(request.POST.get('scale_data', '[]'))
            except json.JSONDecodeError:
                scale_data = []

            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                defaults={'total_scale': scale_data},
            )
            messages.success(request, f"Total marks scale saved for {SECTION_LABELS.get(section, section)}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'reset_subject_scale':
            section = request.POST.get('section', active_section)
            default_scale = GradingConfig.get_default_subject_scale(section)
            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                defaults={'subject_scale': default_scale},
            )
            messages.success(request, f"Subject scale reset to defaults for {SECTION_LABELS.get(section, section)}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'reset_total_scale':
            section = request.POST.get('section', active_section)
            default_scale = GradingConfig.get_default_total_scale(section)
            config, _ = GradingConfig.all_objects.update_or_create(
                school=school,
                school_section=section,
                defaults={'total_scale': default_scale},
            )
            messages.success(request, f"Total marks scale reset to defaults for {SECTION_LABELS.get(section, section)}.")
            return redirect(f'{request.path}?section={section}')

        elif action == 'test_score':
            # Returns JSON: what level + points would a given score get?
            try:
                score = float(request.POST.get('score', -1))
            except (TypeError, ValueError):
                return JsonResponse({'error': 'Invalid score.'}, status=400)
            section = request.POST.get('section', active_section)
            config = GradingConfig.all_objects.filter(school=school, school_section=section).first()
            if not config or not config.subject_scale:
                return JsonResponse({'error': 'No config for this section.'}, status=404)
            level, points = config.get_subject_level(score)
            return JsonResponse({'score': score, 'level': level, 'points': points})

    # GET — load configs for all sections
    configs = {}
    for section_key in SECTION_MAP:
        try:
            config = GradingConfig.all_objects.get(school=school, school_section=section_key)
        except GradingConfig.DoesNotExist:
            config = GradingConfig.all_objects.create(
                school=school,
                school_section=section_key,
                subject_scale=GradingConfig.get_default_subject_scale(section_key),
                total_scale=GradingConfig.get_default_total_scale(section_key),
            )
        configs[section_key] = config

    active_config = configs.get(active_section, configs['PRIMARY'])

    # Show ALL sections as tabs (not grouped). Each section has its own scale.
    available_sections = list(SECTION_MAP.keys())

    # In-use stats: how many marks are currently using each scale?
    # We count by performance_level value at section level.
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
        'active_section': active_section,
        'active_config': active_config,
        'section_labels': SECTION_LABELS,
        'available_sections': available_sections,
        'marks_in_use': marks_in_use,
        'subject_overlaps': subject_overlaps,
        'total_overlaps': total_overlaps,
    })
