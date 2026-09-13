#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the junk-domain gate against REAL hostnames.

The MUST_DROP list is every junk host actually found in the live VM database
(145 of 1214 rows). The MUST_KEEP list is every legitimate host from that same
database - a false positive here silently deletes a real story, which is worse
than letting one spam page through, so both directions are asserted.
"""
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

# --- observed content farms / disposable spam hosts (live DB) --------------
MUST_DROP = [
    "http://constanta.fmufjl.cyou/headline/20260823-31b69499274.html",
    "http://jingmen.visualstudio-cn.top/focus/2026/08/23/08c36199630.html",
    "http://tangier.qepzdeoelpuaftzo.top/x/1.html",
    "http://oakville.1ejcv.icu/a.html",
    "http://badajoz.hlpsrf.cyou/a.html",
    "http://fontainebleau.aljmpzskihrj.top/a.html",
    "http://izmir.kmwrnzzvcmuyepx.top/a.html",
    "http://plzen.illgiwnysvcv.top/a.html",
    "http://gwangju.jwejse.cyou/a.html",
    "http://haixizhou.99gjbbcd.cyou/a.html",
    "http://hochiminhcity.notepad-im.top/a.html",
    "http://chambery.dy-qishui.top/a.html",
    "http://tianjin.srigwy.cyou/a.html",
    "http://dali.1c2m7p.icu/a.html",
    "http://saitama.ht3mkn.icu/a.html",
    "http://jerseycity.myp9qm.icu/a.html",
    "http://groningen.yzdr56.icu/a.html",
    "http://madison.crexhn.cyou/a.html",
    "http://watford.ht3mkn.icu/a.html",
    "http://orebro.qrgx8g.icu/a.html",
    "http://houston.deepl.cyou/a.html",
    "http://haibei.notepadd.top/a.html",
    "http://novara.whatswebap.com/a.html",
    "http://elche.baixi.net/a.html",
    "http://durban.pc-kakaotalk.com.cn/a.html",
    "http://liupanshui.world-cn.cn/a.html",
    "http://archive.mtsoln.com/a.html",
    # pushed to Telegram via ExaMacro - these reached the user
    "http://meizhou.qrlwh.com/a.html",
    "http://www.fuhuikjjt.cn/a.html",
    "http://www.caifudd.cn/a.html",
    "http://www.4ke.cn/a.html",
]

# --- real outlets from the same DB (must survive) -------------------------
MUST_KEEP = [
    "https://finance.sina.com.cn/stock/2026-09-10/doc-1.shtml",
    "https://news.google.com/rss/articles/CBMi-wFBVV95cUxNR0RQ?oc=5",
    "https://www.163.com/money/article/1.html",
    "https://finance.yahoo.com/news/huize-1.html",
    "https://www.0xzx.com/2026091001.html",
    "https://www.sina.cn/news/1.html",
    "https://finance.eastmoney.com/a/1.html",
    "https://m.huize.com/news/1.html",
    "https://simplywall.st/stocks/us/1",
    "https://news.10jqka.com.cn/20260910/1.shtml",
    "https://www.sec.gov/Archives/edgar/data/1",
    "https://m.21jingji.com/article/1.html",
    "https://www.tipranks.com/news/1",
    "https://www.stocktitan.net/news/YB/1.html",
    "https://www.fx168news.com/article/1",
    "https://businesstimescn.com/1.html",
    "https://www.ntdtv.com/gb/2026/09/10/1.html",
    "https://news.alphastreet.com/1",
    "https://www.tradingkey.com/news/1",
    "https://www.moomoo.com/news/1",
    "https://www.reuters.com/markets/1",
    "https://www.bloomberg.com/news/1",
    "https://www.ft.com/content/1",
    "https://www.wsj.com/articles/1",
    "https://www.caixin.com/2026-09-10/1.html",
    "https://www.yicai.com/news/1.html",
    "https://www.stcn.com/article/1.html",
    "https://www.cnstock.com/1.html",
    "https://www.jiemian.com/article/1.html",
    "https://www.thepaper.cn/newsDetail_forward_1",
    "https://www.zhitongcaijing.com/content/1.html",
    "https://xueqiu.com/1/1",
    "https://www.gelonghui.com/p/1",
]

fail = 0
print("=" * 78)
print("TEST 1 - content farms must be rejected")
print("=" * 78)
for u in MUST_DROP:
    got = nu.is_junk_host(u)
    if not got:
        fail += 1
    print(f"  [{'DROP' if got else 'MISS'}] {nu._host_of(u)}")
print(f"  -> {len(MUST_DROP) - fail}/{len(MUST_DROP)} rejected")

print()
print("=" * 78)
print("TEST 2 - real outlets must survive (false positives delete real news)")
print("=" * 78)
fp = 0
for u in MUST_KEEP:
    got = nu.is_junk_host(u)
    if got:
        fp += 1
    print(f"  [{'*** FALSE POSITIVE ***' if got else 'keep'}] {nu._host_of(u)}")
print(f"  -> {len(MUST_KEEP) - fp}/{len(MUST_KEEP)} kept")

print()
print("=" * 78)
print("TEST 3 - edge cases")
print("=" * 78)
EDGE = [
    ("", False, "empty url"),
    ("not a url", False, "no scheme, no dot"),
    ("localhost", False, "bare host, no dot"),
    ("https://example.com/x", False, "ordinary .com"),
    ("https://www.visualstudio-cn.top.evil.com/x", False,
     "junk SLD as a SUBDOMAIN of a real .com - must NOT match"),
    ("HTTPS://CONSTANTA.FMUFJL.CYOU/A", True, "uppercase scheme+host"),
    ("http://constanta.fmufjl.cyou:8080/a?b=1#c", True, "port + query + fragment"),
    ("http://user:pw@constanta.fmufjl.cyou/a", True, "credentials in authority"),
    ("http://spam.top/a", True, "word label on an always-drop TLD (.top)"),
    ("http://realnews.info/a", False, "word label on a SUSPECT TLD - corroboration fails, kept"),
    ("http://xkjhqzp.info/a", True, "machine label on a SUSPECT TLD - dropped"),
    # Scheme-less input. urlparse() alone parses these as a PATH, so .hostname
    # is None - which made the purge command match nothing at all.
    ("constanta.fmufjl.cyou", True, "scheme-less junk host"),
    ("constanta.fmufjl.cyou/path/to/article.html", True, "scheme-less junk host + path"),
    ("www.whatswebap.com", True, "scheme-less known junk SLD"),
    ("finance.sina.com.cn/stock/1.shtml", False, "scheme-less real host + path"),
    ("reuters.com", False, "scheme-less real host"),
    ("constanta.fmufjl.cyou:8080/a", True, "scheme-less with port"),
]
for u, want, why in EDGE:
    got = nu.is_junk_host(u)
    ok = got == want
    if not ok:
        fail += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] want={str(want):5} got={str(got):5}  {why}")

print()
print("=" * 78)
print("TEST 4 - run the gate over the ENTIRE real corpus (_audit/news.db)")
print("=" * 78)
import sqlite3  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_CORPUS = _Path(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
if not _CORPUS.exists():
    print("  SKIPPED - needs the local private fixture _audit/news.db")
    _urls = []
else:
    _conn = sqlite3.connect(str(_CORPUS))
    _urls = [r[0] for r in _conn.execute("SELECT url FROM news WHERE url != ''")]
    _conn.close()

flagged = Counter()
for _u in _urls:
    if nu.is_junk_host(_u):
        flagged[nu._host_of(_u)] += 1

total_flagged = sum(flagged.values())
print(f"  rows with a URL          : {len(_urls)}")
print(f"  rows the gate would drop : {total_flagged} "
      f"({100 * total_flagged / max(1, len(_urls)):.1f}%)")
for _h, _n in flagged.most_common():
    print(f"    {_h:44} {_n}")

# No host that we know is a real outlet may appear in the flagged list.
_bad = [h for h in flagged if any(h == k or h.endswith("." + k) for k in
        ("sina.com.cn", "eastmoney.com", "google.com", "sec.gov", "huize.com",
         "163.com", "yahoo.com", "tipranks.com", "stocktitan.net", "moomoo.com",
         "tradingkey.com", "reuters.com", "caixin.com", "yicai.com", "stcn.com",
         "cnstock.com", "jiemian.com", "thepaper.cn", "xueqiu.com",
         "gelonghui.com", "10jqka.com.cn", "fx168news.com", "simplywall.st",
         "alphastreet.com", "21jingji.com", "ntdtv.com", "businesstimescn.com",
         "zhitongcaijing.com", "0xzx.com"))]
if _bad:
    fp += len(_bad)
    print(f"  *** FALSE POSITIVES on real outlets: {_bad} ***")
else:
    print("  no real outlet flagged  : OK")
print(f"  -> gate is selective: {total_flagged} of {len(_urls)} rows "
      f"({100 * (len(_urls) - total_flagged) / max(1, len(_urls)):.1f}% kept)")

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 and fp == 0 else f'{fail + fp} FAILURE(S)'}"
      f"  (drop-misses={fail}, false-positives={fp})")
sys.exit(1 if (fail or fp) else 0)
