#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the EVENT-level guard: one earnings event = one push, however many
outlets cover it afterwards under their own URL and wording."""
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
COPY = os.path.join(BASE, "_event.db")
shutil.copyfile(os.path.join(BASE, "news.db"), COPY)
nu.DB_FILE = COPY
nu.NO_WRITE = False
nu._db().close()
conn = sqlite3.connect(COPY)
conn.row_factory = sqlite3.Row
now = datetime.now(nu.EASTERN)
cfg = {"push_min_importance": 4, "max_digest_items": 10,
       "push_max_per_ticker": 2, "push_max_age_hours": 72,
       "event_repeat_window_days": 7}
fail = 0

print("=" * 78)
print("1. event_key extraction")
print("=" * 78)
CASES = [
    ("Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion",
     "earnings:2026Q2"),
    ("慧择 (HUIZ) 2026年第二季度业绩电话会议：上半年总签单保费创历史新高",
     "earnings:2026Q2"),
    ("Huize Q2 2026 Earnings Results - AlphaStreet", "earnings:2026Q2"),
    ("Lufax Holding Ltd. 2026 Q2 Results - Earnings Call Presentation",
     "earnings:2026Q2"),
    ("Yuanbao Inc. Reports Second Quarter 2026 Financial Results",
     "earnings:2026Q2"),
    ("Yuanbao Releases Q2 2026 Financial Results", "earnings:2026Q2"),
    ("Qifu Technology Announces Unaudited Financial Results for the Second "
     "Quarter Ended June 30, 2026", "earnings:2026Q2"),
    ("Net profit plunges 80%! Behind Lexin's installment e-commerce surge",
     None),
    ("国家金融监督管理总局就《保险法》公开征求意见", None),
    ("Lufax Sets October EGM to Extend Convertible Notes", "connote"),
    ("LexinFintech Holdings Ltd.: Citigroup lowers rating to Neutral", None),
    # A bare year is not a fiscal period, so the annual key is periodless.
    ("Huize Holding Declares Special Dividend for 2026", "dividend"),
]
for title, want in CASES:
    got = nu.event_key({"title": title, "title_en": ""})
    ok = got == want
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {str(got):20s} (want {str(want):20s}) "
          f"{title[:44]}")

print()
print("=" * 78)
print("2. a DIFFERENT quarter must NOT be blocked by the previous one")
print("=" * 78)
# Start from a clean slate for this ticker: the live fixture already carries
# HUIZ Q2 in the repeat ledger (that is the feature working), which would mask
# what this case is actually testing.
conn.execute("DELETE FROM pushed_stories WHERE ticker='HUIZ'")
conn.commit()
print("  cleared HUIZ history for a controlled test")
q1 = {"ticker": "HUIZ", "source": "Tavily", "url": "https://q1.example/1",
      "title": "Huize (HUIZ) Q1 2026 Earnings Call: Q1 GWP Grows 20%",
      "title_en": "", "published_at": (now - timedelta(days=40))
      .strftime("%Y-%m-%d %H:%M:%S")}
nu.record_pushed_stories(conn, [q1], now=now)
print(f"  pushed Q1 event_key = {nu.event_key(q1)}")
# A Q1 headline that names the period-END date only ("the quarter ended
# March 31, 2026") must still land on the SAME event key as "Q1 2026".
alt_q1 = {"ticker": "HUIZ", "source": "Exa", "url": "https://q1.example/alt",
          "title": "Huize Announces Results for the Quarter Ended March 31, 2026",
          "title_en": "", "published_at": now.strftime("%Y-%m-%d %H:%M:%S")}
alt_key = "earnings:" + (nu._event_period(alt_q1["title"]) or "?")
ok_alt = alt_key == "earnings:2026Q1"
if not ok_alt:
    fail += 1
print(f"  [{'PASS' if ok_alt else 'FAIL'}] period-end-only phrase -> {alt_key} "
      f"(want earnings:2026Q1)")
alt_blocked = nu._story_already_pushed(conn, dict(alt_q1,
                                                 _story_tokens=nu.story_tokens(alt_q1["title"])), now)
ok_altb = alt_blocked
if not ok_altb:
    fail += 1
print(f"  [{'PASS' if ok_altb else 'FAIL'}] the same Q1 event phrased differently "
      f"is BLOCKED={alt_blocked}")
q2 = {"ticker": "HUIZ", "source": "Exa", "url": "https://q2.example/2",
      "title": "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record",
      "title_en": "", "published_at": now.strftime("%Y-%m-%d %H:%M:%S")}
blocked = nu._story_already_pushed(conn, dict(q2), now)
ok = not blocked
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] Q2 2026 pushed={not blocked} "
      f"(must not be blocked by Q1)")

print()
print("=" * 78)
print("3. the SAME event from another outlet, another URL, another language")
print("=" * 78)
q2["_story_tokens"] = nu.story_tokens(q2["title"])
nu.record_pushed_stories(conn, [q2], now=now)
print(f"  Q2 pushed. event_key = {nu.event_key(q2)}")
LATER = [
    ("english, new outlet",
     "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion",
     "Tavily", "https://other.example/a"),
    ("chinese translation",
     "慧择 (HUIZ) 2026年第二季度业绩电话会议：上半年总签单保费创历史新高",
     "GoogleNewsZH", "https://other.example/b"),
    ("analyst write-up of the same quarter",
     "Huize Q2 2026 Earnings Results: premium growth accelerates",
     "Exa", "https://other.example/c"),
]
for label, title, src, url in LATER:
    it = {"ticker": "HUIZ", "source": src, "title": title, "title_en": "",
          "url": url, "published_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    it["_story_tokens"] = nu.story_tokens(title)
    blocked = nu._story_already_pushed(conn, it, now)
    ok = blocked
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {'BLOCKED' if blocked else 'pushed ':8s} "
          f"{label:34s} {title[:38]}")

print()
print("=" * 78)
print("4. a genuinely NEW event for the same ticker still gets through")
print("=" * 78)
NEWS = [
    ("regulatory action", "慧择保险经纪 因违规被罚款500万元", "regulatory"),
    ("new product", "Huize Launches AI Claims Platform in Singapore", "other"),
]
for label, title, cat in NEWS:
    it = {"ticker": "HUIZ", "source": "Exa", "title": title, "title_en": "",
          "url": f"https://new.example/{abs(hash(title)) % 9999}",
          "published_at": now.strftime("%Y-%m-%d %H:%M:%S"),
          "importance": 8, "push": True, "category": cat, "lang": "zh"}
    it["_story_tokens"] = nu.story_tokens(title)
    out = nu.select_push_items([it], cfg, conn=conn, now=now)
    ok = bool(out)
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] pushed={bool(out)}  {label:20s} "
          f"{title[:44]}")

print()
print("=" * 78)
print("5. event window = 0 disables the EVENT guard only")
print("=" * 78)
it = {"ticker": "HUIZ", "source": "Exa",
      "title": "慧择 (HUIZ) 2026年第二季度业绩电话会议：上半年总签单保费创历史新高",
      "title_en": "", "url": "https://window.example/z",
      "published_at": now.strftime("%Y-%m-%d %H:%M:%S"),
      "importance": 9, "push": True, "category": "earnings", "lang": "zh"}
it["_story_tokens"] = nu.story_tokens(it["title"])
cfg_off = dict(cfg, event_repeat_window_days=0)
out = nu.select_push_items([it], cfg_off, conn=conn, now=now)
ok = bool(out)
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] with window=0 the earnings item is "
      f"pushable again (pushed={bool(out)})")

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
