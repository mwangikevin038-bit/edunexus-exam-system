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
    path('school-admin/school-settings/',
         views.school_settings,
         name='school_settings'),
    path('school-admin/term-dates/',
         views.term_dates,
         name='term_dates'),

    # ── Student management ────────────────────────────────────────────────
    path('add-student/', views.add_student, name='add_student'),
    path('class-lists/', views.class_lists, name='class_lists'),
    path('class-lists/search/', views.teacher_search_student, name='teacher_search_student'),
    path('class-lists/search/fields/', views.teacher_search_fields, name='teacher_search_fields'),
    path('class-lists/search/submit/', views.teacher_search_submit, name='teacher_search_submit'),
    path('class-lists/search/reset/', views.teacher_search_reset, name='teacher_search_reset'),
    path('class-lists/search/profile/<int:student_id>/', views.teacher_student_profile_card, name='teacher_student_profile_card'),
    path('class-lists/search/analytics/<int:student_id>/', views.teacher_student_analytics, name='teacher_student_analytics'),
    path('teacher-report-forms/', views.teacher_report_forms, name='teacher_report_forms'),
    path('teacher-report-forms/display/', views.teacher_report_forms_display, name='teacher_report_forms_display'),
    path('teacher-report-forms/comments/', views.teacher_load_comments, name='teacher_load_comments'),
    path('teacher-class-comments/', views.teacher_class_comments, name='teacher_class_comments'),
    path('class-lists/download-pdf/', views.download_classlist_pdf, name='download_classlist_pdf'),
    path('printouts/', views.printouts_hub, name='printouts_hub'),
    path('printouts/class-list/', views.class_list_printout, name='class_list_printout'),
    path('printouts/score-sheet/', views.score_sheet, name='score_sheet'),
    path('printouts/analysis-report/', views.analysis_report, name='analysis_report'),
    path('printouts/report-forms/', views.report_forms, name='report_forms'),
    path('printouts/report-forms/display/', views.report_forms_display, name='report_forms_display'),
    path('printouts/merit-list/', views.merit_list, name='merit_list'),
    path('printouts/api/exams/', views.api_exams_for_class, name='api_exams_for_class'),
    path('printouts/api/streams/', views.api_streams_for_grade_printout, name='api_streams_for_grade_printout'),
    path('printouts/api/subjects/', views.api_subjects_for_grade, name='api_subjects_for_grade'),
    path('printouts/api/teacher/', views.api_teacher_for_subject, name='api_teacher_for_subject'),
    path('printouts/api/analysis-data/', views.api_analysis_data, name='api_analysis_data'),
    path('printouts/api/analysis-report-pdf/', views.analysis_report_pdf, name='analysis_report_pdf'),
    path('learner/<int:student_id>/', views.learner_profile, name='learner_profile'),

    # ── Marks & exams ─────────────────────────────────────────────────────
    path('select-exam/', views.select_exam, name='select_exam'),
    path('select-exam-primary/', views.select_exam_primary, name='select_exam_primary'),
    path('api/clear-mark/', views.clear_mark, name='clear_mark'),
    path('api/save-mark/', views.save_mark, name='save_mark'),
    path('api/batch-save-marks/', views.batch_save_marks, name='batch_save_marks'),
    path('api/update-maximum-marks/', views.update_maximum_marks, name='update_maximum_marks'),
    path('api/return-sheet/', views.return_mark_sheet, name='return_mark_sheet'),

    # ── Results & reports ─────────────────────────────────────────────────
    path('results/', views.results_list, name='results_list'),
    path('results/download-pdf/', views.download_broadsheet_pdf, name='download_broadsheet_pdf'),
    path('report-cards/', views.report_card_select, name='report_card_select'),
    path('report/<int:student_id>/', views.individual_report, name='individual_report'),
    path('report/<int:student_id>/download-pdf/', views.download_individual_report_pdf, name='download_individual_report_pdf'),
    path('bulk-reports/', views.bulk_report_cards, name='bulk_report_cards'),
    path('bulk-reports/download-pdf/', views.download_bulk_report_pdf, name='download_bulk_report_pdf'),
    path('bulk-reports/poll-status/', views.report_card_poll_status, name='report_card_poll_status'),

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
    path('school-admin/faculty/grade-streams/', views.faculty_grade_streams, name='faculty_grade_streams'),
    path('printouts/teachers-list/', views.teachers_list_printout, name='teachers_list_printout'),
    path('school-admin/faculty/teachers-list-pdf/', views.download_teachers_list_pdf, name='download_teachers_list_pdf'),
    path('school-admin/faculty/<int:teacher_id>/classes/', views.teacher_classes, name='teacher_classes'),
    path('school-admin/locks/', views.manage_assessment_locks, name='manage_assessment_locks'),
    path('school-admin/registration/', views.admin_add_student, name='admin_add_student'),
    path('school-admin/registration/search-fields/', views.admin_search_fields, name='admin_search_fields'),
    path('school-admin/registration/search-submit/', views.admin_student_search_submit, name='admin_student_search_submit'),
    path('school-admin/registration/search-reset/', views.admin_search_form_reset, name='admin_search_form_reset'),
    path('school-admin/registration/profile/<int:student_id>/', views.admin_student_profile_card, name='admin_student_profile_card'),
    path('school-admin/registration/profile/<int:student_id>/edit/', views.admin_student_profile_edit, name='admin_student_profile_edit'),
    path('school-admin/registration/profile/<int:student_id>/save/', views.admin_student_profile_save, name='admin_student_profile_save'),
    path('school-admin/registration/profile/<int:student_id>/delete/', views.admin_student_delete, name='admin_student_delete'),
    path('school-admin/registration/analytics/<int:student_id>/', views.admin_student_analytics, name='admin_student_analytics'),
    path('school-admin/exams/', views.manage_exams, name='manage_exams'),
    path('school-admin/exams/edit/', views.edit_exam, name='edit_exam'),
    path('school-admin/exams/analyse/', views.analyse_exam, name='analyse_exam'),
    path('school-admin/exams/review/', views.review_stream_submission, name='review_stream_submission'),
    path('school-admin/exams/review-submission/', views.review_submission, name='review_submission'),
    path('school-admin/classes/', views.manage_classes, name='manage_classes'),
    path('school-admin/classes/<int:grade_id>/streams/', views.manage_streams, name='manage_streams'),
    path('school-admin/classes/<int:grade_id>/streams/<str:stream_name>/subjects/', views.manage_subjects, name='manage_subjects'),
    path('school-admin/api/class-list/', views.api_class_list, name='api_class_list'),
    path('school-admin/class-list/', views.class_list_page, name='class_list_page'),
    path('school-admin/add-new-class/', views.add_new_class, name='add_new_class'),
    path('school-admin/api/grade-subjects/', views.api_grade_subjects, name='api_grade_subjects'),
    path('school-admin/api/check-grade-streams/', views.api_check_grade_streams, name='api_check_grade_streams'),

    # ── Premium CSV Onboarding Engine ────────────────────────────────────
    path('school-admin/csv-onboard/', views.premium_csv_upload_page, name='premium_csv_upload'),
    path('school-admin/csv-onboard/fragment/', views.premium_csv_upload_fragment, name='premium_csv_upload_fragment'),
    path('api/csv-upload/', views.csv_upload_api, name='csv_upload_api'),
    path('api/csv-upload/progress/', views.csv_upload_progress, name='csv_upload_progress'),
    path('api/section-info/', views.get_section_info, name='get_section_info'),
]