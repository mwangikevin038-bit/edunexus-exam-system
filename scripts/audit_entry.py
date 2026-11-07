"""Classify every rule in entry-mobile.css as scoped or global (dev helper).

A rule is "global" when its selector does not sit under .en-shell / #examContent,
i.e. it can affect the rest of the page (chrome, sidebar, sub-nav).
"""
import pathlib
import re

path = pathlib.Path("static/css/entry-mobile.css")
text = path.read_text(encoding="utf-8")

# strip comments
text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)

SCOPED = (".en-shell", "#examContent", ".cp-desktop")

rules = []
# walk top-level blocks (skip @media nesting by descending into it)
i = 0
depth = 0
buf = ""
stack = []  # accumulated prelude per depth


def emit(prelude, body, media):
    prelude = prelude.strip()
    if not prelude or prelude.startswith("@"):
        return
    for sel in prelude.split(","):
        sel = sel.strip()
        if not sel:
            continue
        scoped = any(s in sel for s in SCOPED) or sel.startswith(".en-")
        uses_var = "--en-" in body
        hardcoded = bool(re.search(r"#24E55E|#199E41|#0F8A34|#24e55e|#199e41|rgba\(36,\s*229,\s*94", body, re.I))
        if uses_var or hardcoded:
            rules.append((sel, scoped, uses_var, hardcoded, media))


media = None
while i < len(text):
    ch = text[i]
    if ch == "{":
        # find prelude start (after last ; or { )
        j = i
        while j > 0 and text[j - 1] not in ";{}":
            j -= 1
        prelude = text[j:i]
        # find matching close
        d = 1
        k = i + 1
        while k < len(text) and d:
            if text[k] == "{":
                d += 1
            elif text[k] == "}":
                d -= 1
            k += 1
        body = text[i + 1:k - 1]
        if prelude.strip().startswith("@media"):
            # recurse into media body
            sub = body
            # simple: parse rules inside
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", sub):
                emit(m.group(1), m.group(2), prelude.strip()[:60])
        else:
            emit(prelude, body, None)
        i = k
        continue
    i += 1

global_rules = [r for r in rules if not r[1]]
scoped_rules = [r for r in rules if r[1]]

print(f"total token-using rules: {len(rules)}  (scoped {len(scoped_rules)}, GLOBAL {len(global_rules)})")
print("\n-- GLOBAL rules (leak past .en-shell) --")
for sel, _, uses_var, hardcoded, media in global_rules:
    tags = []
    if uses_var:
        tags.append("var(--en-*)")
    if hardcoded:
        tags.append("hardcoded-emerald")
    print(f"  {sel[:90]:<92} {' '.join(tags)}{'' if not media else '  [' + media + ']'}")
