#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Integration test: do the FETCHERS actually drop junk-domain results?

test_junk_domains.py proves is_junk_host() classifies correctly. This proves the
filter is wired into fetch_exa() and fetch_tavily() - a unit test on the
predicate would still pass if nobody ever called it.

The paid HTTP calls are replaced with canned responses, so this needs no keys,
no network and no credits.
"""
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

# A run that is allowed to "spend" credits, but with persistence neutered.
nu.NO_WRITE = False
nu._write_json = lambda *a, **k: None
nu.exa_usage_today = lambda: {"count": 0, "month_count": 0}
nu.tavily_usage_today = lambda: {"count": 0, "month_count": 0}

GOOD_URL = "https://finance.sina.com.cn/stock/2026-09-12/doc-1.shtml"
JUNK_URLS = [
    "http://constanta.fmufjl.cyou/headline/2026.html",
    "http://meizhou.qrlwh.com/a.html",
    "http://novara.whatswebap.com/a.html",
]


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def fake_post(payload_for):
    """Return a requests.post stand-in that replies with the given payload."""
    def _post(url, *a, **k):
        return FakeResp(payload_for)
    return _post


fail = 0

# ---------------------------------------------------------------- EXA -----
exa_payload = {"results": [
    {"title": "Real Huize earnings story", "url": GOOD_URL,
     "publishedDate": "2026-09-12T10:00:00Z"},
    *[{"title": f"Stuffed keyword farm {i}", "url": u,
       "publishedDate": "2026-09-12T10:00:00Z"} for i, u in enumerate(JUNK_URLS)],
]}
nu.requests.post = fake_post(exa_payload)
got = nu.fetch_exa("huize news", {"exa_api_key": "fake"}, {}, since_dt=None)
urls = [i["url"] for i in (got or [])]
print("=" * 74)
print("EXA fetch")
print(f"  returned {len(urls)} item(s): {urls}")
ok = urls == [GOOD_URL]
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] junk dropped, real result kept")

# ------------------------------------------------------------- Tavily -----
tavily_payload = {"results": [
    {"title": "Real Huize earnings story", "url": GOOD_URL,
     "content": "Huize reported results", "published_date": "2026-09-12T10:00:00Z"},
    *[{"title": f"Stuffed keyword farm {i}", "url": u,
       "content": "Huize", "published_date": "2026-09-12T10:00:00Z"}
      for i, u in enumerate(JUNK_URLS)],
]}
nu.requests.post = fake_post(tavily_payload)
got = nu.fetch_tavily("huize news", {"tavily_api_key": "fake"}, {}, since_dt=None)
urls = [i["url"] for i in (got or [])]
print()
print("Tavily fetch")
print(f"  returned {len(urls)} item(s): {urls}")
ok = urls == [GOOD_URL]
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] junk dropped, real result kept")

# ------------------------------------------------- insert_news safety net --
print()
print("insert_news safety net (covers RSS / Google News / wires)")
import sqlite3  # noqa: E402

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE news (id INTEGER PRIMARY KEY, ticker TEXT, source TEXT,"
             " lang TEXT, item_hash TEXT, title_raw TEXT, url TEXT, snippet TEXT,"
             " published_at TEXT, first_seen TEXT)")
nu.NO_WRITE = False
before = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
nu.insert_news(conn, {"ticker": "HUIZ", "source": "RSS", "title": "farm",
                      "url": JUNK_URLS[0]})
after = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
ok = before == after == 0
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] junk URL NOT stored ({before} -> {after})")

good_item = {"ticker": "HUIZ", "source": "RSS", "title": "real",
             "url": GOOD_URL, "id": GOOD_URL}
nu.insert_news(conn, good_item)
after2 = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
ok = after2 == 1
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] real URL still stored ({after} -> {after2})")
conn.close()

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
