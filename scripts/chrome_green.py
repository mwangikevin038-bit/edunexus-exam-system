"""Which chrome selectors were painted green at HEAD? (dev helper)"""
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HEAD = "0a22130"
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
WANTED = {"#4caf50", "#43a047", "#2e6b31", "#3d8b40", "#22c55e", "#16a34a",
          "#15803d", "#9be816", "#8ad412", "#7ac010"}


def head(path):
    return subprocess.run(["git", "show", f"{HEAD}:{path}"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


for path in ("static/css/sub-nav.css", "static/css/premium-sidebar.css",
             "static/css/layout.css", "static/css/variables.css",
             "students/templates/students/base.html"):
    text = head(path)
    print(f"\n=== {path} ===")
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        body = m.group(2)
        cols = [c for c in HEX.findall(body) if c.lower() in WANTED]
        if not cols:
            continue
        sel = " ".join(m.group(1).split())
        if sel.startswith("@") or sel.strip() in ("from", "to"):
            # keyframes / media: show anyway but mark
            kind = "block"
        else:
            kind = "rule"
        print(f"  [{kind}] {sel[:70]}")
        print(f"          {', '.join(cols)}")
