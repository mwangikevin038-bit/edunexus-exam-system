"""List Phosphor icon classes already used in templates."""
import pathlib
import re
from collections import Counter

counts = Counter()
for p in pathlib.Path(".").rglob("*.html"):
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    counts.update(re.findall(r"class=\"[^\"]*\b(ph-[a-z0-9-]+)\b", text))

print(f"{len(counts)} distinct")
for name, n in counts.most_common():
    print(f"  {n:4d}  {name}")
