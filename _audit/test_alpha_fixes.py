#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression suite for the QFIN / LU / LX recall, relevance, staging, and
primary-source fixes."""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

spec = importlib.util.spec_from_file_location("nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0


def check(name, cond, detail=""):
    global fail
    if not cond:
        fail += 1
    print(f"  {'PASS' if cond else 'FAIL'} - {name}"
          + (f" | {detail}" if detail and not cond else ""))


print("=== 1. Bug 1: MACRO_GNEWS_QUERY uses OR + when:7d ===")
check("MACRO_GNEWS_QUERY uses OR (not 9 AND-joined keywords)",
      " OR " in nu.MACRO_GNEWS_QUERY and "when:" in nu.MACRO_GNEWS_QUERY,
      nu.MACRO_GNEWS_QUERY)

print("\n=== 2. Bug 2: date-only after-hours SEC 6-K not dropped by 17:00 ET delta ===")
since_1700 = datetime(2026, 11, 23, 17, 0, 0, tzinfo=nu.EASTERN)
pub_same_day = nu._parse_pub("2026-11-23")
pub_prev_day = nu._parse_pub("2026-11-22")
check("same-day date-only '2026-11-23' survives 17:00 ET since_dt",
      not nu._pub_before_since("2026-11-23", pub_same_day, since_1700))
check("previous-day date-only '2026-11-22' IS dropped by 2026-11-23 since_dt",
      nu._pub_before_since("2026-11-22", pub_prev_day, since_1700))
exact_early = nu._parse_pub("2026-11-23 12:00:00")
check("exact timestamp 12:00:00 IS still dropped by 17:00 ET since_dt",
      nu._pub_before_since("2026-11-23 12:00:00", exact_early, since_1700))

print("\n=== 3. Bug 3: 21-day rolling corporate/HK items survive 96h ingest & 72h push gates ===")
tmp_dir = tempfile.mkdtemp(prefix="alpha_fix_")
db_path = os.path.join(tmp_dir, "test.db")
nu.DB_FILE = db_path
nu.NO_WRITE = False
conn = nu._db()
now = datetime.now(nu.EASTERN)
ten_days_ago = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
corp_item = {
    "source": "DeepSeekCorp",
    "ticker": "LU",
    "id": "https://example.com/lu-split",
    "title": "陆金所控股宣布10合1合股及ADS比例变更",
    "url": "https://example.com/lu-split",
    "date": ten_days_ago,
    "lang": "zh",
    "snippet": "陆金所宣布10合1合股",
    "_max_age_hours": 22 * 24,
    "_max_push_age_hours": 21 * 24,
}
lu_meta = {"ticker": "LU", "name_zh": "陆金所", "name_en": "Lufax Holding Ltd",
           "aliases_zh": ["陆控"], "keywords": ["平安普惠"]}
all_new = []
rep = {}
accepted = nu.ingest_items(
    conn, all_new, "DeepSeekCorp", [corp_item], "LU",
    since_dt=now - timedelta(days=21), max_age_hours=96,
    relevance_resolved=nu.resolve_relevance(lu_meta),
    run_start=now.strftime("%Y-%m-%d %H:%M:%S"), report=rep)
check("10-day-old DeepSeekCorp item accepted despite 96h default max_age_hours",
      accepted == 1 and len(all_new) == 1, f"accepted={accepted}, rep={rep}")
if all_new:
    all_new[0].update({"importance": 8, "push": True, "is_dup": False,
                       "is_known": False, "relevant": True})
    pushed = nu.select_push_items(all_new, {"push_mode": "all", "push_min_importance": 4,
                                            "push_max_age_hours": 72}, conn=conn, now=now)
    check("10-day-old DeepSeekCorp item survives 72h push gate via _max_push_age_hours",
          len(pushed) == 1, nu.push_decision_counts(all_new))

print("\n=== 4. Bug 4: case-insensitive English matching in _term_in_text & tavily_term_match ===")
terms = ["奇富科技", "Qifu Technology", "Qfin Holdings", "Qfin", "Lufax", "LexinFintech"]
for hl in (
    "QFIN Holdings Announces Q3 2026 Financial Results",
    "LUFAX Reports Third Quarter Earnings",
    "LEXINFINTECH Declares Cash Dividend",
):
    check(f"tavily_term_match accepts uppercase headline: {hl[:35]}",
          nu.tavily_term_match(hl, terms))
check("_term_in_text respects word boundaries (Qfin != faqfinder)",
      not nu._term_in_text("Qfin", "faqfinder tool released"))

print("\n=== 5. Bug 5: discovered keywords + compound brand stems accepted by passes_relevance ===")
check("discovered keyword 平安普惠 passes relevance for LU",
      nu.passes_relevance(
          {"ticker": "LU", "title": "平安普惠宣布推出小微融资新方案", "snippet": ""},
          lu_meta, nu.resolve_relevance(lu_meta)) == "company")
lx_meta = {"ticker": "LX", "name_zh": "乐信", "name_en": "LexinFintech Holdings Ltd."}
check("compound stem Lexin from LexinFintech passes relevance",
      nu.passes_relevance(
          {"ticker": "LX", "title": "Lexin posts Q3 results", "snippet": ""},
          lx_meta, nu.resolve_relevance(lx_meta)) == "company")
check("LU reverse split & ADS ratio pass relevance",
      nu.passes_relevance(
          {"ticker": "LU", "title": "LU reverse split takes effect next week", "snippet": ""},
          lu_meta, nu.resolve_relevance(lu_meta)) == "company"
      and nu.passes_relevance(
          {"ticker": "LU", "title": "LU to change ADS ratio", "snippet": ""},
          lu_meta, nu.resolve_relevance(lu_meta)) == "company")

print("\n=== 6. Bug 9: 2-letter ticker homonyms (LX Group, surname Lu) rejected ===")
for tk, meta, bad in (
    ("LX", lx_meta, "LX Semicon reports Q3 profit jump"),
    ("LX", lx_meta, "LX Pantos wins major earnings award"),
    ("LX", lx_meta, "LX International posts higher revenue"),
    ("LX", lx_meta, "LX Hausys announces CEO appointment"),
    ("LU", lu_meta, "Lu Zhiqiang resigns as chairman of Oceanwide"),
):
    v = nu.passes_relevance({"ticker": tk, "title": bad, "snippet": ""},
                            meta, nu.resolve_relevance(meta))
    check(f"homonym rejected [{tk}]: {bad[:42]}", v != "company", v)

print("\n=== 7. Bug 10: wordlist comments stripped & LLP junk pattern works ===")
check("'#' not in _GENERIC_ALIAS_EXTRA", "#" not in nu._GENERIC_ALIAS_EXTRA)
check("'given' not in _GENERIC_ALIAS_EXTRA", "given" not in nu._GENERIC_ALIAS_EXTRA)
check("'#' not in PROSE_COLLISION_WORDS", "#" not in nu.PROSE_COLLISION_WORDS)
check("'not' not in PROSE_COLLISION_WORDS", "not" not in nu.PROSE_COLLISION_WORDS)
check("is_junk_content catches 'Robbins LLP Files Suit Against QFIN'",
      nu.is_junk_content("Robbins LLP Files Suit Against QFIN"))

print("\n=== 8. Bug 7a & 7b: _event_groups per-ticker keying & scoped promote_run ===")
batch = [
    {"ticker": "QFIN", "_number": 1, "title": "QFIN earnings beat",
     "is_dup": False, "is_known": False, "relevant": True},
    {"ticker": "QFIN", "_number": 2, "_dup_of": 1, "title": "QFIN Q3 results copy",
     "is_dup": True, "is_known": False, "source": "Tavily", "relevant": True},
    {"ticker": "LU", "_number": 1, "title": "Lufax reverse split",
     "is_dup": False, "is_known": False, "relevant": True},
    {"ticker": "LX", "_number": 1, "title": "LX Semicon profit",
     "is_dup": False, "is_known": False, "relevant": False},
]
canon, dropped = nu._event_groups(batch)
by_tk = {c["ticker"]: c for c in canon}
check("QFIN duplicate attached to QFIN (not LU)",
      by_tk["QFIN"]["sources_count"] == 2 and by_tk["LU"]["sources_count"] == 1,
      str([(c["ticker"], c["sources_count"], c.get("also_seen")) for c in canon]))
check("AI relevant=False item excluded from canonical rows",
      "LX" not in by_tk and any(d.get("_drop_reason") == "ai_irrelevant" for d in dropped))

# Staging isolation: promoting MACRO must NOT mark QFIN's staged row as promoted=1
run_id = "2026-11-23 09:15:00"
qfin_stage = {"ticker": "QFIN", "source": "SEC", "id": "acc1", "title": "QFIN 6-K",
              "url": "https://sec.gov/1", "lang": "en", "published_at": run_id,
              "_run_id": run_id}
macro_stage = {"ticker": "MACRO", "source": "Eastmoney724", "id": "m1", "title": "央行降准",
               "url": "https://em.com/1", "lang": "zh", "published_at": run_id,
               "_run_id": run_id, "relevant": True}
nu.stage_item(conn, qfin_stage)
nu.stage_item(conn, macro_stage)
nu.promote_run(conn, [macro_stage], run_id=run_id, tickers=["MACRO"])
qfin_promoted = conn.execute(
    "SELECT promoted FROM staging WHERE id=?", (qfin_stage["_staging_id"],)).fetchone()[0]
macro_promoted = conn.execute(
    "SELECT promoted FROM staging WHERE id=?", (macro_stage["_staging_id"],)).fetchone()[0]
check("macro promote_run marks MACRO promoted=1 while leaving QFIN promoted=0",
      macro_promoted == 1 and qfin_promoted == 0,
      f"macro={macro_promoted}, qfin={qfin_promoted}")

print("\n=== 9. Bug 8: relevant=False blocked in select_push_items ===")
irrel_item = {"ticker": "LX", "source": "GoogleNews", "title": "LX Semicon fined $1M",
              "published_at": now.strftime("%Y-%m-%d %H:%M:%S"), "importance": 9,
              "push": True, "relevant": False}
pushed_irrel = nu.select_push_items([irrel_item], {"push_mode": "all"}, conn=None, now=now)
check("relevant=False item is never pushed even with regulatory keyword",
      len(pushed_irrel) == 0, str(pushed_irrel))

print("\n=== 10. New primary sources: HKEX auto-code & primary event ===")
check("KNOWN_HK_CODES maps QFIN->03660 and LU->06623",
      nu.deepseek_hk_code({}, "QFIN") == "03660" and nu.deepseek_hk_code({}, "LU") == "06623")
check("HKEX filing is recognized as primary event and company-relevant",
      nu.is_primary_event({"source": "HKEX", "title": "陸控 (06623.HK): 內幕消息"})
      and nu.passes_relevance(
          {"ticker": "LU", "source": "HKEX", "title": "董事會會議日期", "snippet": ""},
          lu_meta, nu.resolve_relevance(lu_meta)) == "company")

print("\n=== 11. Live-run catches: law-firm spam, quote templates, and strict AI fail-closed ===")
for junk_hl in (
    "QFIN Investors Have Opportunity to Lead Qfin Holdings, Inc. Securities Fraud Lawsuit with SBS Law - GlobeNewswire",
    "QFIN STOCKHOLDER REMINDER: Qfin Holdings, Inc. Investors - GlobeNewswire",
    "陆控(LU)_陆控怎么样 - 热门讨论 - 雪球",
    "Lufax Holding Ltd (0VU2.F) - Yahoo! Finance Canada",
    "Lufax Holding Ltd (0VU2.DU) Income Statement - Yahoo Finance",
    "Lufax (NYSE:LU) Hits New 12-Month Low - Time to Sell? - MarketBeat",
):
    check(f"is_junk_content rejects live spam/template: {junk_hl[:44]}",
          nu.is_junk_content(junk_hl))

no_ai_item = {"ticker": "QFIN", "source": "GoogleNews",
              "title": "Qfin fined for fraud in test headline",
              "published_at": now.strftime("%Y-%m-%d %H:%M:%S"),
              "importance": 1, "push": False, "_ai_unavailable": True}
check("fail-closed _ai_unavailable item is NEVER pushed even if regulatory keyword hits",
      len(nu.select_push_items([no_ai_item], {"push_mode": "all"}, conn=None, now=now)) == 0)

conn.close()
print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
