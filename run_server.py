"""
Waitress production server for EduNexus Exam System (Windows).
Replaces 'python manage.py runserver' with a multi-threaded WSGI server.

Usage:
    python run_server.py

For 20+ concurrent users, this server handles requests in multiple threads
without blocking the main process.
"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Activate Django
import django
django.setup()

from django.core.wsgi import get_wsgi_application
from waitress import serve

application = get_wsgi_application()

if __name__ == '__main__':
    host = os.environ.get('WAITRESS_HOST', '0.0.0.0')
    port = int(os.environ.get('WAITRESS_PORT', '8000'))
    threads = int(os.environ.get('WAITRESS_THREADS', str(min(8, (os.cpu_count() or 4) * 2))))

    print(f"=" * 60)
    print(f"  EDUNEXUS Production Server (Waitress)")
    print(f"  Listening on: http://{host}:{port}")
    print(f"  Threads: {threads}")
    print(f"  Workers: 4 processes x {threads} threads each")
    print(f"=" * 60)
    print(f"  Press CTRL-BREAK to stop")
    print(f"=" * 60)

    serve(
        application,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=120,
        cleanup_interval=30,
        max_request_body_size=10 * 1024 * 1024,  # 10MB for CSV uploads
        recv_bytes=65536,
    )
