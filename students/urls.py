from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    # ── Health check (no auth) ───────────────────────────────────────────
    path('healthz', views.healthz, name='healthz'),

    # ── Dashboard & profile ───────────────────────────────────────────────
    path('dashboard/', views.dashboard, name='dashboard_alt'),
    path('profile/', views.profile_view, name='home_alt'),
    path('password-change/',
         views.custom_password_change,
         name='password_change'),

    # ── Student management ────────────────────────────────────────────────
    path('add-student/', views.add_student, name='add_student'),
    path('class-lists/', views.class_lists, name='class_lists'),
    path('class-lists/download-pdf/', views.download_classlist_pdf, name='download_classlist_pdf'),
    path('learner/<int:student_id>/', views.learner_profile, name='learner_profile'),

    # ── Marks & exams ─────────────────────────────────────────────────────
    path('select-exam/', views.select_exam, name='select_exam'),
    path('select-exam-primary/', views.select_exam_primary, name='select_exam_primary'),
    path('api/clear-mark/', views.clear_mark, name='clear_mark'),
    path('api/save-mark/', views.save_mark, name='save_mark'),
    path('api/return-sheet/', views.return_mark_sheet, name='return_mark_sheet'),

    # ── Results & reports ─────────────────────────────────────────────────
    path('results/', views.results_list, name='results_list'),
    path('results/download-pdf/', views.download_broadsheet_pdf, name='download_broadsheet_pdf'),
    path('report-cards/', views.report_card_select, name='report_card_select'),
    path('report/<int:student_id>/', views.individual_report, name='individual_report'),
    path('bulk-reports/', views.bulk_report_cards, name='bulk_report_cards'),

    # ── Comments ──────────────────────────────────────────────────────────
    path('manage-master-comments/', views.manage_master_comments, name='manage_master_comments'),
    path('manage-headteacher-comments/', views.manage_headteacher_comments, name='manage_headteacher_comments'),

    # ── Workspace switching (admin only) ──────────────────────────
    path('switch-workspace/', views.switch_workspace, name='switch_workspace'),

    # ── API endpoints ─────────────────────────────────────────────
    path('api/streams-for-grade/', views.api_streams_for_grade, name='api_streams_for_grade'),

    # ── School admin ──────────────────────────────────────────────────────
    path('school-admin/', views.school_admin_dashboard, name='school_admin_dashboard'),
    path('school-admin/grading-config/', views.grading_configuration, name='grading_configuration'),
    path('school-admin/faculty/', views.manage_faculty_matrix, name='manage_faculty_matrix'),
    path('school-admin/locks/', views.manage_assessment_locks, name='manage_assessment_locks'),
    path('school-admin/registration/', views.admin_add_student, name='admin_add_student'),
    path('school-admin/exams/', views.manage_exams, name='manage_exams'),
    path('school-admin/exams/review/', views.review_stream_submission, name='review_stream_submission'),
    path('school-admin/classes/', views.manage_classes, name='manage_classes'),

    # ── Premium CSV Onboarding Engine ────────────────────────────────────
    path('school-admin/csv-onboard/', views.premium_csv_upload_page, name='premium_csv_upload'),
    path('api/csv-upload/', views.csv_upload_api, name='csv_upload_api'),
    path('api/csv-upload/progress/', views.csv_upload_progress, name='csv_upload_progress'),
]