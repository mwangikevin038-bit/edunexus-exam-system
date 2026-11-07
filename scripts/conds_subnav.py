"""Show conditional context for items, and dump any block with no items."""
import json
import pathlib

rows = json.loads(pathlib.Path("shots/subnav.json").read_text(encoding="utf-8"))

for r in rows:
    if not r["items"]:
        print(f"EMPTY: {r['file']}#{r['n']} line {r['line']}")
        continue
    conds = sorted({c for i in r["items"] for c in i["conds"]})
    if conds:
        print(f"{r['file']}#{r['n']}  conds={conds}")
        for i in r["items"]:
            print(f"    {'*' if i['active'] else ' '} {i['label']:<22} {i['conds']}")
