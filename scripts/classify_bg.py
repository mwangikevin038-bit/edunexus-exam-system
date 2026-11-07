"""Classify every body/.main-content/.app-content background rule by context.

Prints, for each rule: line, selector, media context, and whether it is a
candidate for removal (desktop, flat hex, not print, not transparent/gradient).

Comments are blanked to spaces (newlines kept) so character offsets are
identical between classification and editing -- the raw file is never
re-serialized, only surgical replacements are applied.

Usage: python scripts/classify_bg.py [--apply]
"""
import pathlib
import re
import sys

ROOT = pathlib.Path("students/templates")
APPLY = "--apply" in sys.argv

TARGETS = {"body", ".main-content", ".app-content"}
# never touch: transparent (lets a page gradient show through), gradients,
# images, and print-white
KEEP_VALUE = re.compile(r"transparent|gradient|url\(|#fff\b|#ffffff", re.I)


def blank_comments(text):
    """Replace comment bodies with spaces, preserving newlines and offsets."""
    out = list(text)
    for m in re.finditer(r"/\*.*?\*/", text, re.S):
        for i in range(m.start(), m.end()):
            out[i] = "\n" if text[i] == "\n" else " "
    return "".join(out)


def media_at(text, pos):
    """Innermost @media whose block encloses pos."""
    best = ""
    for m in re.finditer(r"@media([^{]*)\{", text):
        if m.start() >= pos:
            break
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if m.start() < pos < i:
            best = " ".join(m.group(1).split())
    return best


def iter_rules(text):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        sel_raw = m.group(1)
        if re.search(r"@keyframes|^\s*(from|to)\s*$", sel_raw):
            continue
        sels = [" ".join(s.split()) for s in sel_raw.split(",")]
        sels = [s for s in sels if s]
        if not sels or not all(s in TARGETS for s in sels):
            continue
        if "background" not in m.group(2):
            continue
        yield m, sels


def main():
    plan = []
    for p in sorted(ROOT.rglob("*.html")):
        raw = p.read_text(encoding="utf-8", errors="replace")
        text = blank_comments(raw)
        found = list(iter_rules(text))
        if not found:
            continue
        print(f"\n=== {p.name} ===")
        for m, sels in found:
            line = text[: m.start()].count("\n") + 1
            media = media_at(text, m.start())
            decls = [d.strip() for d in m.group(2).split(";") if d.strip()]
            bg_decls = [d for d in decls if d.startswith("background")]
            value = bg_decls[0] if bg_decls else ""
            is_print = "print" in media
            is_mobile = bool(re.search(r"max-width", media))
            keep = bool(KEEP_VALUE.search(value))
            candidate = not is_print and not is_mobile and not keep
            tag = "PRINT " if is_print else ("MOBILE" if is_mobile else "DESKTOP")
            print(f"  L{line:<5} {tag:<7} {'CANDIDATE' if candidate else 'keep     '}  "
                  f"{', '.join(sels)}")
            print(f"         {value[:110]}")
            if media:
                print(f"         @media {media[:80]}")
            if candidate:
                plan.append((p, m.start(), m.end(), decls, bg_decls, sels, line))

    nfiles = len({p for p, *_ in plan})
    print(f"\n{len(plan)} candidate rule(s) in {nfiles} file(s)")
    if not APPLY:
        print("(dry run — pass --apply to modify)")
        return

    by_file = {}
    for item in plan:
        by_file.setdefault(item[0], []).append(item)

    for p, items in by_file.items():
        raw = p.read_text(encoding="utf-8", errors="replace")
        # apply back-to-front so earlier offsets stay valid
        for _, start, end, decls, bg_decls, sels, line in sorted(
            items, key=lambda it: -it[1]
        ):
            new_decls = [d for d in decls if d not in bg_decls]
            if new_decls:
                replacement = ",".join(sels) + "{" + ";".join(new_decls) + ";}"
            else:
                replacement = ""
            raw = raw[:start] + replacement + raw[end:]
        p.write_text(raw, encoding="utf-8")
        print(f"  edited {p.name} ({len(items)} rule(s))")


if __name__ == "__main__":
    main()
