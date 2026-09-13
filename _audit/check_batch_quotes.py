#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Batch-file hygiene: quote parity and CRLF line endings.

Both checks exist because both bugs were real here, and both were invisible
until the launcher was actually run.

1. QUOTE PARITY. cmd.exe counts double quotes before deciding a line is a
   comment, so a line with an ODD number of quotes makes the parser keep
   consuming following lines while it looks for the closer. That silently eats
   real statements. open_panel.cmd had:

       REM  stdin and exits immediately with "Input redirection is not
       REM  supported" when there is none (e.g. launched from another

   One quote per line. The result: `setlocal enabledelayedexpansion` and
   `set "URL="` never ran, so the script printed
   `[open_panel] opening !URL!` and Windows tried to open a FILE named !URL!.

2. CRLF LINE ENDINGS. cmd.exe mis-parses batch files that use bare LF: the
   same line-swallowing, plus REM comments executed as commands. Both files in
   this repo were LF-only, so whether the launcher worked at all depended on
   the cloner's core.autocrlf setting. .gitattributes now pins eol=crlf.

Exit 0 = clean, 1 = at least one problem.
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

TARGETS = sorted(list(ROOT.glob("*.bat")) + list(ROOT.glob("*.cmd")))

# A legitimate odd-quote line: the standard "strip quotes from a variable"
# assignment, e.g.  set "URL=!URL:"=!"  . Verified to work in a clean batch
# file, so it is exempt rather than reported on every run.
QUOTE_STRIP_IDIOM = re.compile(
    r'^\s*(?:if\s+defined\s+\w+\s+)?set\s+"[A-Za-z_]\w*=!.*:"=!"\s*$')

quote_problems = []
eol_problems = []
total_lines = 0

for path in TARGETS:
    raw = path.read_bytes()
    bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
    if bare_lf:
        eol_problems.append((path.name, bare_lf, len(raw)))

    text = raw.decode("cp1252", errors="replace")
    issues = []
    for n, line in enumerate(text.split("\n"), 1):
        total_lines += 1
        body = line.rstrip("\r")
        if QUOTE_STRIP_IDIOM.match(body):
            continue
        # ^" and \" are escapes and do not participate in parity.
        stripped = body.replace('^"', "").replace('\\"', "")
        if stripped.count('"') % 2:
            issues.append((n, body.strip()[:88]))
    if issues:
        quote_problems.append((path.name, issues))

print(f"batch files checked : {len(TARGETS)}  ({total_lines} lines)")
for p in TARGETS:
    print(f"  {p.name}")

if quote_problems:
    print()
    for name, issues in quote_problems:
        print(f"ODD QUOTES: {name} - {len(issues)} line(s)")
        for n, body in issues:
            print(f"    line {n}: {body}")

if eol_problems:
    print()
    for name, bare, size in eol_problems:
        print(f"LF-ONLY: {name} - {bare} bare LF of {size} bytes "
              f"(cmd.exe requires CRLF)")

if quote_problems or eol_problems:
    print()
    print("VERDICT: BROKEN - cmd.exe will mis-parse these files; real commands")
    print("         (setlocal, set, if) are silently swallowed.")
else:
    print()
    print("VERDICT: OK - CRLF endings and balanced quotes throughout")

sys.exit(1 if (quote_problems or eol_problems) else 0)
