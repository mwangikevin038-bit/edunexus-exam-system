"""What green did the chrome actually use at HEAD? (dev helper)

Compares the committed version of each chrome file against the working tree
and lists every green hex in each, so we can see which green the user saw.
"""
import re
import subprocess
from collections import Counter

HEAD = "0a22130"
FILES = [
    "students/templates/students/base.html",
    "static/css/sub-nav.css",
    "static/css/premium-sidebar.css",
    "static/css/layout.css",
    "static/css/variables.css",
    "static/css/base.css",
    "static/css/components.css",
]

GREEN = re.compile(r"#[0-9a-fA-F]{6}\b")


def greenish(hexes):
    out = Counter()
    for h in hexes:
        h = h.lower()
        r, g, b = (int(h[i : i + 2], 16) for i in (1, 3, 5))
        # a "green" is one where green clearly dominates both other channels
        if g > r + 25 and g > b + 25:
            out[h] += 1
    return out


def at_head(path):
    try:
        return subprocess.run(
            ["git", "show", f"{HEAD}:{path}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
    except OSError:
        return ""


def work(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


for f in FILES:
    old, new = at_head(f), work(f)
    print(f"\n=== {f} ===")
    o, n = greenish(GREEN.findall(old)), greenish(GREEN.findall(new))
    print(f"  HEAD : {dict(o) if o else '(none)'}")
    print(f"  now  : {dict(n) if n else '(none)'}")
    if old and not new:
        print("  !! file deleted/empty now")
