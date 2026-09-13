#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test delete_news_item: removes one row, clears the right ledger entry, and
leaves everything else untouched."""
import importlib.util, os, shutil, sqlite3, sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec); spec.loader.exec_module(nu)

BASE = r"F:\MyRepository\PortfolioNewsUpdater\_audit"
COPY = os.path.join(BASE, "_del.db")
shutil.copyfile(os.path.join(BASE, "news.db"), COPY)
nu.DB_FILE = COPY; nu.NO_WRITE = False
nu._db().close()
conn = sqlite3.connect(COPY); conn.row_factory = sqlite3.Row
fail = 0

# A pushed row with a real URL + a ledger entry, like the panel would show.
url = "https://example.com/huize-q2-earnings"
title = "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion"
conn.execute("INSERT INTO news (ticker, source, lang, item_hash, title_raw, "
             "title_en, importance, pushed, url, first_seen) "
             "VALUES ('HUIZ','Tavily','en','deadbeef01',?,?,9,1,?,?)",
             (title, title, url, datetime.now(nu.EASTERN).strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
nu.record_pushed_stories(conn, [{"ticker": "HUIZ", "source": "Tavily",
                                 "title": title, "url": url,
                                 "title_en": title}],
                         now=datetime.now(nu.EASTERN))
before_news = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
before_led = conn.execute("SELECT COUNT(*) FROM pushed_stories").fetchone()[0]
print(f"before: news={before_news} ledger={before_led}")

print()
print("=== 1. delete the row (panel passes ticker|source|item_hash) ===")
res = nu.delete_news_item(conn, "HUIZ", source="Tavily", item_hash="deadbeef01")
ok = res["deleted"] == 1
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] deleted={res['deleted']} "
      f"ledger_removed={res['ledger_removed']}")
gone = conn.execute("SELECT COUNT(*) FROM news WHERE item_hash='deadbeef01'").fetchone()[0]
ok = gone == 0
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] row is gone")
after_news = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
ok = after_news == before_news - 1
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] only one row removed "
      f"(news={after_news}, expected {before_news-1})")
led = conn.execute("SELECT COUNT(*) FROM pushed_stories").fetchone()[0]
ok = led < before_led
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] ledger entry cleared (ledger={led})")

print()
print("=== 2. the story is no longer suppressed (can be pushed again) ===")
it = {"ticker": "HUIZ", "source": "Exa", "url": url, "title": title,
      "title_en": title}
it["_story_tokens"] = nu.story_tokens(title)
blocked = nu._story_already_pushed(conn, it, datetime.now(nu.EASTERN))
ok = not blocked
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] blocked={blocked} (want False)")

print()
print("=== 3. keep_ledger=True keeps the dedup memory ===")
conn.execute("INSERT INTO news (ticker, source, lang, item_hash, title_raw, "
             "title_en, importance, pushed, url, first_seen) "
             "VALUES ('HUIZ','Tavily','en','deadbeef02',?,?,9,1,?,?)",
             (title, title, url, datetime.now(nu.EASTERN).strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
nu.record_pushed_stories(conn, [{"ticker": "HUIZ", "source": "Tavily",
                                 "title": title, "url": url, "title_en": title}],
                         now=datetime.now(nu.EASTERN))
res = nu.delete_news_item(conn, "HUIZ", source="Tavily", item_hash="deadbeef02",
                          keep_ledger=True)
gone = conn.execute("SELECT COUNT(*) FROM news WHERE item_hash='deadbeef02'").fetchone()[0]
remain = conn.execute("SELECT COUNT(*) FROM pushed_stories WHERE ticker='HUIZ' AND "
                      "story_key=?", (nu.story_key({"ticker": "HUIZ", "url": url,
                                                    "title": ""}),)).fetchone()[0]
ok = gone == 0 and remain == 1 and res["ledger_removed"] == 0
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] row gone={gone==0}, ledger kept={remain==1}")

print()
print("=== 4. bogus id deletes nothing ===")
res = nu.delete_news_item(conn, "HUIZ", source="Tavily", item_hash="ffffffff99")
ok = res["deleted"] == 0
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] deleted={res['deleted']}")

print()
print("=== 5. --dump-news output carries item_hash (the panel needs it) ===")
rows = nu.list_news(conn, limit=3)
ok = all("item_hash" in r and r["item_hash"] for r in rows) and rows
if not ok: fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] keys include item_hash: "
      f"{'item_hash' in (rows[0] if rows else {})}")

conn.close()
for e in ("", "-wal", "-shm"):
    p = COPY + e
    if os.path.exists(p): os.remove(p)
print()
print("=" * 70)
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print("=" * 70)
sys.exit(1 if fail else 0)
