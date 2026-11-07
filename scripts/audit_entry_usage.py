"""For entry-mobile.css: which --en-* tokens are used vs declared here, and
does any other file depend on this sheet's emerald values? (dev helper)"""
import pathlib
import re

sheet = pathlib.Path("static/css/entry-mobile.css").read_text(encoding="utf-8")
used = set(re.findall(r"var\((--[a-z0-9-]+)", sheet))
declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", sheet))

print(f"used {len(used)}, declared {len(declared)}")
missing = used - declared
print(f"\nUSED BUT NOT DECLARED HERE (resolve to variables.css today): {len(missing)}")
for t in sorted(missing):
    print("   ", t)
extra = declared - used
print(f"\nDECLARED BUT NEVER USED: {len(extra)}")
for t in sorted(extra):
    print("   ", t)

# hardcoded emerald outside the :root block?
root_end = sheet.index("}", sheet.index(":root"))
tail = sheet[root_end:]
hexes = re.findall(r"#[0-9A-Fa-f]{6}|rgba\(\s*36,\s*229,\s*94[^)]*\)", tail)
print(f"\nhardcoded emerald-ish values after :root: {len(hexes)} -> {set(hexes)}")

# does teacher_classes.html load entry-mobile.css?
tc = pathlib.Path("students/templates/students/teacher_classes.html")
if tc.exists():
    t = tc.read_text(encoding="utf-8", errors="replace")
    print("\nteacher_classes.html loads entry-mobile:", "entry-mobile" in t)
    for m in re.finditer(r".{60}--en-green-dark.{40}", t, re.S):
        print("   ctx:", " ".join(m.group(0).split()))
