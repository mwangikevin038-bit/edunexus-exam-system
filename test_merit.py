import os, time
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'school.settings')
import django; django.setup()

from django.test import Client
from django.contrib.auth.models import User
from django.core.cache import cache

admin = User.objects.filter(is_superuser=False, school_admin_profile__isnull=False).first()
c = Client(HTTP_HOST='localhost:8000')
c.force_login(admin)
school = admin.school_admin_profile.school

# Find an exam with published data
from students.models import ExamResultSnapshot
snap = ExamResultSnapshot.objects.filter(school=school).first()
if snap:
    exam_id = snap.exam_id
    grade = snap.class_name
    stream = snap.stream
    print(f"Using snap data: grade={grade} stream={stream} exam_id={exam_id}")
else:
    print("No snapshots found, trying with Exam directly")
    from students.models import Exam
    exam = Exam.all_objects.filter(school=school, is_deleted=False).first()
    if not exam:
        print("No exams found"); exit()
    exam_id = exam.id
    grade = exam.class_name if hasattr(exam, 'class_name') else 'Grade 7'
    stream = 'Yellow'
    print(f"Using exam: grade={grade} stream={stream} exam_id={exam_id}")

# Clear merit list cache
from django_redis import get_redis_connection
try:
    conn = get_redis_connection("default")
    pattern = f'*merit_list:{school.pk}:{grade}:*'
    keys = conn.keys(pattern)
    if keys:
        conn.delete(*keys)
        print(f"Cleared {len(keys)} merit list cache keys")
except Exception as e:
    print(f"Cache clear note: {e}")

# First call — cache miss, hits DB
print("\n=== FIRST CALL (cache miss) ===")
t0 = time.time()
r1 = c.get('/printouts/merit-list/', {'grade': grade, 'stream': stream, 'exam_id': str(exam_id)})
t1 = time.time()
print(f"Status: {r1.status_code}  Time: {(t1-t0)*1000:.0f}ms")
if r1.status_code != 200:
    print(f"Content: {r1.content[:300].decode('utf-8', errors='replace')}")

# Second call — should hit Redis cache
print("\n=== SECOND CALL (cache hit) ===")
t0 = time.time()
r2 = c.get('/printouts/merit-list/', {'grade': grade, 'stream': stream, 'exam_id': str(exam_id)})
t1 = time.time()
print(f"Status: {r2.status_code}  Time: {(t1-t0)*1000:.0f}ms")

# Verify cache key exists
cache_key = f'merit_list:{school.pk}:{grade}:{stream}:{exam_id}'
cached = cache.get(cache_key)
print(f"\nCache key exists: {cached is not None}")
if cached:
    print(f"Cached broadsheet rows: {len(cached.get('broadsheet', []))}")
    print(f"Cached subjects: {len(cached.get('published_subjects', []))}")

# Third call — also cache hit
print("\n=== THIRD CALL (cache hit) ===")
t0 = time.time()
r3 = c.get('/printouts/merit-list/', {'grade': grade, 'stream': stream, 'exam_id': str(exam_id)})
t1 = time.time()
print(f"Status: {r3.status_code}  Time: {(t1-t0)*1000:.0f}ms")

# Test HTMX partial request
print("\n=== HTMX PARTIAL REQUEST ===")
t0 = time.time()
r4 = c.get('/printouts/merit-list/', {'grade': grade, 'stream': stream, 'exam_id': str(exam_id)},
           HTTP_HX_REQUEST='true')
t1 = time.time()
print(f"Status: {r4.status_code}  Time: {(t1-t0)*1000:.0f}ms")
has_broadsheet = 'broadsheet-document' in r4.content.decode('utf-8', errors='replace')
print(f"Has broadsheet document: {has_broadsheet}")

# Test empty form (no params)
print("\n=== EMPTY FORM (no params) ===")
r5 = c.get('/printouts/merit-list/')
print(f"Status: {r5.status_code}")
has_form = 'ml-fetch-btn' in r5.content.decode('utf-8', errors='replace')
print(f"Has form button: {has_form}")

print("\n=== ALL TESTS PASSED ===" if all(r.status_code == 200 for r in [r1, r2, r3, r4, r5]) else "\n=== SOME TESTS FAILED ===")
