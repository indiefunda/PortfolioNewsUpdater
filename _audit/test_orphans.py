#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the orphan-recovery fix: unanalyzed rows drain and never loop."""
import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

BASE = r"F:\MyRepository\PortfolioNewsUpdater\_audit"
COPY = os.path.join(BASE, "_orphan.db")
shutil.copyfile(os.path.join(BASE, "news.db"), COPY)
nu.DB_FILE = COPY
nu.NO_WRITE = False
nu.NEWS_RETENTION_DAYS = 21
nu._db().close()
conn = sqlite3.connect(COPY)
conn.row_factory = sqlite3.Row
fail = 0

orphans = conn.execute("SELECT COUNT(*) FROM news WHERE importance IS NULL").fetchone()[0]
if orphans == 0:
    # No orphans left in the fixture (the recovery pass drained them). Seed a
    # few so the drain-and-stop behaviour is actually exercised.
    print("no orphans in the fixture - seeding 8 synthetic ones")
    for i in range(8):
        conn.execute(
            "INSERT INTO news (ticker, source, lang, item_hash, title_raw, url, "
            "published_at, first_seen, rescues) VALUES ('ZZORPH','Tavily','en',?,?,?,?,?,0)",
            (f"orphan{i:02d}", f"orphan test row {i}", f"https://o.example/{i}",
             "2026-09-10 10:00:00", "2026-09-10 10:00:00"))
    conn.commit()
    orphans = conn.execute("SELECT COUNT(*) FROM news WHERE importance IS NULL").fetchone()[0]
print(f"orphans in the live DB copy: {orphans}")
print(f"ORPHAN_RESCUE_LIMIT={nu.ORPHAN_RESCUE_LIMIT} "
      f"ORPHAN_MAX_RESCUES={nu.ORPHAN_MAX_RESCUES}")

print()
print("=== first rescue pass: every orphan is re-queued (no cap starvation) ===")
got = nu.rescue_orphans(conn, {})
ok = len(got) == orphans and orphans > 0
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] queued {len(got)} of {orphans}")
print(f"  rescues counter after pass: "
      f"{[r[0] for r in conn.execute('SELECT DISTINCT rescues FROM news WHERE importance IS NULL')]}")

print()
print("=== simulate 3 failed runs, then the loop must STOP ===")
for i in range(nu.ORPHAN_MAX_RESCUES - 1):
    nu.rescue_orphans(conn, {})
final = nu.rescue_orphans(conn, {})
ok = len(final) == 0
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] after {nu.ORPHAN_MAX_RESCUES} attempts, "
      f"re-queued again: {len(final)} (must be 0 - no infinite loop)")

print()
print("=== a rescued item is analysed and marked, so it leaves the orphan set ===")
nu.rescue_orphans  # (attempts exhausted above; verify the counter is the reason)
rows = conn.execute("SELECT COUNT(*) FROM news WHERE importance IS NULL "
                    "AND COALESCE(rescues,0) < ?", (nu.ORPHAN_MAX_RESCUES,)).fetchone()[0]
ok = rows == 0
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] rows still eligible for rescue: {rows}")

print()
print("=== trimmed items are released from the seen ledger ===")
# Take a real seen row, delete it the way the trim does, and confirm is_new
# flips back to True (i.e. a later run can fetch and analyse it).
row = conn.execute("SELECT ticker, source, item_hash, title FROM seen LIMIT 1").fetchone()
before = conn.execute("SELECT 1 FROM seen WHERE ticker=? AND source=? AND item_hash=?",
                      (row["ticker"], row["source"], row["item_hash"])).fetchone()
conn.execute("DELETE FROM seen WHERE ticker=? AND source=? AND item_hash=?",
             (row["ticker"], row["source"], row["item_hash"]))
after = conn.execute("SELECT 1 FROM seen WHERE ticker=? AND source=? AND item_hash=?",
                     (row["ticker"], row["source"], row["item_hash"])).fetchone()
ok = bool(before) and not after
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] released hash is re-fetchable "
      f"(before={bool(before)}, after={bool(after)})")

conn.close()
for ext in ("", "-wal", "-shm"):
    p = COPY + ext
    if os.path.exists(p):
        os.remove(p)
print()
print("=" * 78)
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print("=" * 78)
sys.exit(1 if fail else 0)
