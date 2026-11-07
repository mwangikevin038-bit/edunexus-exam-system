"""Was the lime token (--en-green) ever wired up at HEAD? (dev helper)

Scans the committed tree for var(--en-green) usage, and dumps every
green-ish colour in base.html including rgba/named forms.
"""
import re
import subprocess
from collections import Counter

HEAD = "0a22130"
GREENISH_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def tracked():
    out = run(["git", "ls-tree", "-r", "--name-only", HEAD])
    return [p for p in out.splitlines()
            if p.endswith((".css", ".html", ".js"))]


def head_file(p):
    return run(["git", "show", f"{HEAD}:{p}"])


def is_green(r, g, b):
    return g > r + 25 and g > b + 25


# 1. where was var(--en-green) referenced at HEAD?
print("=== var(--en-green) references at HEAD ===")
hits = Counter()
for p in tracked():
    t = head_file(p)
    n = len(re.findall(r"var\(--en-green", t))
    if n:
        hits[p] = n
if hits:
    for p, n in hits.most_common():
        print(f"  {n:4d}  {p}")
else:
    print("  NONE — the lime token was never referenced by any file")

# 2. green-ish rgba() in base.html at HEAD
print("\n=== base.html @ HEAD: green rgba() ===")
t = head_file("students/templates/students/base.html")
c = Counter()
for m in RGBA.finditer(t):
    r, g, b = map(int, m.groups())
    if is_green(r, g, b):
        c[f"rgb({r},{g},{b})"] += 1
print(f"  {dict(c) if c else '(none)'}")

# 3. premium-sidebar: what was #4CAF50 painted on?
print("\n=== premium-sidebar.css @ HEAD: #4CAF50 context ===")
t = head_file("static/css/premium-sidebar.css")
for m in re.finditer(r"([^{}]+)\{([^{}]*#4CAF50[^{}]*)\}", t, re.I):
    sel = " ".join(m.group(1).split())
    print(f"  {sel[:80]}")
    for d in m.group(2).split(";"):
        if "4CAF50" in d.upper() or "green" in d.lower():
            print(f"        {d.strip()}")
