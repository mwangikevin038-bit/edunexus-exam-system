"""Audit the duplicated sub-nav blocks across templates (dev helper)."""
import pathlib
import re

root = pathlib.Path("students/templates")
pat = re.compile(r'<nav class="sub-nav-bar dm-subnav">.*?</nav>', re.S)

for p in sorted(root.rglob("*.html")):
    text = p.read_text(encoding="utf-8", errors="replace")
    for m in pat.finditer(text):
        block = m.group(0)
        hrefs = re.findall(r'href="([^"]*)"', block)
        labels = re.findall(r'</i>\s*([A-Za-z][^<\n]*)', block)
        print(f"{p.name}  ({len(block)} chars, {len(hrefs)} items)")
        print(f"    hrefs  : {hrefs}")
        print(f"    labels : {[l.strip() for l in labels]}")
