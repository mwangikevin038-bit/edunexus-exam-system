"""Which --en-* tokens does entry-mobile.css define that variables.css doesn't,
and does anything outside entry-mobile.css reference them? (dev helper)"""
import pathlib
import re

root = pathlib.Path(".")
DECL = re.compile(r"(--[a-z0-9-]+)\s*:")


def first_roots(text):
    out = set()
    for m in re.finditer(r":root\s*\{", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out |= set(DECL.findall(text[m.end():i]))
    return out


entry = pathlib.Path("static/css/entry-mobile.css").read_text(encoding="utf-8")
variables = pathlib.Path("static/css/variables.css").read_text(encoding="utf-8")

entry_tokens = first_roots(entry)
canonical = first_roots(variables)
private = entry_tokens - canonical

print(f"entry-mobile defines {len(entry_tokens)} tokens; {len(private)} not in variables.css:")
for t in sorted(private):
    print(f"   {t}")
print(f"\nshared names (the actual conflict): {len(entry_tokens & canonical)}")
for t in sorted(entry_tokens & canonical):
    print(f"   {t}")

# Anything outside entry-mobile.css that references the private names?
hits = []
for p in list(root.rglob("*.css")) + list(root.rglob("*.html")) + list(root.rglob("*.js")):
    if p.name == "entry-mobile.css" or "node_modules" in p.parts or "shots" in p.parts:
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for t in private:
        if t in text:
            hits.append(f"{p} -> {t}")

print("\nreferences outside entry-mobile.css:", len(hits))
for h in hits:
    print("   ", h)
