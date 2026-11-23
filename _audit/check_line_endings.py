#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: shell scripts must never reach the VM with CRLF line endings.

Why this exists: the panel uploads the WORKING-COPY setup_cloud.sh and runs it
with bash on Debian. A CRLF copy fails on the first continuation line with

    setup_cloud.sh: line 35: $'\\r': command not found
    setup_cloud.sh: line 74: syntax error near unexpected token `||'

which is what broke "Upload config to server" on 2026-11-23: the upload copied
every file, then the setup script died, so the server kept its previous config.
.gitattributes pins `*.sh text eol=lf`, but that only affects files GIT writes -
an editor (or a script that rewrites the file with Python's default newline
translation) can put CRLF back on disk, and git will not report a diff because it
normalises on read. So the invariant has to be checked on the bytes.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

fail = 0
checked = 0

for base, _dirs, files in os.walk(ROOT):
    if os.sep + ".git" in base:
        continue
    for name in files:
        if not name.endswith(".sh"):
            continue
        path = os.path.join(base, name)
        rel = os.path.relpath(path, ROOT)
        checked += 1
        with open(path, "rb") as fh:
            data = fh.read()
        crlf = data.count(b"\r\n")
        if crlf:
            fail += 1
            print(f"  FAIL {rel}: {crlf} CRLF line ending(s) - bash will fail on the VM")
        else:
            print(f"  OK   {rel}: LF only ({data.count(b'\\n')} lines)")

if checked == 0:
    fail += 1
    print("  FAIL no *.sh files found - the sweep is not looking where expected")

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
