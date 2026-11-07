"""Dump every sub-nav block to a reference file (dev helper)."""
import pathlib
import re

root = pathlib.Path("students/templates")
pat = re.compile(r'<nav class="sub-nav-bar dm-subnav">.*?</nav>', re.S)
out = []

for p in sorted(root.rglob("*.html")):
    text = p.read_text(encoding="utf-8", errors="replace")
    for i, m in enumerate(pat.finditer(text), 1):
        line = text[: m.start()].count("\n") + 1
        out.append(f"{'=' * 70}\n{p}  line {line}  block {i}\n{'=' * 70}\n{m.group(0)}\n")

pathlib.Path("shots/subnav_blocks.txt").write_text("\n".join(out), encoding="utf-8")
print(f"wrote {len(out)} blocks")
