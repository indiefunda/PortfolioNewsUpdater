#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the digest's links.

Two complaints drove this:
  1. The digest shows the AI's ENGLISH title but links to the ORIGINAL page. For
     a Chinese source that lands the reader on text they cannot read, so every
     Chinese item now carries a translate link.
  2. "…and N more item(s) stored — see panel Step 5" was a dead end: a count
     with nowhere to go, pointing at a local UI step. It now links to the web
     view when one is configured.
"""
import importlib.util
import sys
import urllib.parse

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


ZH_URL = "https://finance.sina.com.cn/stock/2026-09-11/doc-1.shtml"
EN_URL = "https://www.stocktitan.net/news/YB/1.html"

zh = {"ticker": "LX", "title": "Lexin Q2 results", "title_raw": "乐信第二季度业绩",
      "lang": "zh", "url": ZH_URL, "importance": 8, "reason": "why",
      "impact": "impact", "published_at": "2026-09-11 10:00:00",
      "category": "earnings"}
en = {"ticker": "YB", "title": "Yuanbao results", "title_raw": "Yuanbao results",
      "lang": "en", "url": EN_URL, "importance": 7}
# No `lang` at all, but the raw title is Chinese - must still get a link.
nolang = {"ticker": "HUIZ", "title": "Huize news", "title_raw": "慧择发布公告",
          "url": "https://m.huize.com/a", "importance": 6}
# No `lang`, English raw title - must NOT get a link.
nolang_en = {"ticker": "QFIN", "title": "Qifu news", "title_raw": "Qifu news",
             "url": "https://example.com/q"}

print("=" * 74)
print("TEST 1 - Chinese items get a translate link, English ones do not")
print("=" * 74)
msg = nu.format_digest([zh, en], 2, stored_count=0)
enc = urllib.parse.quote(ZH_URL, safe="")
check("Chinese item has a translate link", f"🔤 English: https://translate.google.com/translate?sl=auto&tl=en&u={enc}" in msg)
check("Chinese item still links the original", ZH_URL in msg)
check("English item has NO translate link", msg.count("🔤 English") == 1)
check("English item still links the original", EN_URL in msg)
check("title shown is the English one", "Lexin Q2 results" in msg)

print()
print("=" * 74)
print("TEST 2 - Chinese detected without a `lang` field")
print("=" * 74)
check("CJK raw title (no lang) gets a link",
      nu.format_digest([nolang], 1).count("🔤 English") == 1)
check("English raw title (no lang) does not",
      nu.format_digest([nolang_en], 1).count("🔤 English") == 0)
check("_item_is_chinese: explicit lang=zh", nu._item_is_chinese({"lang": "zh"}) is True)
check("_item_is_chinese: CJK in raw title",
      nu._item_is_chinese({"title_raw": "中文标题"}) is True)
check("_item_is_chinese: plain English",
      nu._item_is_chinese({"lang": "en", "title_raw": "Hello"}) is False)

print()
print("=" * 74)
print("TEST 3 - the 'stored' line is no longer a dead end")
print("=" * 74)
with_web = nu.format_digest([en], 1, stored_count=7, web_url="https://x.web.app/")
check("links to the web view", "https://x.web.app" in with_web, "")
# NOTE: write `not in`, never `"x" in y is False` - Python chains that into
# `("x" in y) and (y is False)`, which is always False and silently passes.
check("trailing slash stripped from the link",
      "Read them all: https://x.web.app" in with_web
      and "https://x.web.app/" not in with_web)
check("names the count", "7 more stored" in with_web)
check("no stale panel reference", "panel Step 5" not in with_web)

no_web = nu.format_digest([en], 1, stored_count=7)
check("without web_url: plain count, no link", "7 more item(s) stored" in no_web)
check("without web_url: still no panel reference", "Step 5" not in no_web)

check("no stored line when nothing is stored",
      "stored" not in nu.format_digest([en], 1, stored_count=0))

print()
print("=" * 74)
print("TEST 4 - every digest section gets the same treatment")
print("=" * 74)
macro = nu.format_macro([dict(zh, ticker="MACRO")])
sector = nu.format_sector_watch([dict(zh, ticker="LX")])
globalm = nu.format_global_markets([dict(zh, ticker="MACRO")])
for name, text in (("CHINA MACRO", macro), ("SECTOR", sector),
                   ("GLOBAL MARKETS", globalm)):
    check(f"{name} carries a translate link", text and "🔤 English" in text)
    check(f"{name} keeps the original link", text and ZH_URL in text)

print()
print("=" * 74)
print("TEST 5 - items with no URL produce nothing extra")
print("=" * 74)
lines = []
nu.append_source_lines(lines, {"lang": "zh", "title_raw": "中文"})
check("no URL -> no lines", lines == [], str(lines))
lines = []
nu.append_source_lines(lines, {"url": EN_URL, "lang": "en"})
check("English URL -> exactly one line", lines == [f"    {EN_URL}"], str(lines))
lines = []
nu.append_source_lines(lines, {"url": ZH_URL, "lang": "zh"})
check("Chinese URL -> original + translate", len(lines) == 2, str(len(lines)))
check("translate line is indented the same", lines[1].startswith("    🔤"), lines[1][:20])

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
