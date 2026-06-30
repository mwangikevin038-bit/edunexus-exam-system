"""
Constants and lookup tables for the students views module.

Provides a single source of truth for subject choices, grade/stream/term lists,
performance scales, and assessment mappings used throughout the system.
"""

from students.models import Student, Subject

# ── Subject Choices (from Subject model) ─────────────────
def get_subject_choices(school=None, section=None):
    """Return subject choices from Subject model."""
    qs = Subject.objects.all()
    if school:
        qs = qs.filter(school=school)
    if section:
        qs = qs.filter(school_section=section)
    return [(s.code, s.name) for s in qs.order_by('grade', 'code')]

SUBJECT_CHOICES = []  # Populated dynamically via get_subject_choices()

SUBJECT_SHORT_MAP = {
    '901': 'ENG', '902': 'KIS', '903': 'MAT', '905': 'SCI',
    '906': 'AGR', '907': 'SOC', '908': 'CRE', '909': 'IRE',
    '911': 'C/A', '912': 'PRE',
}

PRIMARY_SUBJECT_SHORT_MAP = {
    'ENG': 'ENG', 'KIS': 'KIS', 'MAT': 'MAT', 'SCI': 'SCI',
    'SOC': 'SOC', 'CRE': 'CRE', 'IRE': 'IRE', 'AGR': 'AGR', 'CAS': 'CAS',
}

PRIMARY_SUBJECT_NAMES = {
    'ENG': 'English Language',
    'KIS': 'Kiswahili',
    'MAT': 'Mathematics',
    'SCI': 'Science and Technology',
    'SOC': 'Social Studies',
    'CRE': 'Christian Religious Education',
    'IRE': 'Islamic Religious Education',
    'AGR': 'Agriculture and Nutrition',
    'CAS': 'Creative Arts and Sports',
}

PRIMARY_PERF_LEVELS = ['EE', 'ME', 'AE', 'BE']

LOWER_PRIMARY_GRADE_CHOICES = ['Grade 1', 'Grade 2', 'Grade 3']

LOWER_PRIMARY_SUBJECT_SHORT_MAP = {
    'ELA': 'ELA', 'LIT': 'LIT', 'ENV': 'ENV', 'HYG': 'HYG',
    'CRE': 'CRE', 'IRE': 'IRE', 'HRE': 'HRE', 'MA': 'MA',
    'KLA': 'KLA', 'CRA': 'CRA',
}

LOWER_PRIMARY_SUBJECT_NAMES = {
    'ELA': 'English Language Activities',
    'LIT': 'Literacy Activities',
    'ENV': 'Environmental Activities',
    'HYG': 'Hygiene and Nutrition Activities',
    'CRE': 'Christian Religious Education Activities',
    'IRE': 'Islamic Religious Education Activities',
    'HRE': 'Hindu Religious Education Activities',
    'MA': 'Mathematical Activities',
    'KLA': 'Kiswahili Language Activities',
    'CRA': 'Creative Activities',
}

# ── Grade/Stream/Term ────────────────────────────────────────────────────────
# GRADE_CHOICES and TERM_CHOICES are system-wide (same for all tenants).
# STREAM_CHOICES is no longer hardcoded — use get_streams_for_school() instead.
GRADE_CHOICES  = [c[0] for c in Student.CLASS_CHOICES]
TERM_CHOICES   = [c[0] for c in Student.TERM_CHOICES]


def get_streams_for_school(school, section=None):
    """Return dynamic stream names from the Grade/Stream models for a school."""
    from students.models import Stream
    qs = Stream.all_objects.filter(school=school)
    if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
        qs = qs.filter(school_section=section)
    return list(qs.values_list("name", flat=True).distinct().order_by("name"))


def get_grades_for_school(school, section=None):
    """Return dynamic grade names from the Grade model for a school."""
    from students.models import Grade
    qs = Grade.all_objects.filter(school=school)
    if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
        qs = qs.filter(school_section=section)
    return list(qs.values_list("name", flat=True).distinct().order_by("name"))

PERFORMANCE_SCALE = [
    (90, 'EE1', 8),
    (75, 'EE2', 7),
    (58, 'ME1', 6),
    (41, 'ME2', 5),
    (31, 'AE1', 4),
    (21, 'AE2', 3),
    (11, 'BE1', 2),
    (0,  'BE2', 1),
]

ORDERED_LEVELS = ['EE1', 'EE2', 'ME1', 'ME2', 'AE1', 'AE2', 'BE1', 'BE2']

PRIMARY_GRADE_CHOICES = ['Grade 4', 'Grade 5', 'Grade 6']
JSS_GRADE_CHOICES = ['Grade 7', 'Grade 8', 'Grade 9']

ASSESSMENT_MAP = {
    'opener': 'Opener Assessment',
    'mid':    'Mid Term Assessment',
    'end':    'End Term Assessment',
}

ASSESSMENT_SLUG_MAP = {name: slug for slug, name in ASSESSMENT_MAP.items()}

RELIGION_SUBJECTS = ['908', '909', 'CRE', 'IRE']  # CRE, IRE (JSS + Primary)
OPPOSITE_RELIGION_SUBJECT = {
    '908': '909', '909': '908',
    'CRE': 'IRE', 'IRE': 'CRE',
}

# Maps subject code → religion tag for Student.religion field
RELIGION_TAG = {
    '908': 'CRE', '909': 'IRE',
    'CRE': 'CRE', 'IRE': 'IRE',
}
