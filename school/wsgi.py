"""
WSGI config for EduNexus Exam System.

Entry point for WSGI-compatible web servers (e.g., Gunicorn).
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')

application = get_wsgi_application()
