#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Show the tabpane open/close order in the panel HTML.

A pane is any <div class="tabpane" id="pane-x"> ... </div><!-- /pane -->.
For the tabs to work, every pane must OPEN and CLOSE before the next opens:
if they nest, hiding the outer display:none pane also hides the inner ones and
only one tab ever appears to work.

Exit code 0 = sequential (correct), 1 = nested (broken).
"""
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SRC = r"F:\MyRepository\PortfolioNewsUpdater\cloud_manager.py"
src = io.open(SRC, encoding="utf-8").read()

# Find every pane div in the whole file rather than slicing at "HTML = ".
# The old slice started too late (it matched a later assignment) and silently
# dropped pane-scan from the report.
panes = []
for m in re.finditer(r'<div class="tabpane(?P<extra>[^"]*)"\s+id="(?P<id>pane-\w+)"', src):
    panes.append((src[:m.start()].count("\n") + 1, m.group("id"), m.group("extra").strip()))

opens = [p for p, _, _ in panes]
closes = [src[:m.start()].count("\n") + 1
          for m in re.finditer(r"<!-- /pane -->", src)]

print(f"pane divs found   : {len(panes)}")
print(f"closing markers   : {len(closes)}")
print()

depth = 0
broken = False
events = sorted(
    [(l, "open", i, e) for l, i, e in panes] + [(l, "close", "", "") for l in closes]
)
for line, kind, name, extra in events:
    if kind == "open":
        if depth:
            broken = True
            print(f"  L{line:<5} OPEN  {name:12} at depth {depth}  <-- NESTED INSIDE A PANE")
        else:
            print(f"  L{line:<5} OPEN  {name:12} at depth 0" + (f"  ({extra})" if extra else ""))
        depth += 1
    else:
        depth -= 1
        print(f"  L{line:<5} CLOSE              depth now {depth}")
        if depth < 0:
            broken = True
            print("        ^^^ closing marker with no open pane")

print()
if depth != 0:
    broken = True
    print(f"UNBALANCED: {depth} pane(s) left open at end of file.")
if len(panes) != len(closes):
    broken = True
    print(f"MISMATCH: {len(panes)} open divs vs {len(closes)} closing markers.")

ids = [i for _, i, _ in panes]
if len(set(ids)) != len(ids):
    broken = True
    print(f"DUPLICATE pane ids: {ids}")

print("VERDICT: " + ("BROKEN - panes nest, tabs cannot work independently"
                     if broken else
                     f"OK - {len(panes)} panes open and close sequentially: {', '.join(ids)}"))
sys.exit(1 if broken else 0)
