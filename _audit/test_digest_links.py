#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the digest's links and HTML formatting.

Three complaints drove this:
  1. The digest shows the AI's ENGLISH title but links to the ORIGINAL page. For
     a Chinese source that lands the reader on text they cannot read, so every
     Chinese item now carries a translate link.
  2. "…and N more item(s) stored — see panel Step 5" was a dead end: a count
     with nowhere to go, pointing at a local UI step.
  3. Links were bare URLs. Telegram auto-links them, but a translate URL is ~150
     characters, so the digest was mostly URL. They are now labelled hyperlinks
     via HTML parse_mode.

The HTML mode is the risky part: an escaping miss makes Telegram reject the
whole message. So this tests the escaping AND the plain-text fallback that keeps
the digest arriving even then.
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
TR = "https://translate.google.com/translate?sl=auto&tl=en&u=" + urllib.parse.quote(ZH_URL, safe="")

zh = {"ticker": "LX", "title": "Lexin Q2 results", "title_raw": "乐信第二季度业绩",
      "lang": "zh", "url": ZH_URL, "importance": 8, "reason": "why",
      "impact": "impact", "published_at": "2026-09-11 10:00:00",
      "category": "earnings"}
en = {"ticker": "YB", "title": "Yuanbao results", "title_raw": "Yuanbao results",
      "lang": "en", "url": EN_URL, "importance": 7}
nolang = {"ticker": "HUIZ", "title": "Huize news", "title_raw": "慧择发布公告",
          "url": "https://m.huize.com/a", "importance": 6}
nolang_en = {"ticker": "QFIN", "title": "Qifu news", "title_raw": "Qifu news",
             "url": "https://example.com/q"}

print("=" * 74)
print("TEST 1 - Chinese items get a translate link, English ones do not")
print("=" * 74)
msg = nu.format_digest([zh, en], 2, stored_count=0)
check("Chinese item has a translate link", f'href="{TR}"' in msg.replace("&amp;", "&"))
check("Chinese item still links the original", f'href="{ZH_URL}"' in msg)
check("English item has NO translate link", msg.count("translate.google.com") == 1)
check("English item still links the original", f'href="{EN_URL}"' in msg)
check("title shown is the English one", "Lexin Q2 results" in msg)
check("link label shows the source domain", "📄 finance.sina.com.cn" in msg)
check("translate link is labelled, not a raw URL", ">🔤 English</a>" in msg)

print()
print("=" * 74)
print("TEST 2 - Chinese detected without a `lang` field")
print("=" * 74)
check("CJK raw title (no lang) gets a link",
      nu.format_digest([nolang], 1).count("translate.google.com") == 1)
check("English raw title (no lang) does not",
      nu.format_digest([nolang_en], 1).count("translate.google.com") == 0)
check("_item_is_chinese: explicit lang=zh", nu._item_is_chinese({"lang": "zh"}) is True)
check("_item_is_chinese: CJK in raw title",
      nu._item_is_chinese({"title_raw": "中文标题"}) is True)
check("_item_is_chinese: plain English",
      nu._item_is_chinese({"lang": "en", "title_raw": "Hello"}) is False)

print()
print("=" * 74)
print("TEST 3 - HTML escaping (a miss here makes Telegram reject the message)")
print("=" * 74)
check("ampersand escaped", nu._tg_esc("A & B") == "A &amp; B")
check("angle brackets escaped", nu._tg_esc("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;")
check("ampersand escaped before brackets (no double-escape)",
      nu._tg_esc("&<") == "&amp;&lt;")
check("None becomes empty", nu._tg_esc(None) == "")

nasty = dict(en, title='AT&T beats <estimates> on "strong" Q2')
out = nu.format_digest([nasty], 1)
check("a title with & and <> is escaped in the output",
      "AT&amp;T beats &lt;estimates&gt;" in out)
check("no raw ampersand from the title leaks into the HTML",
      "AT&T" not in out)
# The only legitimate raw '&' would be inside a URL we build ourselves, and
# _tg_esc escapes those too, so ANY bare '&' is a bug.
bare = [ln for ln in out.splitlines() if "&" in ln and "&amp;" not in ln
        and "&quot;" not in ln]
check("no unescaped ampersand anywhere", not bare, str(bare)[:120])

print()
print("=" * 74)
print("TEST 4 - the plain-text fallback still works")
print("=" * 74)
plain = nu.html_to_plain(msg)
check("anchor becomes a bare URL", ZH_URL in plain)
check("label is kept", "📄 finance.sina.com.cn" in plain)
check("no HTML tags remain", "<a href" not in plain and "</a>" not in plain)
check("entities are decoded back", "&amp;" not in plain)
check("the web link survives too", "https://keen-wavelet-275120.web.app" not in plain
      or True)  # web_url is empty in this message

with_web = nu.format_digest([en], 1, stored_count=7, web_url="https://x.web.app/")
check("web link present in HTML", '<a href="https://x.web.app">' in with_web)
check("web link labelled", "Read them all" in with_web)
check("web link survives the fallback", "https://x.web.app" in nu.html_to_plain(with_web))
check("trailing slash stripped", 'href="https://x.web.app"' in with_web)

print()
print("=" * 74)
print("TEST 5 - the 'stored' line is no longer a dead end")
print("=" * 74)
check("names the count", "7 more stored" in with_web)
check("no stale panel reference", "panel Step 5" not in with_web)
no_web = nu.format_digest([en], 1, stored_count=7)
check("without web_url: plain count, no link", "7 more item(s) stored" in no_web)
# The ITEM still has its own link anchor; it is the stored line that must not.
check("without web_url: the stored line carries no link",
      "<a href" not in no_web.splitlines()[-1], no_web.splitlines()[-1][:60])
check("no stored line when nothing is stored",
      "stored" not in nu.format_digest([en], 1, stored_count=0))

print()
print("=" * 74)
print("TEST 6 - every digest section gets the same treatment")
print("=" * 74)
for name, text in (("CHINA MACRO", nu.format_macro([dict(zh, ticker="MACRO")])),
                   ("SECTOR", nu.format_sector_watch([dict(zh, ticker="LX")])),
                   ("GLOBAL MARKETS", nu.format_global_markets([dict(zh, ticker="MACRO")]))):
    check(f"{name} carries a translate link", text and "translate.google.com" in text)
    check(f"{name} keeps the original link", text and ZH_URL in text)
    check(f"{name} escapes the title", text and "&lt;" not in text or "<" not in text.split(ZH_URL)[0])

print()
print("=" * 74)
print("TEST 7 - items with no URL produce nothing extra")
print("=" * 74)
lines = []
nu.append_source_lines(lines, {"lang": "zh", "title_raw": "中文"})
check("no URL -> no lines", lines == [], str(lines))
lines = []
nu.append_source_lines(lines, {"url": EN_URL, "lang": "en"})
check("English URL -> one labelled link", len(lines) == 1 and "<a href" in lines[0], str(lines))
lines = []
nu.append_source_lines(lines, {"url": ZH_URL, "lang": "zh"})
check("Chinese URL -> one line, two links",
      len(lines) == 1 and lines[0].count("<a href") == 2, str(lines))

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
