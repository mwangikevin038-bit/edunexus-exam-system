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


# ── Section / sub-section ↔ class mapping (single source of truth) ─────
# These three sets are the authoritative rule for which class_name
# belongs to which section. Every CSV upload, form, and view MUST
# validate against these — never hardcode a class range elsewhere.
LOWER_PRIMARY_CLASSES = frozenset({'Grade 1', 'Grade 2', 'Grade 3'})
UPPER_PRIMARY_CLASSES = frozenset({'Grade 4', 'Grade 5', 'Grade 6'})
JSS_CLASSES           = frozenset({'Grade 7', 'Grade 8', 'Grade 9'})

PRIMARY_CLASSES = LOWER_PRIMARY_CLASSES | UPPER_PRIMARY_CLASSES
ALL_VALID_CLASSES = PRIMARY_CLASSES | JSS_CLASSES


def classes_for_section(section):
    """
    Return the set of class names that belong to a given workspace section.
    section is one of: 'LOWER_PRIMARY', 'PRIMARY', 'JSS', 'BOTH'.
    Returns None for 'BOTH' (caller should decide).
    """
    if section == 'LOWER_PRIMARY':
        return LOWER_PRIMARY_CLASSES
    if section == 'PRIMARY':
        return UPPER_PRIMARY_CLASSES | LOWER_PRIMARY_CLASSES
    if section == 'JSS':
        return JSS_CLASSES
    if section == 'BOTH':
        return ALL_VALID_CLASSES
    return frozenset()


def section_for_class(class_name):
    """
    Return ('PRIMARY'|'JSS', 'LOWER'|'UPPER'|None) for a given class name.
    Returns (None, None) for unknown / empty.
    """
    if not class_name:
        return None, None
    if class_name in LOWER_PRIMARY_CLASSES:
        return 'PRIMARY', 'LOWER'
    if class_name in UPPER_PRIMARY_CLASSES:
        return 'PRIMARY', 'UPPER'
    if class_name in JSS_CLASSES:
        return 'JSS', None
    return None, None


def validate_rows_for_section(rows, section, class_field='class_name'):
    """
    Strict-validate every row in `rows` belongs to `section`.
    Accounts for PRIMARY containing both LOWER and UPPER sub-sections.
    """
    if section == 'PRIMARY':
        # Primary accepts both sub-sections (Grades 1-3 AND Grades 4-6)
        allowed = LOWER_PRIMARY_CLASSES | UPPER_PRIMARY_CLASSES
    else:
        allowed = classes_for_section(section)

    if not allowed:
        return False, [f"Unknown workspace section: {section!r}"], set()

    # Build a clean, lowercase look-up map stripped of any accidental padding spaces
    allowed_lower = {str(a).lower().strip(): a for a in allowed}

    errors = []
    offending = set()
    
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        cls = (row.get(class_field) or '').strip()
        if not cls:
            continue  
            
        # Clean the incoming class string for a completely safe match
        clean_cls = cls.lower().strip()
        
        if clean_cls not in allowed_lower:
            offending.add(cls)
            errors.append(
                f"Row {i}: class '{cls}' does not belong to workspace '{section}'. "
                f"Allowed: {sorted(allowed)}"
            )
            
    return (len(errors) == 0), errors, offending


LOWER_PRIMARY_SUBJECT_SHORT_MAP = {
    'ELA': 'ELA', 'KLA': 'KLA', 'MA': 'MA', 'ILA': 'ILA',
}

LOWER_PRIMARY_SUBJECT_NAMES = {
    'ELA': 'English Language Activities',
    'KLA': 'Kiswahili Language Activities',
    'MA': 'Mathematical Activities',
    'ILA': 'Integrated Learning Area',
}

# ── Grade/Stream/Term ────────────────────────────────────────────────────────
GRADE_CHOICES  = [c[0] for c in Student.CLASS_CHOICES]
TERM_CHOICES   = [c[0] for c in Student.TERM_CHOICES]


def _resolve_db_section(section):
    """Map workspace section to the DB school_section value used by Grade/Stream."""
    if section in ('LOWER_PRIMARY', 'PRIMARY'):
        return 'PRIMARY'
    return 'JSS'


def get_streams_for_school(school, section=None):
    """Return dynamic stream names from the Grade/Stream models for a school."""
    from students.models import Stream
    qs = Stream.all_objects.filter(school=school)
    if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
        qs = qs.filter(school_section=_resolve_db_section(section))
    return list(qs.values_list("name", flat=True).distinct().order_by("name"))


def get_grades_for_school(school, section=None):
    """Return dynamic grade names from the Grade model for a school."""
    from students.models import Grade
    qs = Grade.all_objects.filter(school=school)
    if section in ('LOWER_PRIMARY', 'PRIMARY', 'JSS'):
        qs = qs.filter(school_section=_resolve_db_section(section))
    return list(qs.values_list("name", flat=True).distinct().order_by("name"))


ORDERED_LEVELS = ['EE1', 'EE2', 'ME1', 'ME2', 'AE1', 'AE2', 'BE1', 'BE2']

PRIMARY_GRADE_CHOICES = ['Grade 4', 'Grade 5', 'Grade 6']
JSS_GRADE_CHOICES = ['Grade 7', 'Grade 8', 'Grade 9']

ASSESSMENT_MAP = {
    'opener': 'Opener Assessment',
    'mid':    'Mid Term Assessment',
    'end':    'End Term Assessment',
}

ASSESSMENT_SLUG_MAP = {name: slug for slug, name in ASSESSMENT_MAP.items()}

# ── Subject Display Order (broadsheet column order) ──────────────────────────
SUBJECT_DISPLAY_ORDER = {
    # JSS codes
    '901': 1, '902': 2, '903': 3, '905': 4, '906': 5,
    '907': 6, '908': 7, '909': 8, '911': 9, '912': 10,
    # Primary codes
    'ENG': 1, 'KIS': 2, 'MAT': 3, 'SCI': 4, 'AGR': 5,
    'SOC': 6, 'CRE': 7, 'IRE': 8, 'CAS': 9,
}


def sort_subjects(subject_list):
    """Sort a list of (code, short_name) tuples by SUBJECT_DISPLAY_ORDER."""
    return sorted(subject_list, key=lambda x: SUBJECT_DISPLAY_ORDER.get(x[0], 99))


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
