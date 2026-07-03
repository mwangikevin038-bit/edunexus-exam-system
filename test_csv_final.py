import json, time, os, sys, django
sys.path.insert(0, r'C:\Exam System')
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
User = get_user_model()
u = User.objects.get(username='admin@lungalunga')
c = Client(HTTP_HOST='localhost:8000')
c.force_login(u)

rows = []
for i in range(10):
    rows.append({
        'student_name': f'Student {i}',
        'class_name': 'Grade 5',
        'stream': 'Main',
        'parent_name': f'Parent {i}',
        'parent_phone': f'07555{i:04d}',
        'admission_no': str(9200 + i),
    })

resp = c.post('/api/csv-upload/', data=json.dumps({'rows': rows}), content_type='application/json', HTTP_HOST='localhost:8000')
data = json.loads(resp.content)
print(f"Upload: {data.get('status')} total={data.get('total')} id={data.get('upload_id','')[:12]}")

upload_id = data.get('upload_id', '')
if upload_id:
    for attempt in range(10):
        time.sleep(2)
        resp2 = c.get(f'/api/csv-upload/progress/?upload_id={upload_id}', HTTP_HOST='localhost:8000')
        d = json.loads(resp2.content)
        status = d.get('status', '?')
        created = d.get('created', 0)
        processed = d.get('processed', 0)
        total = d.get('total', 0)
        errors = d.get('errors', [])
        msg = d.get('message', '')[:80]
        print(f"  [{attempt}] {status} processed={processed}/{total} created={created} errors={len(errors)} msg={msg}")
        if status in ('completed', 'completed_with_errors', 'error'):
            break
    print(f"\nFinal: {json.dumps(d, indent=2)}")
