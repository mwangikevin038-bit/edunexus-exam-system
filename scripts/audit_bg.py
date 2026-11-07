"""Print every per-page background override with surrounding context, so each
can be edited precisely (dev helper)."""
import pathlib
import re

ROOT = pathlib.Path("students/templates")
PAT = re.compile(
    r"(?:^|\n)([ \t]*)((?:\.main-content|\.app-content|body)\s*\{[^}]*background[^}]*\})"
)

for p in sorted(ROOT.rglob("*.html")):
    text = p.read_text(encoding="utf-8", errors="replace")
    hits = list(PAT.finditer(text))
    if not hits:
        continue
    print(f"\n=== {p.name} ===")
    for m in hits:
        line = text[: m.start()].count("\n") + 2
        rule = " ".join(m.group(2).split())
        # which media / block encloses it?
        before = text[: m.start()]
        media = ""
        for mm in re.finditer(r"@media[^{]*", before):
            if before[mm.end() :].count("{") > before[mm.end() :].count("}"):
                media = mm.group(0).strip()
        tag = f"  [{media}]" if media else ""
        print(f"  L{line:<5} {rule[:150]}{tag}")
