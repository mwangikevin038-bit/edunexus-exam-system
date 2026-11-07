"""Print a compact table of every sub-nav block for preset design."""
import json
import pathlib
import re
from collections import defaultdict

rows = json.loads(pathlib.Path("shots/subnav.json").read_text(encoding="utf-8"))

# Group blocks that have identical item signature -> same preset.
def sig(r):
    return tuple(
        (re.sub(r"^.*?/", "/", i["href"]), i["label"], i["active"])
        for i in r["items"]
    )

groups = defaultdict(list)
for r in rows:
    groups[sig(r)].append(r)

for n, (s, members) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
    print(f"-- PRESET {n}  ({len(members)} blocks)")
    print(f"   files: {', '.join(f'{m['file']}{'' if m['n']==1 else '#' + str(m['n'])}' for m in members)}")
    for href, label, active in s:
        print(f"   {'*' if active else ' '} {label:<26} {href}")
    print()
