"""Show the :root blocks each mobile stylesheet re-declares, and how they
differ from the canonical tokens in variables.css (dev helper)."""
import pathlib
import re

ROOT = pathlib.Path("static/css")
files = [
    "capture-mobile.css",
    "dashboard-mobile.css",
    "entry-mobile.css",
    "rc-mobile.css",
    "results-mobile.css",
]

DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]+);")


def first_root(text):
    m = re.search(r":root\s*\{", text)
    if not m:
        return {}
    depth, i = 1, m.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return dict(DECL.findall(text[m.end():i]))


canonical = first_root((ROOT / "variables.css").read_text(encoding="utf-8"))
print(f"variables.css: {len(canonical)} tokens\n")

for name in files:
    got = first_root((ROOT / name).read_text(encoding="utf-8"))
    diff = {k: v.strip() for k, v in got.items() if canonical.get(k, "").strip() != v.strip()}
    unknown = [k for k in got if k not in canonical]
    print(f"{name}: {len(got)} tokens — {len(got) - len(diff)} identical, "
          f"{len(diff)} differing, {len(unknown)} not in variables.css")
    for k in sorted(diff):
        print(f"    DIFF {k}: {got[k].strip()}\n         variables: {canonical.get(k)}")
    for k in sorted(unknown):
        print(f"    ONLY-HERE {k}: {got[k].strip()}")
