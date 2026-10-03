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
from students.views.password_reset import (
    RateLimitedPasswordResetView,
    SecurePasswordResetConfirmView,
    SecurePasswordResetDoneView,
    SecurePasswordResetCompleteView,
)
from django.conf import settings
from django.conf.urls.static import static
import os

urlpatterns = [
    # ── Django admin ──────────────────────────────────────────────────────
    path('admin/', admin.site.urls),

    # ── Superuser app ─────────────────────────────────────────────────────
    path('super/', include('superuser.urls')),

    # ── Public entry points ───────────────────────────────────────────────
    path('', views.welcome_page, name='welcome_page'),   # edunexus.net/
    path('login/', views.login_view, name='login'),      # edunexus.net/login/
    path('logout/', views.logout_view, name='logout'),

    # ── Password reset flow (with enhanced security) ─────────────────────
    path('forgot-password/',
         RateLimitedPasswordResetView.as_view(),
         name='password_reset'),
    path('forgot-password/done/',
         SecurePasswordResetDoneView.as_view(),
         name='password_reset_done'),
    path('reset/<uidb64>/<token>/',
         SecurePasswordResetConfirmView.as_view(),
         name='password_reset_confirm'),
    path('reset/done/',
         SecurePasswordResetCompleteView.as_view(),
         name='password_reset_complete'),

    # ── All app routes (no /students/ prefix anymore) ─────────────────────
    path('', include('students.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATICFILES_DIRS[0])
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
elif os.environ.get('EDUNEXUS_SERVE_MEDIA', 'False').strip().lower() in ('1', 'true', 'yes', 'on'):
    # Production single-server mode: serve uploads straight from Django.
    # (static() no-ops when DEBUG is False, so wire the view manually.
    #  Swap for S3/Cloudinary or an nginx `location /media/` at scale.)
    from django.urls import re_path
    from django.views.static import serve as _serve_media
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', _serve_media, {'document_root': settings.MEDIA_ROOT}),
    ]

handler404 = 'students.views.custom_404'
handler500 = 'students.views.custom_500'