#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Inspect the local news.db stub that local runs created in the repo root."""
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
p = r"F:\MyRepository\PortfolioNewsUpdater\news.db"
print("exists:", os.path.exists(p), "size:", os.path.getsize(p) if os.path.exists(p) else 0)
c = sqlite3.connect(p)
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
for t in tables:
    try:
        n = c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
        print(f"  {t}: {n} rows")
    except Exception as exc:
        print(f"  {t}: {exc}")
print()
# which local scripts open the real DB path without redirecting it?
import glob
print("scripts that set NO_WRITE=False (may touch the local DB):")
for f in sorted(glob.glob(r"F:\MyRepository\PortfolioNewsUpdater\_audit\*.py")):
    src = open(f, encoding="utf-8", errors="replace").read()
    if "NO_WRITE = False" in src or "NO_WRITE=False" in src:
        redirects = 'DB_FILE = ' in src or "DB_FILE=" in src
        print(f"  {os.path.basename(f):26s} redirects DB_FILE: {redirects}")
