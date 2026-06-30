"""
students/views/__init__.py
==========================
Re-exports all view functions so that `from students import views` and
`from students.views import login_view` continue to work after the
monolith split.

Module layout:
  constants.py    – Subject/grade/stream/performance lookup tables
  helpers.py      – Shared helper functions (access control, scoring, queries)
  auth.py         – Login, logout, password change, welcome page
  dashboard.py    – Teacher & admin dashboards, profile view
  students_mgmt.py – Student registration, CSV upload, class lists, promotions
  exams.py        – Mark entry, exam management, submission review, locks
  reports.py      – Results list, report cards (single & bulk)
  faculty.py      – Faculty matrix, subject comments, learner profile
  pdf_exports.py  – Playwright-based PDF downloads (broadsheet, class list)
  classes_manage.py – Grade/stream management
  csv_upload.py   – Premium CSV onboarding wizard & progress API
"""

# ── Constants ────────────────────────────────────────────────────────────────
from .constants import *  # noqa: F401,F403

# ── Helpers ──────────────────────────────────────────────────────────────────
from .helpers import *  # noqa: F401,F403

# ── Auth views ───────────────────────────────────────────────────────────────
from .auth import (  # noqa: F401
    welcome_page,
    logout_view,
    login_view,
    switch_workspace,
    custom_password_change,
)

# ── Dashboard views ──────────────────────────────────────────────────────────
from .dashboard import (  # noqa: F401
    profile_view,
    dashboard,
    school_admin_dashboard,
)

# ── Student management views ─────────────────────────────────────────────────
from .students_mgmt import (  # noqa: F401
    add_student,
    admin_add_student,
    api_streams_for_grade,
    class_lists,
)

# ── Exam & mark entry views ──────────────────────────────────────────────────
from .exams import (  # noqa: F401
    select_exam,
    select_exam_primary,
    clear_mark,
    save_mark,
    manage_exams,
    review_stream_submission,
    manage_assessment_locks,
)

# ── Results & report views ───────────────────────────────────────────────────
from .reports import (  # noqa: F401
    results_list,
    report_card_select,
    individual_report,
    bulk_report_cards,
)

# ── Faculty & comments views ─────────────────────────────────────────────────
from .faculty import (  # noqa: F401
    manage_master_comments,
    manage_headteacher_comments,
    manage_faculty_matrix,
    learner_profile,
)

# ── PDF export views ─────────────────────────────────────────────────────────
from .pdf_exports import (  # noqa: F401
    download_broadsheet_pdf,
    download_classlist_pdf,
)

# ── Class/stream management ──────────────────────────────────────────────────
from .classes_manage import (  # noqa: F401
    manage_classes,
)

# ── CSV upload views ─────────────────────────────────────────────────────────
from .csv_upload import (  # noqa: F401
    premium_csv_upload_page,
    csv_upload_api,
    csv_upload_progress,
)
