"""Census every green used across the committed tree at HEAD (dev helper).

Answers: which green was actually the app's system colour, and how dominant.
"""
import re
import subprocess
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HEAD = "0a22130"
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def run(args):
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def is_green(r, g, b):
    return g > r + 25 and g > b + 25


files = [p for p in run(["git", "ls-tree", "-r", "--name-only", HEAD]).splitlines()
         if p.endswith((".css", ".html", ".js"))]

hexes, rgbs = Counter(), Counter()
per_file = {}
for p in files:
    t = run(["git", "show", f"{HEAD}:{p}"])
    if not t:
        continue
    fh = Counter()
    for h in HEX.findall(t):
        h = h.lower()
        r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
        if is_green(r, g, b):
            hexes[h] += 1
            fh[h] += 1
    for m in RGBA.finditer(t):
        r, g, b = map(int, m.groups())
        if is_green(r, g, b):
            key = f"rgb({r},{g},{b})"
            rgbs[key] += 1
            fh[key] += 1
    if fh:
        per_file[p] = fh

print("=== green hex usage across whole tree @ HEAD ===")
for h, n in hexes.most_common():
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    print(f"  {n:4d}  {h}   rgb({r},{g},{b})")

print("\n=== green rgba/rgb usage @ HEAD ===")
for k, n in rgbs.most_common(8):
    print(f"  {n:4d}  {k}")

print("\n=== files using green @ HEAD ===")
for p, fh in sorted(per_file.items(), key=lambda kv: -sum(kv[1].values())):
    top = ", ".join(f"{k}x{v}" for k, v in fh.most_common(3))
    print(f"  {sum(fh.values()):4d}  {p}   [{top}]")

# what did the sub-nav / sidebar paint at HEAD?
print("\n=== chrome selectors painted green @ HEAD ===")
for p in ("static/css/sub-nav.css", "static/css/premium-sidebar.css",
          "static/css/layout.css"):
    t = run(["git", "show", f"{HEAD}:{p}"])
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", t):
        body = m.group(2)
        if not any(is_green(*map(int, (h[i:i + 2], h[i + 2:i + 4], h[i + 4:i + 6])))
                   for h in (x.lower() for x in HEX.findall(body))
                   for i in (1, 3, 5)):
            if not re.search(r"#(?:4caf50|22c55e|43a047|2e6b31|3d8b40)", body, re.I):
                continue
        sel = " ".join(m.group(1).split())
        if "@" in sel or "from" == sel.strip() or "to" == sel.strip():
            continue
        cols = [c for c in HEX.findall(body)
                if re.search(r"#(?:4caf50|22c55e|43a047|2e6b31|3d8b40)", c, re.I)]
        if cols:
            print(f"  {p.split('/')[-1]:<22} {sel[:62]:<64} {cols}")
