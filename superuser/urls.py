"""
superuser/urls.py
=================
All routes for the EduNexus platform superuser portal.
Mounted at the public schema domain e.g. superuser.localhost/super/
"""

from django.conf import settings
from django.urls import path
from . import views

urlpatterns = [
    # Auth — unique secret URL derived from SECRET_KEY to prevent brute-force
    path(f'{settings.SUPERUSER_SECRET_TOKEN}/signin/',   views.super_login,   name='super_login'),
    path(f'{settings.SUPERUSER_SECRET_TOKEN}/signout/',  views.super_logout,  name='super_logout'),

    # Dashboard
    path('dashboard/', views.super_dashboard, name='super_dashboard'),

    # School management
    path('schools/',                      views.school_list,          name='super_school_list'),
    path('schools/create/',               views.school_create,        name='super_school_create'),
    path('schools/<int:school_id>/edit/', views.school_edit,          name='super_school_edit'),
    path('schools/<int:school_id>/delete/', views.school_delete,      name='super_school_delete'),
    path('schools/<int:school_id>/toggle-status/', views.school_toggle_status, name='super_school_toggle'),

    # Broadcasts
    path('broadcasts/',                        views.broadcast_list,   name='super_broadcast_list'),
    path('broadcasts/create/',                 views.broadcast_create, name='super_broadcast_create'),
    path('broadcasts/<int:broadcast_id>/toggle/', views.broadcast_toggle, name='super_broadcast_toggle'),
    path('broadcasts/<int:broadcast_id>/delete/', views.broadcast_delete, name='super_broadcast_delete'),
]