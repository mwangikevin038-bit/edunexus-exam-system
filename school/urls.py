"""
URL configuration for the EduNexus Exam System.

Routes:
- /admin/          Django admin portal
- /super/          Superuser management portal
- /                Welcome page
- /login/          School login
- /logout/         Logout
- /forgot-password/ Password reset flow
- (app routes)     Student/Teacher management
"""

from django.contrib import admin
from django.urls import path, include
from students import views
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views

urlpatterns = [
    # ── Django admin ──────────────────────────────────────────────────────
    path('admin/', admin.site.urls),

    # ── Superuser app ─────────────────────────────────────────────────────
    path('super/', include('superuser.urls')),

    # ── Public entry points ───────────────────────────────────────────────
    path('', views.welcome_page, name='welcome_page'),   # edunexus.net/
    path('login/', views.login_view, name='login'),      # edunexus.net/login/
    path('logout/', views.logout_view, name='logout'),

    # ── Password reset flow ───────────────────────────────────────────────
    path('forgot-password/',
         auth_views.PasswordResetView.as_view(
             template_name='password_reset_form.html',
             email_template_name='email/password_reset_email.txt',
             html_email_template_name='email/password_reset_email.html',
             subject_template_name='email/password_reset_subject.txt',
             from_email='EDUNEXUS <noreply@edunexus.system>',
         ),
         name='password_reset'),
    path('forgot-password/done/',
         auth_views.PasswordResetDoneView.as_view(
             template_name='password_reset_done.html'),
         name='password_reset_done'),
    path('reset/<uidb64>/<token>/',
         auth_views.PasswordResetConfirmView.as_view(
             template_name='password_reset_confirm.html'),
         name='password_reset_confirm'),
    path('reset/done/',
         auth_views.PasswordResetCompleteView.as_view(
             template_name='password_reset_complete.html'),
         name='password_reset_complete'),

    # ── All app routes (no /students/ prefix anymore) ─────────────────────
    path('', include('students.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)