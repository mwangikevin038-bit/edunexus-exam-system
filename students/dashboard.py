"""
Unfold admin dashboard callback.

Provides KPI metrics and recent activity for the Django admin dashboard.
"""

from django.apps import apps
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model


User = get_user_model()


def get_dashboard_context(request, context, *args, **kwargs):
    School = apps.get_model('students', 'School')
    Student = apps.get_model('students', 'Student')
    Teacher = apps.get_model('students', 'Teacher')

    active_schools = School.objects.filter(status__in=['active', 'trial'])

    context.update({
        "kpi_metrics": [
            {
                "title": "Total Enrolled Students",
                "metric": Student.all_objects.filter(school__in=active_schools).count(),
                "footer": f"Aggregated across {active_schools.count()} school(s)",
                "icon": "school",
            },
            {
                "title": "Active Faculty Members",
                "metric": Teacher.all_objects.filter(school__in=active_schools, is_active=True).count(),
                "footer": "Assigned platform wide",
                "icon": "person_pin",
            },
            {
                "title": "System Users",
                "metric": User.objects.count(),
                "footer": "Global Superadmins & Staff",
                "icon": "manage_accounts",
            },
        ],
        "log_entries": LogEntry.objects.none(),
    })
    return context
