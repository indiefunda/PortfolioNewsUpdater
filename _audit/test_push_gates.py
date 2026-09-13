#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the AGE gate and the REPEAT gate on a COPY of the live news.db."""
import importlib.util
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

BASE = r"F:\MyRepository\PortfolioNewsUpdater\_audit"
COPY = os.path.join(BASE, "_test_copy.db")
shutil.copyfile(os.path.join(BASE, "news.db"), COPY)
# Run the real migration path against the copy so pushed_stories / rescues
# exist exactly as they will on the VM.
nu.DB_FILE = COPY
nu.NO_WRITE = False
_mig = nu._db()
_mig.close()
conn = sqlite3.connect(COPY)
conn.row_factory = sqlite3.Row
cfg = {"push_min_importance": 4, "max_digest_items": 10,
       "push_max_per_ticker": 2, "push_max_age_hours": 72}
fail = 0

print("=" * 78)
print("AGE GATE: an item published > 72h ago must never be pushed")
print("=" * 78)
# Pick an old item that is NOT regulatory: regulatory items are deliberately
# EXEMPT from the age gate, so using one as the fixture made this test pass or
# fail depending on which row happened to be oldest.
REG_RE = r"(处罚|罚款|立案|约谈|调查|退市|监管|违规|delist|fraud|investigat|penalt|enforcement|regulat)"
old = None
for row in conn.execute(
        "SELECT ticker, source, title_raw, title_en, published_at, first_seen, lang "
        "FROM news WHERE published_at != '' AND importance IS NOT NULL "
        "ORDER BY published_at ASC LIMIT 200"):
    hay = f"{row['title_raw'] or ''} {row['title_en'] or ''}"
    if not nu.re.search(REG_RE, hay, nu.re.IGNORECASE):
        old = row
        break
if old is None:
    print("  [SKIP] no non-regulatory old row in the fixture")
    old = conn.execute(
        "SELECT ticker, source, title_raw, title_en, published_at, first_seen, lang "
        "FROM news WHERE published_at != '' AND importance IS NOT NULL "
        "ORDER BY published_at ASC LIMIT 1").fetchone()
it_old = dict(old)
it_old.update({"push": True, "importance": 9, "category": "earnings",
               "_story_tokens": nu.story_tokens(it_old["title_raw"])})
age = nu._item_age_hours(it_old)
pushed = nu.select_push_items([it_old], cfg, conn=conn)
ok = not pushed and it_old.get("_too_old")
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] published {it_old['published_at']} "
      f"({age:.0f}h old) -> pushed={bool(pushed)} _too_old={it_old.get('_too_old')}")
print(f"        {it_old['title_raw'][:70]}")

print()
print("=" * 78)
print("AGE GATE exemption: a REGULATORY item is pushed even when old")
print("=" * 78)
it_reg = dict(it_old)
it_reg["_too_old"] = False
it_reg["title_raw"] = "分期乐 因违规被罚款500万元"
it_reg["title"] = it_reg["title_raw"]
it_reg["title_en"] = "Fenqile fined 5 million yuan for violations"
it_reg["_story_tokens"] = nu.story_tokens(it_reg["title_raw"])
it_reg["importance"] = 9
it_reg["push"] = True
it_reg["category"] = "regulatory"
it_reg.pop("_repeat", None)
it_reg.pop("_superseded", None)
pushed = nu.select_push_items([it_reg], cfg, conn=conn)
ok = bool(pushed)
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] regulatory old item pushed={bool(pushed)} "
      f"importance={it_reg.get('importance')} unique_title={it_reg.get('title','')[:34]}")
print(f"        flags: _regulatory={it_reg.get('_regulatory')} "
      f"_too_old={it_reg.get('_too_old')} _repeat={it_reg.get('_repeat')} "
      f"push={it_reg.get('push')} is_dup={it_reg.get('is_dup')} "
      f"is_known={it_reg.get('is_known')} age={nu._item_age_hours(it_reg)}")

print()
print("=" * 78)
print("REPEAT GATE: the SAME story re-reported later must be suppressed")
print("=" * 78)
# Simulate a story that already went out, then a re-report under a NEW url and
# slightly different wording (exactly the 'Huize results again' pattern).
first = {"ticker": "HUIZ", "source": "Tavily", "url": "https://a.example/1",
         "title": "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion",
         "title_en": "Huize Q2 2026 earnings: record GWP"}
nu.record_pushed_stories(conn, [first], now=datetime.now(nu.EASTERN))

repeats = [
    ("new URL, same headline",
     "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion",
     "https://b.example/2"),
    ("new outlet, tiny rewording",
     "Huize (HUIZ) Q2 2026 Earnings Call: H1 Total Written Premium Hits Record RMB 4.2 Billion",
     "https://c.example/3"),
]
for label, title, url in repeats:
    it = {"ticker": "HUIZ", "source": "Exa", "title": title, "url": url,
          "title_en": title, "importance": 9, "push": True, "category": "earnings",
          "lang": "en", "_story_tokens": nu.story_tokens(title)}
    out = nu.select_push_items([it], cfg, conn=conn)
    ok = not out and it.get("_repeat")
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:28s} pushed={bool(out)} "
          f"_repeat={it.get('_repeat')}")

print()
print("=" * 78)
print("A GENUINELY NEW story for the same ticker must still be pushed")
print("=" * 78)
fresh = {"ticker": "HUIZ", "source": "Tavily",
         "title": "Huize Announces Singapore Unit Profitability Milestone",
         "url": "https://d.example/4", "title_en":
         "Huize Announces Singapore Unit Profitability Milestone",
         "importance": 8, "push": True, "category": "press_release", "lang": "en",
         "published_at": datetime.now(nu.EASTERN).strftime("%Y-%m-%d %H:%M:%S")}
fresh["_story_tokens"] = nu.story_tokens(fresh["title"])
out = nu.select_push_items([fresh], cfg, conn=conn)
ok = bool(out) and not fresh.get("_repeat")
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] new story pushed={bool(out)} "
      f"_repeat={fresh.get('_repeat')}")

print()
print("=" * 78)
print("FAIR SEATS: 4 HUIZ + 1 QFIN + 1 CAAS candidates -> all 3 names appear")
print("=" * 78)
def mk(t, i, imp):
    ti = f"{t} story number {i} about a distinct event"
    d = {"ticker": t, "source": "Tavily", "title": ti, "url": f"https://e/{t}{i}",
         "title_en": ti, "importance": imp, "push": True, "lang": "en",
         "published_at": datetime.now(nu.EASTERN).strftime("%Y-%m-%d %H:%M:%S")}
    d["_story_tokens"] = nu.story_tokens(ti)
    return d
cands = [mk("HUIZ", 1, 10), mk("HUIZ", 2, 9), mk("HUIZ", 3, 9), mk("HUIZ", 4, 8),
         mk("QFIN", 1, 8), mk("CAAS", 1, 8)]
out = nu.select_push_items(cands, cfg, conn=conn)
names = sorted({x["ticker"] for x in out})
ok = names == ["CAAS", "HUIZ", "QFIN"]
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] pushed {len(out)}: "
      + ", ".join(f"{x['ticker']}⭐{x['importance']}" for x in out))

conn.close()
os.remove(COPY)
print()
print("=" * 78)
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print("=" * 78)
sys.exit(1 if fail else 0)
