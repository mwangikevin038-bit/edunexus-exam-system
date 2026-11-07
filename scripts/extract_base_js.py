"""Move base.html's trailing inline script into static/js/app.js (one-off).

The block is lifted verbatim so execution order and the globals other
templates rely on (window.showToast / showLoader / hideLoader) are unchanged.
"""
import pathlib

BASE = pathlib.Path("students/templates/students/base.html")
OUT = pathlib.Path("static/js/app.js")

HEADER = """/* ============================================================================
   EDUNEXUS — app shell behaviour
   Loaded synchronously at the end of <body>, in place of the inline block that
   used to live in students/base.html.  Nothing here is wrapped in an IIFE:
   page templates call showLoader()/hideLoader()/showToast() directly, and
   headteacher_comments_modal / profile check for window.showToast.
   ========================================================================== */

"""

text = BASE.read_text(encoding="utf-8")
lines = text.splitlines(keepends=True)

start = next(i for i, l in enumerate(lines) if l.strip() == "<!-- SCRIPTS -->")
open_i = start + 1
assert lines[open_i].strip() == "<script>", lines[open_i]
close_i = next(i for i in range(open_i, len(lines)) if lines[i].strip() == "</script>")

js = "".join(lines[open_i + 1:close_i])
OUT.write_text(HEADER + js, encoding="utf-8")

replacement = (
    "    <!-- SCRIPTS -->\n"
    "    <script src=\"{% static 'js/app.js' %}?v=1\"></script>\n"
)

# Drop everything from <!-- SCRIPTS --> through the stray empty <script></script>
# that followed the block, up to (but not including) edunexus_print.js.
tail = next(i for i in range(close_i, len(lines)) if "edunexus_print.js" in lines[i])
# keep the blank line(s) immediately before edunexus_print.js out of the cut
while lines[tail - 1].strip() == "":
    tail -= 1

new = "".join(lines[:start]) + replacement + "\n" + "".join(lines[tail:])
BASE.write_text(new, encoding="utf-8")

print(f"extracted {js.count(chr(10))} lines -> {OUT}")
print(f"base.html: {len(lines)} -> {len(new.splitlines())} lines")
