#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the new relevance + dedup gates against the REAL 10-day corpus."""
import importlib.util
import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

BASE = r"F:\MyRepository\PortfolioNewsUpdater\_audit"
try:
    lookup = json.load(open(BASE + r"\company_lookup.json", encoding="utf-8"))
except FileNotFoundError:
    # The relevance cases below are driven by the REAL discovered lookup for
    # this portfolio, which is private data and deliberately not committed.
    # Skip on a fresh clone instead of crashing - the generic gates are covered
    # by the other suites.
    print("SKIPPED - needs the local private fixture _audit/company_lookup.json")
    sys.exit(0)
conn = sqlite3.connect(BASE + r"\news.db")
conn.row_factory = sqlite3.Row

print("=" * 78)
print("TEST 1 - relevance gate: is the off-company noise now rejected?")
print("=" * 78)
MUST_DROP = [
    ("HUIZ", "Strong El Niño is redrawing the insurance industry's risk map"),
    ("YB", "PICC: Accelerate Non-Auto Insurance Governance and Dividend Insurance Transformation"),
    ("YB", "Bancassurance Premium Growth Slows for Five Listed Insurers; Where Does Competition Go After Do"),
    ("LX", "After Xu Jiayin's Verdict, the State Moves: Completed Homes Are Good, But the Costs Behind Are"),
    ("LX", "PBOC Announcement: Payment Licenses Under Two Listed Companies Suspended From Review, Emergency"),
    ("LU", "Unified Asset-Management Product Disclosure Nears Countdown: How Can a 100-Trillion-Yuan Market Ach"),
    ("QFIN", "When 'building robots' is no longer scarce"),
    ("QFIN", "Meta's Acquisition of Manus Withdrawn; Founding Team Regains Control"),
    ("CAAS", "Zhongjian Technology suspected of information disclosure fraud in Huawei cooperation"),
    ("HUIZ", "828 Real-Estate Policy Deduction: From Awareness to Pain, Then AI Remodeling"),
]
fail = 0
for ticker, title in MUST_DROP:
    meta = lookup.get(ticker, {})
    resolved = nu.resolve_relevance(meta)
    it = {"ticker": ticker, "title": title, "snippet": ""}
    verdict = nu.passes_relevance(it, meta, resolved)
    # With sector_watch OFF (the default) both 'unrelated' and 'sector' are
    # dropped - only 'company' reaches the digest.
    dropped = verdict != "company"
    if not dropped:
        fail += 1
    print(f"  [{'PASS' if dropped else 'FAIL'}] {ticker:5s} {verdict:9s} "
          f"{'dropped' if dropped else 'KEPT!'}  {title[:56]}")

print()
print("=" * 78)
print("TEST 2 - relevance gate: real company mentions must still pass")
print("=" * 78)
MUST_KEEP = [
    ("HUIZ", "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion"),
    ("HUIZ", "慧择保险 2026 年第二季度业绩"),
    ("YB", "元保Q1狂撒6.3亿激进获客 「魔方业务」成收割老年人重灾区"),
    ("LX", "净利暴跌 80%！乐信分期电商业务暴增60%背后"),
    ("LX", "分期乐 被罚款"),
    ("QFIN", "奇富科技净利下滑超60%"),
    ("LU", "Lufax Sets October EGM to Extend Convertible Notes"),
    ("LU", "陆金所控股（LU）推进港股复牌"),
    ("CAAS", "China Automotive Systems Reports Q2 2026 Results"),
    ("CAAS", "中汽系统 发布财报"),
]
for ticker, title in MUST_KEEP:
    meta = lookup.get(ticker, {})
    resolved = nu.resolve_relevance(meta)
    it = {"ticker": ticker, "title": title, "snippet": ""}
    verdict = nu.passes_relevance(it, meta, resolved)
    ok = verdict == "company"
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {ticker:5s} {verdict:9s} {title[:62]}")

print()
print("=" * 78)
print("TEST 3 - same-story dedup on the REAL duplicate clusters")
print("=" * 78)
CASES = [
    ("HUIZ earnings x4 (real 9/10 run)", [
        ("HUIZ", "GoogleNewsZH", "Huize (HUIZ) Q2 2026 Earnings Call: H1 Total Written Premium Hits Record RMB 4.2 Billion"),
        ("HUIZ", "Tavily", "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion"),
        ("HUIZ", "GoogleNews", "Huize (HUIZ) Q2 2026 Earnings Call: H1 GWP Hits Record RMB 4.2 Billion - TradingKey"),
        ("HUIZ", "Exa", "Huize (HUIZ) Q2 2026 Earnings Call: H1 Total Written Premium Hits Record RMB 4.2 Billion"),
    ]),
    ("YB Q2 release x4", [
        ("YB", "Tavily", "Yuanbao Inc. Posts Strong Q2 2026 Growth and Expands AI-Driven Inclusive Health Insurance"),
        ("YB", "Tavily", "Yuanbao Releases Q2 2026 Financial Results"),
        ("YB", "GoogleNews", "Yuanbao Releases Q2 2026 Financial Results - AlphaStreet"),
        ("YB", "GoogleNews", "Net income jumps 36% at Yuanbao Inc. (YB) in Q2 2026 - Stock Titan"),
    ]),
    ("HUIZ insurance law x2 (cross-ticker, SAME story)", [
        ("HUIZ", "Exa", "NFRA seeks public comment on draft amendment to the PRC Insurance Law"),
        ("HUIZ", "Exa", "国家金融监督管理总局就《中华人民共和国保险法（修订草案征求意见稿）》公开征求意见"),
    ]),
]
for label, rows in CASES:
    items = [{"ticker": t, "source": s, "title": ti, "url": f"https://x/{i}",
              "lang": "zh" if nu.is_chinese(ti) else "en"}
             for i, (t, s, ti) in enumerate(rows)]
    kept, collapsed = nu.dedupe_same_story(items)
    print(f"  {label}: {len(rows)} in -> {len(kept)} kept, {collapsed} collapsed")
    for k in kept:
        print(f"      KEPT: [{k['source']:11s}] {k['title'][:70]}")

print()
print("=" * 78)
print("TEST 4 - two genuinely DIFFERENT stories must NOT collapse")
print("=" * 78)
DISTINCT = [
    ("LX", "Tavily", "LexinFintech Holdings Ltd.: Citigroup lowers rating to Neutral"),
    ("LX", "GoogleNewsZH", "净利暴跌 80%！乐信分期电商业务暴增60%背后：买吖卡捆绑销售深陷套路贷争议"),
    ("LX", "Tavily", "Analysts Just Shaved Their LexinFintech Holdings Ltd. Forecasts Dramatically"),
    ("LX", "Exa", "Regulators draw compliance red lines for financial online marketing"),
]
items = [{"ticker": t, "source": s, "title": ti, "url": f"https://y/{i}",
          "lang": "zh" if nu.is_chinese(ti) else "en"}
         for i, (t, s, ti) in enumerate(DISTINCT)]
kept, collapsed = nu.dedupe_same_story(items)
print(f"  {len(DISTINCT)} distinct LX stories -> {len(kept)} kept, {collapsed} collapsed")
ok = len(kept) == len(DISTINCT)
if not ok:
    fail += 1
print(f"  [{'PASS' if ok else 'FAIL'}] distinct stories preserved")

print()
print("=" * 78)
print("TEST 5 - macro gate: Fed/US items must leave CHINA MACRO")
print("=" * 78)
MACRO_CASES = [
    ("美联储理事沃勒：如果通胀数据偏热 我会考虑支持加息", False, "Fed/Waller"),
    ("交易员在美联储理事沃勒“鸽派”表态后降低了美联储加息押注", False, "Fed bets"),
    ("8月非农就业远超预期 现货白银跌幅扩大至3%", False, "US payrolls"),
    ("【ADP数据公布后，美联储9月维持利率不变的概率为37.8%】", False, "ADP/CME"),
    ("【美股盘前要闻速递】美股三大股指期货齐跌", False, "US premarket"),
    ("央行：继续实施好适度宽松的货币政策 完善利率体系", True, "PBoC policy"),
    ("六大行下调存款利率 降息预期升温", True, "rate cut"),
    ("助贷新规落地 消费金融公司承压", True, "assisted-loan reg"),
    ("财政部将发行3000亿元特别国债", True, "special T-bonds"),
]
for text, want, label in MACRO_CASES:
    got = nu.is_macro(text)
    ok = got == want
    if not ok:
        fail += 1
    gl = nu.is_global_markets(text)
    print(f"  [{'PASS' if ok else 'FAIL'}] macro={str(got):5s} global={str(gl):5s} {label:20s} {text[:44]}")

print()
print("=" * 78)
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print("=" * 78)
# Without this the script printed a verdict but always exited 0, so a failing
# gate looked like a pass to anything that checked the return code.
sys.exit(1 if fail else 0)
