#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the stale-fiscal-period guard.

Why this exists: on 2026-11-10 the pipeline pushed
    "LexinFintech Holdings Ltd. R (1LFA.HA) Q4 FY2025 earnings call transcript"
because the Yahoo page carried a 2026-11-09 date. Q4 FY2025 ended 31 Dec 2025 -
eleven months old. Every date-based freshness gate passed it; only the headline
told the truth. These checks pin that behaviour, and pin the opposite: a
headline that names NO period must never be dropped by this rule.
"""
import os
import sys
import datetime
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0
NOW = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=nu.EASTERN)


def check(name, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"   [{detail}]" if detail and not ok else ""))


def is_stale(t):
    return nu.stale_fiscal_period(t, now=NOW)


print("stale periods (must be refused)")
print("-" * 72)
check("Q4 FY2025 transcript pushed on 2026-11-10", is_stale(
    "LexinFintech Holdings Ltd. R (1LFA.HA) Q4 FY2025 earnings call transcript"))
check("Q1 2026", is_stale("Qifu Technology Q1 2026 results"))
check("2025 Q4", is_stale("Lufax 2025 Q4 earnings"))
check("FY2025", is_stale("FY2025 annual report"))
check("Third Quarter 2025", is_stale("Lufax Reports Third Quarter 2025 Financial Results"))
check("乐信2025年第四季度财报", is_stale("乐信2025年第四季度财报"))
check("2025年第三季度", is_stale("陆控2025年第三季度业绩"))

print()
print("current periods (must be kept)")
print("-" * 72)
check("Q3 2026", not is_stale("Qfin Holdings Q3 2026 Results"))
check("2026 Q3", not is_stale("2026 Q3 earnings recap"))
check("Q4 2026 (in progress)", not is_stale("Q4 2026 outlook"))
check("第三季度（2026）", not is_stale("陆控第三季度（2026）业绩"))
check("2026年第三季度", not is_stale("乐信2026年第三季度财报"))

print()
print("no period named (must never be dropped by this rule)")
print("-" * 72)
check("plain headline", not is_stale("老虎证券宣布回购"))
check("spelled-out but no year", not is_stale("Lufax Reports Third Quarter Results"))
check("ticker and company only", not is_stale("UP Fintech Faces Securities Lawsuit"))
check("empty", not is_stale(""))
check("fiscal-year mention of the CURRENT year", not is_stale("FY2026 guidance raised"))

print()
print("boundary")
print("-" * 72)
# 150-day cutoff: Q2 2026 ended 30 Jun 2026 -> 133 days before 10 Nov 2026 (kept).
check("Q2 2026 is inside the 150-day window", not is_stale("Q2 2026 results"))
# Q1 2026 ended 31 Mar 2026 -> 224 days (refused).
check("Q1 2026 is outside the window", is_stale("Q1 2026 results"))

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
