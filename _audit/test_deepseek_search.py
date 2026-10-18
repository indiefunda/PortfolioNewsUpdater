#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate for the DeepSeek Search / Fetch source.

Covers the things that cost money or silently lose news:
  * response mapping (summary -> snippet, date -> item, source name)
  * billing counts only NON-EMPTY results; empty searches are free
  * daily/monthly caps, --no-write, HTTP 402 (balance) and 429 handling
  * the delta gate drops undated/stale news (discovery may keep undated)
  * junk hosts and off-company titles are dropped
  * key resolution (dedicated key, DeepSeek AI key, and NOT a Gemini key)
  * query shapes for the daily run, deep search, macro and discovery
  * the source is actually wired into the daily loop (not just imported)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location("nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

TMP = tempfile.mkdtemp(prefix="deepseek_test_")
nu.DEEPSEEK_USAGE_FILE = os.path.join(TMP, "deepseek_usage.json")
nu.NO_WRITE = False
nu._DEEPSEEK_EXHAUSTED = False
nu._CHEAP_CAP_WARNED = False

fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail)) if detail else "")
    if not cond:
        fails.append(name)


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


calls = []
_next = []


def fake_post(url, headers=None, json=None, timeout=None):
    calls.append({"url": url, "headers": headers, "body": json})
    if _next:
        return _next.pop(0)
    return Resp(200, {"results": []})


nu.requests.post = fake_post

CFG = {"ai_provider": "deepseek", "ai_base_url": "https://api.deepseek.com",
       "sources": {"deepseek_search": True, "deepseek_macro": True}}
SEC = {"ai_api_key": "sk-test"}


def now_iso(delta_min=0):
    return (datetime.now(nu.EASTERN) + timedelta(minutes=delta_min)).isoformat()


print("=== key resolution ===")
check("dedicated key wins",
      nu.deepseek_key({"deepseek_api_key": "ds", "ai_api_key": "ai"}, CFG) == "ds")
check("DeepSeek AI key is reused for Search",
      nu.deepseek_key(SEC, CFG) == "sk-test")
check("a Gemini key is NOT used against api.deepseek.com",
      nu.deepseek_key({"ai_api_key": "g"},
                      {"ai_provider": "gemini",
                       "ai_base_url": "https://generativelanguage.googleapis.com"}) == "")
check("a proxy base is not assumed to be DeepSeek",
      nu.deepseek_key(SEC, {"ai_provider": "deepseek",
                            "ai_base_url": "https://proxy.example/v1"}) == "")
check("source toggle is honoured",
      nu.deepseek_enabled(SEC, {"sources": {"deepseek_search": False}}) is False)

print()
print("=== query shapes ===")
since = datetime.now(nu.EASTERN) - timedelta(hours=10)
zh = nu.deepseek_ticker_query({"name_en": "LexinFintech"}, ["乐信", "分期乐"], [],
                              "LX", since)
en = nu.deepseek_ticker_query({"name_en": "Toyota Motor Corporation"}, [],
                              ["Toyota Motor Corporation", "Toyota"], "TM", since)
macro = nu.deepseek_macro_query(since)
disc = nu.deepseek_discovery_query("乐信", "LexinFintech", "LX")
disc_en = nu.deepseek_discovery_query("乐信", "LexinFintech", "LX", overseas=True)
check("Chinese query uses 新闻 + after + lang:zh",
      "乐信" in zh and "新闻" in zh and "after:" in zh and "lang:zh" in zh, zh)
check("English query uses the short derived name",
      en.startswith("Toyota news") and "after:" in en, en)
check("macro query is Chinese and dated",
      "助贷" in macro and "lang:zh" in macro and "after:" in macro, macro)
check("discovery query is Chinese",
      "子公司" in disc and "lang:zh" in disc, disc)
check("overseas discovery query targets subsidiaries",
      "overseas subsidiaries" in disc_en, disc_en)
check("queries stay within the 128-char API limit",
      all(len(q) <= 128 for q in (zh, en, macro, disc, disc_en)))

print()
print("=== response mapping, delta, junk and term filter ===")
_next[:] = [Resp(200, {"results": [
    {"title": "乐信发布Q3财报", "url": "https://finance.example.com/lx-q3",
     "date": now_iso(-30), "summary": "S" * 500, "snippet": "short"},
    {"title": "乐信旧闻", "url": "https://finance.example.com/old",
     "date": now_iso(-60 * 24 * 30), "summary": "old"},
    {"title": "乐信无日期", "url": "https://finance.example.com/undated",
     "date": None, "summary": "undated"},
    {"title": "乐信 on a farm", "url": "https://spam.cyou/lx",
     "date": now_iso(-20), "summary": "junk"},
    {"title": "Heineken buyback", "url": "https://news.example.com/heineken",
     "date": now_iso(-20), "summary": "unrelated"},
]})]
items = nu.fetch_deepseek_search("乐信 新闻 after:2026-01-01 lang:zh", SEC, CFG,
                                 since_dt=since, terms=["乐信"], limit=10)
check("valid result mapped", len(items) == 1, len(items))
if items:
    it = items[0]
    check("source name", it["source"] == "DeepSeekSearch", it["source"])
    check("summary becomes the snippet (trimmed to 400)",
          len(it["snippet"]) == 400 and it["snippet"].startswith("S"), len(it["snippet"]))
    check("date preserved", bool(it["date"]), it["date"])
    check("language detected", it["lang"] == "zh", it["lang"])
usage = nu.deepseek_usage_today()
check("one attempt recorded", usage.get("attempts") == 1, usage)
check("one billable request recorded", usage.get("count") == 1, usage)

print()
print("=== an empty result set is free ===")
before = nu.deepseek_usage_today().get("count", 0)
_next[:] = [Resp(200, {"results": []})]
empty = nu.fetch_deepseek_search("乐信 新闻", SEC, CFG, since_dt=since, terms=["乐信"])
after = nu.deepseek_usage_today()
check("empty search returns []", empty == [], empty)
check("empty search does NOT consume budget",
      after.get("count", 0) == before, after.get("count"))
check("empty search still counts as an attempt",
      after.get("attempts", 0) == usage.get("attempts", 0) + 1, after.get("attempts"))

print()
print("=== discovery may keep undated profile pages ===")
_next[:] = [Resp(200, {"results": [
    {"title": "业务与产品", "url": "https://www.lexin.com/business",
     "date": None, "summary": "乐信旗下分期乐、桔子理财等业务。"},
]})]
prof = nu.fetch_deepseek_search(
    nu.deepseek_discovery_query("乐信", "LexinFintech", "LX"), SEC, CFG,
    since_dt=None)
check("undated result kept for discovery", len(prof) == 1, len(prof))

print()
print("=== Chinese quote/profile pages are junk ===")
for title in ("乐信(LX)_个股概览_股票价格_实时行情_走势图_新闻资讯",
              "乐信(NASDAQ:LX)-公司资料-美股-搜狐",
              "乐信(LX)财报_财务概况_股票财务指标分析"):
    check(f"junk: {title[:34]}", nu.is_junk_content(title), title)
check("a real Chinese headline survives",
      not nu.is_junk_content("乐信发布Q3财报：营收同比增长12%"))

print()
print("=== caps and fail-closed paths ===")
nu._CHEAP_CAP_WARNED = False
cfg_cap = dict(CFG, deepseek_max_daily_searches=1)
_next[:] = [Resp(200, {"results": []})]
calls.clear()
capped = nu.fetch_deepseek_search("乐信 新闻", SEC, cfg_cap, since_dt=since)
check("daily cap stops the request", capped is None and not calls, len(calls))

nu._DEEPSEEK_EXHAUSTED = False
nu.NO_WRITE = True
calls.clear()
dry = nu.fetch_deepseek_search("乐信 新闻", SEC, CFG, since_dt=since)
check("--no-write makes no request", dry is None and not calls, len(calls))
nu.NO_WRITE = False

nu._DEEPSEEK_EXHAUSTED = False
_next[:] = [Resp(402, text="insufficient balance")]
calls.clear()
ex = nu.fetch_deepseek_search("乐信 新闻", SEC, CFG, since_dt=since)
check("402 returns None", ex is None)
check("402 disables further calls this run", nu._DEEPSEEK_EXHAUSTED is True)
calls.clear()
again = nu.fetch_deepseek_search("乐信 新闻", SEC, CFG, since_dt=since)
check("exhausted flag short-circuits without another request",
      again is None and not calls, len(calls))

nu._DEEPSEEK_EXHAUSTED = False
_next[:] = [Resp(429, text="qps")]
check("429 returns None (delta not advanced)",
      nu.fetch_deepseek_search("乐信", SEC, CFG, since_dt=since) is None)

print()
print("=== Fetch API (official site) ===")
nu._DEEPSEEK_EXHAUSTED = False
_next[:] = [Resp(200, {"title": "业务", "content": "# 业务\n分期乐 桔子理财"})]
page = nu.fetch_deepseek_page("https://www.lexin.com/views/in/business.html", SEC, CFG)
check("page content returned", "分期乐" in page, page[:30])
check("fetch recorded", nu.deepseek_usage_today().get("fetch_count") == 1)
check("non-http URL refused", nu.fetch_deepseek_page("ftp://x/y", SEC, CFG) == "")

print()
print("=== wiring ===")
src = open(os.path.join(ROOT, "news_updater.py"), encoding="utf-8").read()
check("daily loop calls DeepSeekSearch", '"DeepSeekSearch"' in src)
check("macro tier calls DeepSeekMacro", '"DeepSeekMacro"' in src)
check("ad-hoc scan records deepseek_searches",
      'cost["deepseek_searches"]' in src)
check("discovery queries DeepSeek", "deepseek_discovery_query(" in src)
check("discovery reads the official site", "fetch_deepseek_page(" in src)
check("--dump-usage exposes deepseek", '"deepseek": deepseek_usage_today()' in src)

example = json.load(open(os.path.join(ROOT, "config_local.example.json"),
                         encoding="utf-8-sig"))
check("example config enables the source",
      example.get("sources", {}).get("deepseek_search") is True)
check("example config carries the budget keys",
      "deepseek_max_daily_searches" in example and
      "deepseek_max_monthly_searches" in example)
check("example config enables the macro query",
      example.get("sources", {}).get("deepseek_macro") is True)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("RESULT: " + ("ALL TESTS PASSED" if not fails else f"{len(fails)} FAILURE(S)"))
sys.exit(1 if fails else 0)
