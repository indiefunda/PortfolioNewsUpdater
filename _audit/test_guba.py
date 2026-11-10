#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the 股吧 (guba) fetcher.

Why this source exists: the English feed had Lufax's Q3 as a bare 6-K, while the
Chinese board carried the actual numbers ("陆控三季度总营收61.8亿", "第三季亏损
扩大至15.35亿元"). The checks below pin the two properties that make it usable:

  * it parses the board's embedded `var article_list=` blob (the US boards are
    server-rendered; the HK ones are JS shells and are not used);
  * it keeps a Chinese write-up that names the company, and drops the three
    classes that made the board noisy in measurement: index roundups with no
    company name, off-topic chatter, and posts outside the delta window.

A 200 page with no parseable blob must return None (a layout change), never []
- otherwise the delta advances past a window that was never actually read.
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


def check(name, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"   [{detail}]" if detail and not ok else ""))


def post(pid, title, ts="2026-10-30 09:46:20", nick="陆控资讯", ptype=1, clicks=100):
    return {"post_id": pid, "post_title": title, "post_publish_time": ts,
            "user_nickname": nick, "post_type": ptype,
            "post_click_count": clicks, "post_comment_count": 3}


PAGE = (
    '<html><script>var article_list={"re":['
    # 1) the real thing: a news-account write-up naming the company -> KEEP
    + __import__("json").dumps(
        post(1, "陆控三季度总营收61.8亿，消金业务新贷同比增长25.2%"), ensure_ascii=False)
    + ","
    # 2) index roundup from the same news account, no company name -> DROP
    + __import__("json").dumps(
        post(2, "纳斯达克中国金龙指数收涨0.59%"), ensure_ascii=False)
    + ","
    # 3) retail chatter naming the company, no event word -> DROP
    + __import__("json").dumps(
        post(3, "陆金所明天会涨吗", nick="散户老王", ptype=0), ensure_ascii=False)
    + ","
    # 4) retail chatter WITH an event word and the name -> KEEP
    + __import__("json").dumps(
        post(4, "陆金所回购公告怎么看", nick="散户老王", ptype=0), ensure_ascii=False)
    + ","
    # 5) a news write-up outside the delta window -> DROP
    + __import__("json").dumps(
        post(5, "陆金所三季度预亏13亿", ts="2026-10-19 12:00:00"), ensure_ascii=False)
    + ']};</script></html>'
)


class FakeResp:
    status_code = 200
    text = PAGE

    def raise_for_status(self):
        pass


real_get = nu.requests.get
real_attr = nu.requests.get
nu.requests.get = lambda *a, **k: FakeResp()
try:
    terms = ["陆金所", "陆控", "平安普惠"]
    got = nu.fetch_guba("LU", None, terms=terms)
    titles = [it["title"] for it in (got or [])]
    check("returns items", bool(got), str(got))
    check("keeps the Chinese write-up that names the company",
          any("三季度总营收61.8亿" in t for t in titles), str(titles))
    check("drops the index roundup with no company name",
          not any("金龙指数" in t for t in titles), str(titles))
    check("drops chatter with no event word",
          not any("明天会涨吗" in t for t in titles), str(titles))
    check("keeps chatter that names an event and the company",
          any("回购公告" in t for t in titles), str(titles))
    check("URL points at the post, not the board",
          all("/news,usLU," in it["url"] for it in (got or [])),
          str([it["url"] for it in (got or [])]))
    check("source label is Guba", all(it["source"] == "Guba" for it in (got or [])))
    check("dates are normalised to ET (Beijing 09:46 -> 21:46 the day before)",
          any(it["date"].endswith("21:46:20") for it in (got or [])),
          str([it.get("date") for it in (got or [])]))

    # Delta window: the 19 Oct post must be excluded when the window starts later.
    since = datetime.datetime(2026, 10, 30, 0, 0, tzinfo=nu.EASTERN)
    got2 = nu.fetch_guba("LU", since, terms=terms)
    check("delta window drops older posts",
          not any("预亏13亿" in it["title"] for it in (got2 or [])),
          str([it["title"] for it in (got2 or [])]))

    # A page whose layout changed: must be None (retry), never [] (advance delta).
    class Empty(FakeResp):
        text = "<html><body>no blob here</body></html>"

    nu.requests.get = lambda *a, **k: Empty()
    check("unparseable page returns None, not []",
          nu.fetch_guba("LU", None, terms=terms) is None)

    # A hard failure (network) also returns None.
    def boom(*a, **k):
        raise RuntimeError("network down")

    nu.requests.get = boom
    check("network failure returns None", nu.fetch_guba("LU", None, terms=terms) is None)

    # A non-US symbol has no usable board and must not make a request.
    nu.requests.get = boom
    check("non-US symbol returns [] without a request",
          nu.fetch_guba("0700.HK", None, terms=terms) == [])
finally:
    nu.requests.get = real_get

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
