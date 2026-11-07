"""Dump raw blocks for specific templates."""
import pathlib
import re
import sys

names = set(sys.argv[1:])
pat = re.compile(r'<nav class="sub-nav-bar dm-subnav">.*?</nav>', re.S)
for p in sorted(pathlib.Path("students/templates").rglob("*.html")):
    if p.name not in names:
        continue
    text = p.read_text(encoding="utf-8", errors="replace")
    for i, m in enumerate(pat.finditer(text), 1):
        print("=" * 25, p.name, "block", i, "=" * 25)
        print(m.group(0))
