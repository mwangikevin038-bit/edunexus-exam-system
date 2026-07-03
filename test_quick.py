import urllib.request, urllib.parse, http.cookiejar, re, json

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

# Login
resp = opener.open('http://localhost:8000/login/')
html = resp.read().decode()
csrf = re.search(r'csrfmiddlewaretoken.*?value="(.*?)"', html).group(1)

data = urllib.parse.urlencode({'csrfmiddlewaretoken': csrf, 'username': 'admin@lungalunga', 'password': 'Admin123!'}).encode()
req = urllib.request.Request('http://localhost:8000/login/', data=data)
req.add_header('Referer', 'http://localhost:8000/login/')
try:
    opener.open(req)
except:
    pass

# Get CSRF from cookie
csrftoken = ''
for c in cj:
    if c.name == 'csrftoken':
        csrftoken = c.value

# Test API
rows = [{'student_name': f'FastTest{i}', 'class_name': 'Grade 5', 'stream': 'Blue', 'parent_name': f'FT{i}', 'parent_phone': f'07620{i:04d}'} for i in range(5)]
body = json.dumps({'rows': rows}).encode()
req = urllib.request.Request('http://localhost:8000/api/csv-upload/', data=body, method='POST')
req.add_header('Content-Type', 'application/json')
req.add_header('X-CSRFToken', csrftoken)
req.add_header('Referer', 'http://localhost:8000/school-admin/csv-onboard/')
try:
    resp = opener.open(req)
    result = json.loads(resp.read().decode())
    print(f"Upload: {result}")
    uid = result.get('upload_id', '')
    
    import time
    time.sleep(3)
    
    # Check progress
    req2 = urllib.request.Request(f'http://localhost:8000/api/csv-upload/progress/?upload_id={uid}')
    resp2 = opener.open(req2)
    progress = json.loads(resp2.read().decode())
    print(f"Progress: {json.dumps(progress, indent=2)}")
except urllib.error.HTTPError as e:
    print(f"Error {e.code}: {e.read().decode()[:500]}")
