#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Removing a holding from the watchlist must remove its news.

The live case: HUIZ was swapped out of `tickers`, but its rows stayed in
news.db for the 21-day retention and the hosted page (which mirrors the DB) kept
listing it. These checks pin the two halves of the fix:

  * the DB side - prune_unconfigured_tickers() deletes the removed ticker from
    every table a removal owns, keeps pseudo-tickers (MACRO/GLOBAL) and sector
    rows of still-configured names, and never treats an empty watchlist as
    "delete everything";
  * the page side - news_web.current_items() filters such rows out of the
    payload immediately, so the page and the Firestore sync are right even
    before the updater's next run prunes the database;
  * the label - MACRO/GLOBAL are TIERS, not holdings, so the page must never
    mark the China-macro section "(old)" just because it is not a stock.
"""
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import importlib.util
import json
import sqlite3
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nu = load("nu", "news_updater.py")
web = load("web", "news_web.py")

fail = 0


def check(name, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"   [{detail}]" if detail and not ok else ""))


TICKERS = ["YB", "LX", "CAAS", "QFIN", "LU", "TIGR"]

print("ticker_is_current")
print("-" * 72)
check("configured ticker is current", nu.ticker_is_current("LX", TICKERS) is True)
check("removed ticker is NOT current", nu.ticker_is_current("HUIZ", TICKERS) is False)
check("case/whitespace tolerated", nu.ticker_is_current(" lx ", TICKERS) is True)
check("MACRO is always current", nu.ticker_is_current("MACRO", TICKERS) is True)
check("GLOBAL is always current", nu.ticker_is_current("GLOBAL", TICKERS) is True)
check("sector row follows its base ticker",
      nu.ticker_is_current("LX~SECTOR", TICKERS) is True)
check("sector row of a removed ticker goes too",
      nu.ticker_is_current("HUIZ~SECTOR", TICKERS) is False)
check("empty ticker is not current", nu.ticker_is_current("", TICKERS) is False)
check("modules agree on the pseudo-tickers",
      tuple(web.PSEUDO_TICKERS) == tuple(nu.PSEUDO_TICKERS))
check("web filter agrees with the updater",
      web.ticker_is_current("HUIZ", TICKERS) is False
      and web.ticker_is_current("LU", TICKERS) is True)


def fresh_db(with_all_tables=True):
    conn = sqlite3.connect(":memory:")
    tables = ("news", "staging", "seen", "last_fetched", "pushed_stories")
    for t in (tables if with_all_tables else ("news",)):
        conn.execute(f"CREATE TABLE {t} (ticker TEXT, x TEXT)")
    for ticker in ("HUIZ", "HUIZ", "LX", "MACRO", "HUIZ~SECTOR"):
        conn.execute("INSERT INTO news (ticker) VALUES (?)", (ticker,))
    for table in tables[1:]:
        try:
            conn.execute(f"INSERT INTO {table} (ticker) VALUES ('HUIZ')")
        except sqlite3.Error:
            pass
    conn.commit()
    return conn


print()
print("prune_unconfigured_tickers")
print("-" * 72)
conn = fresh_db()
before = conn.execute("SELECT COUNT(*) FROM news WHERE ticker='HUIZ'").fetchone()[0]
preview = nu.prune_unconfigured_tickers(conn, TICKERS, dry_run=True)
after_preview = conn.execute(
    "SELECT COUNT(*) FROM news WHERE ticker='HUIZ'").fetchone()[0]
check("dry run reports HUIZ rows across every table",
      preview.get("HUIZ", 0) >= 5, str(preview))
check("dry run deletes nothing", after_preview == before, str(after_preview))

removed = nu.prune_unconfigured_tickers(conn, TICKERS)
check("dry run predicted exactly what the real run did", preview == removed,
      f"{preview} != {removed}")
check("HUIZ rows removed from news",
      conn.execute("SELECT COUNT(*) FROM news WHERE ticker LIKE 'HUIZ%'"
                   ).fetchone()[0] == 0)
check("HUIZ removed from every table it touched",
      all(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ticker='HUIZ'"
                       ).fetchone()[0] == 0
          for t in ("news", "staging", "seen", "last_fetched", "pushed_stories")))
check("configured ticker kept",
      conn.execute("SELECT COUNT(*) FROM news WHERE ticker='LX'").fetchone()[0] == 1)
check("MACRO kept", conn.execute(
    "SELECT COUNT(*) FROM news WHERE ticker='MACRO'").fetchone()[0] == 1)
check("return value names the removed ticker(s)",
      set(removed) == {"HUIZ", "HUIZ~SECTOR"}, str(removed))
check("second run is a no-op", nu.prune_unconfigured_tickers(conn, TICKERS) == {})

conn = fresh_db()
check("empty watchlist deletes nothing (a bad config must not wipe the DB)",
      nu.prune_unconfigured_tickers(conn, []) == {}
      and conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 5)

conn = fresh_db(with_all_tables=False)
check("a missing table does not crash the prune",
      nu.prune_unconfigured_tickers(conn, TICKERS).get("HUIZ") == 2)

print()
print("company lookup")
print("-" * 72)
import tempfile

tmp = os.path.join(tempfile.gettempdir(), "pnu_lookup_test.json")
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"HUIZ": {"name_zh": "\u6167\u62e9"}, "LX": {"name_zh": "\u4e50\u4fe1"}}, fh)
real_lookup = nu.COMPANY_LOOKUP_FILE
nu.COMPANY_LOOKUP_FILE = tmp
try:
    check("preview names the removed ticker",
          nu.prune_unconfigured_lookup(TICKERS, dry_run=True) == ["HUIZ"])
    check("dry run leaves the file alone",
          set(json.load(open(tmp, encoding="utf-8"))) == {"HUIZ", "LX"})
    check("real run removes it", nu.prune_unconfigured_lookup(TICKERS) == ["HUIZ"])
    check("configured tickers survive",
          set(json.load(open(tmp, encoding="utf-8"))) == {"LX"})
    check("empty watchlist leaves the lookup alone",
          nu.prune_unconfigured_lookup([]) == [])
finally:
    nu.COMPANY_LOOKUP_FILE = real_lookup
    try:
        os.remove(tmp)
    except OSError:
        pass

print()
print("web payload filter")
print("-" * 72)
items = [{"ticker": "HUIZ", "id": 1}, {"ticker": "LX", "id": 2},
         {"ticker": "MACRO", "id": 3}, {"ticker": "HUIZ~SECTOR", "id": 4}]
kept = web.current_items(items, TICKERS)
check("removed ticker filtered out of the payload",
      [i["id"] for i in kept] == [2, 3], str([i["id"] for i in kept]))
check("no configured list -> no filtering (generic builder stays usable)",
      len(web.current_items(items, [])) == 4)

print()
print("page label: tiers are never (old)")
print("-" * 72)
# Build the real page and run its OWN label functions under node, rather than
# re-implementing the rule here: a test of a copy would pass while the shipped
# page stayed wrong.
page = web.build_index(
    {"items": [], "tickers": ["MACRO", "GLOBAL", "LX", "HUIZ",
                              "LX~SECTOR", "HUIZ~SECTOR"],
     "configured_tickers": ["LX"]}, False)
labels = {}
try:
    start = page.index("function isTierTicker")
    end = page.index("tickers.forEach", start)
    js = ("var configured = ['LX'];\n" + page[start:end] +
          "var all = ['MACRO','GLOBAL','LX','HUIZ','LX~SECTOR','HUIZ~SECTOR'];\n"
          "var out = {}; all.forEach(function(t){ out[t] = isOldHolding(t); });\n"
          "console.log(JSON.stringify(out));\n")
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         timeout=60)
    labels = json.loads(res.stdout.strip().splitlines()[-1])
except Exception as exc:
    print(f"  (node evaluation failed: {exc})")

check("MACRO is never (old)", labels.get("MACRO") is False, str(labels))
check("GLOBAL is never (old)", labels.get("GLOBAL") is False, str(labels))
check("a configured holding is not (old)", labels.get("LX") is False, str(labels))
check("a removed holding is still (old)", labels.get("HUIZ") is True, str(labels))
check("a sector row follows its base ticker",
      labels.get("LX~SECTOR") is False and labels.get("HUIZ~SECTOR") is True,
      str(labels))

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
