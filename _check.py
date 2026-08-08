import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
django.setup()

from django.test import RequestFactory
from students.views.students_mgmt import get_section_info

factory = RequestFactory()

# Test PRIMARY
req = factory.get('/api/section-info/', {'section': 'PRIMARY'})
resp = get_section_info(req)
print(f"PRIMARY: {resp.content.decode()}")

# Test JSS
req = factory.get('/api/section-info/', {'section': 'JSS'})
resp = get_section_info(req)
print(f"JSS:     {resp.content.decode()}")
