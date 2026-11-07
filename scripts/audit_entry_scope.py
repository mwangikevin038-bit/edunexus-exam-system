"""List every rule in entry-mobile.css that uses --en-* tokens, with its
selector, so we can tell which ones sit outside .en-shell (dev helper)."""
import pathlib
import re

text = pathlib.Path("static/css/entry-mobile.css").read_text(encoding="utf-8")
text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

rows = []
for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
    sel = " ".join(m.group(1).split())
    body = m.group(2)
    if "--en-" not in body:
        continue
    # which media query (if any) encloses this match
    before = text[: m.start()]
    media = ""
    for mm in re.finditer(r"@media[^{]*", before):
        if before[mm.end() :].count("{") > before[mm.end() :].count("}"):
            media = mm.group(0).strip()
    rows.append((sel, media, len(re.findall(r"var\(--en-", body))))

outside = [r for r in rows if not r[0].startswith(".en-") and ".en-shell" not in r[0]]
print(f"{len(rows)} token-using rules; {len(outside)} NOT under an .en-* selector:\n")
for sel, media, n in outside:
    print(f"  {sel[:100]}")
    if media:
        print(f"      {media}")
    print()

print("--- media-query :root blocks ---")
for m in re.finditer(r"@media[^{]*\{\s*:root\s*\{", text):
    print("  ", m.group(0).replace("\n", " "))
