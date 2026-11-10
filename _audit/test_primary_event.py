#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify that primary-source events are exempt from the model's push veto.

Why this exists: on 2026-11-09 the pipeline stored and then refused to send a
QFIN SC 13G disclosing a 5.2% Goldman stake, three YB Form 4s (VP, Director,
COO) and an LU 6-K. The model's reason was "does not change company
fundamentals" - the wrong test for a company this size, where those filings are
the news. These checks pin both halves: facts are never vetoed, and genuine
commentary still is.
"""
import os
import sys
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0


def check(name, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"   [{detail}]" if detail and not ok else ""))


def ev(title, source="GoogleNews"):
    return nu.is_primary_event({"title": title, "source": source})


print("primary events (must be exempt from the veto)")
print("-" * 72)
check("QFIN Goldman stake", ev("奇富科技：高盛持股1260.38万股，占比5.2%", "Guba"))
check("SC 13G filing", ev("Qfin Holdings, Inc. - SCHEDULE 13G filed 2026-11-09", "SEC"))
check("13D", ev("SC 13D/A filed by Lufax Holding"))
check("Form 4 COO", ev("Form 4 Yuanbao Inc.: Ying Dana Li (Director, COO)", "Tavily"))
check("6-K", ev("Lufax Holding Ltd - 6-K: 0001193125-26-452772", "SEC"))
check("8-K", ev("Qifu Technology, Inc. 8-K"))
check("Chinese penalty", ev("乐信融担及时任责任人违规被罚", "DeepSeekSearch"))
check("buyback", ev("奇富科技宣布3亿美元回购计划"))
check("insider increase", ev("老虎证券董事增持5万股"))
check("any SEC item", ev("Some filing with no keywords", "SEC"))

print()
print("commentary / PR (the veto still applies)")
print("-" * 72)
check("dividend opinion piece", not ev("LexinFintech: Its ~25% Dividend Yield Is the Warning"))
check("PR event attendance", not ev("元保CEO方锐出席太保答谢会", "DeepSeekSearch"))
check("monthly return form", not ev("陆控(06623)月报表截至二零二六年十月三十一日"))
check("price chart", not ev("LexinFintech Holdings Ltd. R (1LFA.HA) Seasonal returns chart"))
check("analyst rating", not ev("UP Fintech: Regulatory Risk And Likely 3Q26 Weakness Leads To A Sell Rating"))
check("empty title", not ev(""))

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
