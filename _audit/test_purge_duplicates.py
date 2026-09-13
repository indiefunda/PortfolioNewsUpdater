#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the duplicate purge.

The MUST_MERGE cases are real rows from the live database; the MUST_NOT_MERGE
cases are the false positives an earlier version actually produced. That
version clustered transitively through a seed and had no entity guard, so it
planned to delete "BitVentures Limited (BVC)" and "iHuman Inc. (IH)" as
duplicates of a Huize page, and merged "Torrid Holdings (CURV)" with
"Maase Inc. (MAAS)" - both different companies, both scoring over the
threshold because a generic quote-page template supplies nearly every token.
"""
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0


def check(label, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def row(rid, title, ticker="MACRO", source="Sina724", importance=5, pushed=0,
        lang="en"):
    """Match the column order of nu._DUP_COLS."""
    return (rid, ticker, source, lang, title, importance, pushed,
            "2026-09-11 17:00:58", "http://x/" + str(rid))


print("=" * 76)
print("TEST 1 - the user's actual rows")
print("=" * 76)
citia = row(1, "Citi expects Fed to hike in September, cut before mid-2027")
citib = row(2, "Citi forecasts September Fed hike, then cuts by 2027")
jpm = row(3, "JPMorgan Changes Course, Now Expects Fed to Raise Rates in "
             "September and December")

check("Citi reworded pair IS a duplicate", nu._dup_pair_matches(citia, citib) is True)
check("Citi vs JPMorgan is NOT", nu._dup_pair_matches(citia, jpm) is False)
check("Citi reworded vs JPMorgan is NOT", nu._dup_pair_matches(citib, jpm) is False)

print()
print("=" * 76)
print("TEST 2 - false positives an earlier version produced")
print("=" * 76)
torrid = row(10, "Torrid Holdings Inc. (CURV) Stock Price, News, Quote & History - Yahoo Finance")
maase = row(11, "Maase Inc. (MAAS) Stock Price, News, Quote & History")
crd = row(12, "CRD.A Chart and Quote: NYSE:CRD.A - TradingView")
crvl = row(13, "CRVL chart and quote: NASDAQ:CRVL - TradingView")
huiz_pb = row(14, "Huize Holding Limited Price to Book Forward - NASDAQ:HUIZ")
bvc = row(15, "BitVentures Limited (BVC) Stock Price, News, Quote and History")
ihuman = row(16, "iHuman Inc. (IH) Stock Price, News, Quote & History - Yahoo Finance")

check("Torrid(CURV) vs Maase(MAAS) NOT merged",
      nu._dup_pair_matches(torrid, maase) is False)
check("CRD.A vs CRVL NOT merged", nu._dup_pair_matches(crd, crvl) is False)
check("HUIZ price page vs BitVentures(BVC) NOT merged",
      nu._dup_pair_matches(huiz_pb, bvc) is False)
check("HUIZ price page vs iHuman(IH) NOT merged",
      nu._dup_pair_matches(huiz_pb, ihuman) is False)

print()
print("=" * 76)
print("TEST 2b - different ACTORS on the same subject are different news")
print("=" * 76)
citi_qfin = row(17, "Citi Downgrades Qifu Technology (QFIN) to Sell, Cuts Target Price to $8")
bofa_qfin = row(18, "BofA cuts Qifu Technology stock price target to $11 on earnings outlook")
check("Citi action vs BofA action NOT merged",
      nu._dup_pair_matches(citi_qfin, bofa_qfin) is False)
# ...but two Citi write-ups of the same call still merge.
citi_again = row(19, "Citi downgrades Qifu Technology to Sell, target price cut to $8")
check("two Citi write-ups of one call DO merge",
      nu._dup_pair_matches(citi_qfin, citi_again) is True)

print()
print("=" * 76)
print("TEST 3 - opposite directions must never collapse")
print("=" * 76)
hike = row(20, "Citi expects Fed to hike rates in September")
cut = row(21, "Citi expects Fed to cut rates in September")
check("'to hike' vs 'to cut' NOT merged", nu._dup_pair_matches(hike, cut) is False)
up = row(22, "Qifu Technology upgrades full-year guidance")
down = row(23, "Qifu Technology downgrades full-year guidance")
check("upgrades vs downgrades NOT merged", nu._dup_pair_matches(up, down) is False)
both = row(24, "Citi expects Fed to hike in September, cut by 2027")
check("a headline naming BOTH sides is still comparable to the reworded copy",
      nu._opposite_polarity(nu.story_tokens(both[4]),
                            nu.story_tokens(citib[4])) is False)

print()
print("=" * 76)
print("TEST 4 - clustering end to end (in-memory DB)")
print("=" * 76)
import sqlite3  # noqa: E402

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE news (id INTEGER PRIMARY KEY, ticker TEXT, source TEXT,"
             " lang TEXT, title_raw TEXT, title_en TEXT, importance INTEGER,"
             " pushed INTEGER, first_seen TEXT, url TEXT)")
rows = [citia, citib, jpm, torrid, maase, crd, crvl, huiz_pb, bvc, ihuman]
for r in rows:
    conn.execute("INSERT INTO news (id,ticker,source,lang,title_en,importance,"
                 "pushed,first_seen,url) VALUES (?,?,?,?,?,?,?,?,?)",
                 (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]))
groups = nu.find_duplicate_groups(conn)
victims = [v for g in groups for v in g["victims"]]
vids = sorted(v[0] for v in victims)
check("exactly one duplicate group", len(groups) == 1, f"got {len(groups)}")
check("only the Citi copy is removed", vids == [2], f"victims={vids}")
check("no distinct company deleted",
      not ({10, 11, 12, 13, 14, 15, 16} & set(vids)), f"victims={vids}")

print()
print("=" * 76)
print("TEST 5 - a pushed row is never deleted")
print("=" * 76)
conn2 = sqlite3.connect(":memory:")
conn2.execute("CREATE TABLE news (id INTEGER PRIMARY KEY, ticker TEXT, source TEXT,"
              " lang TEXT, title_raw TEXT, title_en TEXT, importance INTEGER,"
              " pushed INTEGER, first_seen TEXT, url TEXT)")
for r in (row(30, "Poni Financial Advisory expands services", pushed=1),
          row(31, "Poni Financial Advisory expands services", pushed=1),
          row(32, "Poni Financial Advisory expands services", pushed=0)):
    conn2.execute("INSERT INTO news (id,ticker,source,lang,title_en,importance,"
                  "pushed,first_seen,url) VALUES (?,?,?,?,?,?,?,?,?)",
                  (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]))
g2 = nu.find_duplicate_groups(conn2)
v2 = sorted(v[0] for g in g2 for v in g["victims"])
check("only the unpushed copy goes", v2 == [32], f"victims={v2}")
check("both pushed copies survive",
      not ({30, 31} & set(v2)), f"victims={v2}")

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
