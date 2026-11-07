"""Parse every sub-nav block into structured preset data (dev helper)."""
import json
import pathlib
import re

root = pathlib.Path("students/templates")
pat = re.compile(r'<nav class="sub-nav-bar dm-subnav">.*?</nav>', re.S)

ITEM_RE = re.compile(
    r'<a href="(?P<href>[^"]*)"[^>]*class="sub-nav-link(?P<cls>[^"]*)"[^>]*>\s*'
    r'(?P<icon><i class="[^"]*"></i>|<svg.*?</svg>)\s*'
    r'(?P<label>[^<]+?)\s*</a>',
    re.S,
)
COND_RE = re.compile(r'\{%\s*(if|elif|else|endif)([^%]*)%\}')

rows = []
for p in sorted(root.rglob("*.html")):
    text = p.read_text(encoding="utf-8", errors="replace")
    for i, m in enumerate(pat.finditer(text), 1):
        block = m.group(0)
        # Track conditional nesting so we can see which items are gated.
        items = []
        for im in ITEM_RE.finditer(block):
            before = block[: im.start()]
            conds = COND_RE.findall(before)
            icon = im.group("icon")
            if icon.startswith("<i"):
                ic = re.search(r'class="(ph [^"]*)"', icon).group(1)
            else:
                ic = "SVG:" + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", icon)).strip()[:40]
            items.append({
                "href": im.group("href"),
                "label": im.group("label").strip(),
                "icon": ic,
                "active": " active" in im.group("cls"),
                "conds": [" ".join(c).strip() for c in conds[-1:]] if conds else [],
            })
        rows.append({
            "file": p.name,
            "line": text[: m.start()].count("\n") + 1,
            "n": i,
            "items": items,
        })

pathlib.Path("shots/subnav.json").write_text(
    json.dumps(rows, indent=1), encoding="utf-8"
)
print(f"{len(rows)} blocks, {sum(len(r['items']) for r in rows)} items")
