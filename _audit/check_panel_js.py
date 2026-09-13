#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Syntax-check the panel's JavaScript for real.

The old version counted quotes per line and flagged any line with an odd number.
That produced permanent false positives on ordinary comments containing an
apostrophe ("the user's config", "the server's DEFAULT_CONFIG"), so the checker
cried wolf on every run and real breakage could hide behind the noise.

The failure mode it was trying to catch is real though: cloud_manager.py holds
the HTML in a Python triple-quoted string, so a `\\n` sequence inside a JS
string literal becomes a REAL newline and splits the string across lines,
killing the whole script.  The definitive test for that is a JS parser, so we
hand the extracted source to `node --check`.

Also writes _audit/panel.js so test_tabs.js can load the same extraction.
"""
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
CM = HERE.parent / "cloud_manager.py"

spec = importlib.util.spec_from_file_location("cm", CM)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
js = re.search(r"<script>(.*?)</script>", m.HTML, re.S).group(1)

# Keep the extraction test_tabs.js consumes in sync with what we check.
(HERE / "panel.js").write_text(js, encoding="utf-8")

lines = js.split("\n")
print(f"panel JS: {len(lines)} lines  ->  {HERE / 'panel.js'}")

problems = []

# ---- 1. definitive: ask a real JS parser --------------------------------
node = shutil.which("node")
if node:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(js)
        tmp = fh.name
    try:
        proc = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        if proc.returncode == 0:
            print("node --check            : OK (parses cleanly)")
        else:
            print("node --check            : FAILED")
            err = (proc.stderr or "").strip()
            print("  " + err.replace("\n", "\n  ")[:1200])
            problems.append("JavaScript does not parse")
    finally:
        Path(tmp).unlink(missing_ok=True)
else:
    print("node --check            : SKIPPED (node not on PATH)")

# ---- 2. targeted: newline inside a string literal -----------------------
# A JS string literal that opens and does not close on the same line, IGNORING
# comments and regex/division ambiguity, is the exact symptom of the
# triple-quoted-HTML bug.  Comments are stripped first so apostrophes in prose
# no longer count.
def strip_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    state = None  # None | "'" | '"' | '`' | '//' | '/*'
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "//"; i += 2; continue
            if c == "/" and nxt == "*":
                state = "/*"; i += 2; continue
            if c in "'\"`":
                state = c
            out.append(c)
        elif state == "//":
            if c == "\n":
                state = None; out.append(c)
        elif state == "/*":
            if c == "*" and nxt == "/":
                state = None; i += 2; continue
            if c == "\n":
                out.append(c)
        else:  # inside a string
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(src[i + 1]); i += 2; continue
            elif c == state:
                state = None
        i += 1
    return "".join(out)


clean = strip_comments(js)
# Only run the heuristic when there is no parser to ask. When node is present,
# `node --check` is strictly better: an unterminated string (the actual bug this
# guards against) cannot parse, while the heuristic also trips over apostrophes
# in comments and on regex literals like /[&<>"']/ - false alarms that would
# otherwise fail the suite on healthy code.
if not node:
    for i, line in enumerate(clean.split("\n")):
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*"):
            continue
        j, in_s = 0, None
        while j < len(stripped):
            ch = stripped[j]
            if in_s:
                if ch == "\\":
                    j += 2; continue
                if ch == in_s:
                    in_s = None
            elif ch in "'\"":
                in_s = ch
            j += 1
        if in_s is not None:
            problems.append(f"line {i + 1}: string opened with {in_s} never closed: "
                            f"{stripped[:90]}")

if problems:
    print("\nPROBLEMS:")
    for p in problems:
        print(f"  {p}")
    print("\nVERDICT: BROKEN")
else:
    print("string-literal scan     : "
          + ("skipped (node --check is authoritative)"
             if node else "OK (no unterminated strings)"))
    print("\nVERDICT: OK")

sys.exit(1 if problems else 0)
