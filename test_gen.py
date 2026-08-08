import os, sys
os.chdir(r'C:\Exam System')
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'
import django; django.setup()

from students.views.pdf_exports import _generate_pdf, _playwright_ok, _playwright_checked, _verify_playwright
print('playwright_ok:', _playwright_ok)
print('playwright_checked:', _playwright_checked)

_verify_playwright()
print('After verify:', _playwright_ok)

html = '<html><head></head><body><h1>Test PDF</h1><p>Page 1</p></body></html>'
try:
    pdf = _generate_pdf(html, viewport={'width': 794, 'height': 1123}, margin={'top':'0.12in','right':'0.35in','bottom':'0.4in','left':'0.35in'}, wait_for_charts=False, timeout=30)
    print('PDF generated: %d bytes' % len(pdf))
    print('Valid PDF:', pdf[:5] == b'%PDF-')
except Exception as e:
    print('FAILED:', e)
    import traceback
    traceback.print_exc()
