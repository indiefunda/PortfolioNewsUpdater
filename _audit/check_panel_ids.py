#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-check every DOM id the panel's JavaScript looks up against the ids that
actually exist in the panel's HTML.

Why this matters: `$('typo')` returns null, and the very next property write
throws. The tab or button then silently does nothing in the browser while every
other check still passes - which is exactly how "the tabs don't work" and
"ctrl-shift-r does nothing" felt from the outside.

Exit 0 = every referenced id exists, 1 = at least one dangling reference.
"""
import importlib.util
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
CM = HERE.parent / "cloud_manager.py"

spec = importlib.util.spec_from_file_location("cm", CM)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
html = m.HTML

js = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
markup = html[:html.index("<script>")]

declared = set(re.findall(r'id="([^"]+)"', markup))
# Ids the JS creates itself at runtime, or that live outside the main HTML body.
runtime = set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", js))
declared |= runtime

# Build a lookup of declared-id -> line number for good error messages.
decl_line = {}
for i, line in enumerate(markup.split("\n"), 1):
    for ident in re.findall(r'id="([^"]+)"', line):
        decl_line.setdefault(ident, i)

# Every $( 'id' ) and getElementById('id') in the panel script.
refs = {}
for i, line in enumerate(js.split("\n"), 1):
    for ident in re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", line):
        refs.setdefault(ident, i)
    for ident in re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", line):
        refs.setdefault(ident, i)

dangling = {k: v for k, v in refs.items() if k not in declared}

print(f"ids declared in markup : {len(declared)}")
print(f"ids referenced by JS   : {len(refs)}")
print()

if dangling:
    print("DANGLING REFERENCES (JS looks these up, HTML never defines them):")
    for ident, line in sorted(dangling.items(), key=lambda kv: kv[1]):
        print(f"  line {line:<5} $('{ident}')  -> no id=\"{ident}\" in the HTML")
    print()
    print("VERDICT: BROKEN - these will throw or return null in the browser")
else:
    print("VERDICT: OK - every referenced id is defined in the markup")

# Ids defined but never referenced are only informational (they may be pure CSS
# hooks), so they never fail the check.
unused = sorted(declared - set(refs))
if unused:
    print(f"\n(info) declared but not looked up by JS ({len(unused)}): "
          f"{', '.join(unused[:20])}")

sys.exit(1 if dangling else 0)
