"""Test bulk report PDF generation with authentication."""
import os, sys
os.chdir(r'C:\Exam System')
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'
import django; django.setup()

from django.test import RequestFactory
from django.contrib.sessions.middleware import SessionMiddleware
from django.contrib.messages.middleware import MessageMiddleware
from students.views.pdf_exports import download_bulk_report_pdf
from students.models import Student

# Get some real student IDs
students = Student.all_objects.all()[:3]
ids = [str(s.id) for s in students]
print(f"Testing with {len(ids)} students: {ids}")

factory = RequestFactory()
request = factory.get(f'/bulk-reports/download-pdf/?ids={",".join(ids)}&year=2026&term=Term+2&assessment=end&mode=inline')

# Add session + auth
middleware = SessionMiddleware(lambda r: None)
middleware.process_request(request)
request.session.save()

from django.contrib.auth.models import User, AnonymousUser
request.user = User.objects.filter(is_superuser=True).first() or User.objects.first()
if not request.user:
    print("ERROR: No users found!")
    sys.exit(1)

# Add messages middleware
msg_middleware = MessageMiddleware(lambda r: None)
msg_middleware.process_request(request)
request.session.save()

# Mock school
from students.security import get_request_school
try:
    school = get_request_school(request)
    print(f"School: {school}")
except Exception as e:
    print(f"Could not get school from request: {e}")
    # Try to set it manually
    from school.models import School
    school = School.objects.first()
    print(f"Using first school: {school}")

# Now test the actual view
try:
    response = download_bulk_report_pdf(request)
    print(f"Response status: {response.status_code}")
    print(f"Content-Type: {response.get('Content-Type', 'unknown')}")
    print(f"Content-Length: {len(response.content)} bytes")
    if response.status_code == 302:
        print(f"Redirect to: {response.get('Location', 'unknown')}")
        print("This means the view hit an error and redirected with a message")
    elif b'%PDF-' in response.content[:10]:
        print("SUCCESS: Got a valid PDF!")
    else:
        print("Content starts with:", response.content[:200])
except Exception as e:
    print(f"EXCEPTION: {e}")
    import traceback
    traceback.print_exc()
