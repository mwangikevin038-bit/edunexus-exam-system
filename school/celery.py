"""
Celery application configuration for EduNexus.

Discovers tasks from all installed apps and configures the broker
connection from Django settings.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "school.settings")

app = Celery("school")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
