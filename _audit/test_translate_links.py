#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test which 'translate this page' URL forms actually work today.

The digest and the web page both link to
    translate.google.com/translate?sl=auto&tl=en&u=<url>
which is a DEPRECATED Google endpoint. This checks what each candidate really
returns, using a real Chinese article from the archive.
"""
import sqlite3
import sys
import urllib.parse

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# A real Chinese article from the local archive copy.
conn = sqlite3.connect(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
row = conn.execute(
    "SELECT url, title_raw FROM news WHERE lang='zh' AND url LIKE 'http%' "
    "AND url NOT LIKE '%news.google.com%' LIMIT 1").fetchone()
conn.close()
if not row:
    print("no Chinese article found to test with")
    sys.exit(1)
article, title = row
print(f"test article: {article}")
print(f"  ({title[:60]})")
enc = urllib.parse.quote(article, safe="")
print()

CANDIDATES = [
    ("Google proxy (what we use now)",
     f"https://translate.google.com/translate?sl=auto&tl=en&u={enc}"),
    ("Google widget with op=translate",
     f"https://translate.google.com/?sl=auto&tl=en&u={enc}&op=translate"),
    ("Google widget op=websites",
     f"https://translate.google.com/?sl=auto&tl=en&u={enc}&op=websites"),
    ("Microsoft translatetheweb.com",
     f"https://www.translatetheweb.com/?from=zh-Hans&to=en&a={enc}"),
    ("Microsoft microsofttranslator.com",
     f"https://www.microsofttranslator.com/bv.aspx?from=zh-Hans&to=en&a={enc}"),
    ("DeepL (URL as text - probably useless)",
     f"https://www.deepl.com/translator#en/zh/{enc}"),
]

for label, url in CANDIDATES:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25,
                         allow_redirects=True)
        body = r.text or ""
        # A translated page should contain English and NOT be a tiny error shell.
        has_english = any(w in body for w in (" the ", " and ", " of ", " company "))
        looks_error = any(w in body.lower() for w in
                          ("error", "not found", "cannot be reached", "unsupported"))
        print(f"{label}")
        print(f"   HTTP {r.status_code}  final={r.url[:100]}")
        print(f"   bytes={len(body)}  english_looking={has_english}  error_words={looks_error}")
    except Exception as exc:
        print(f"{label}")
        print(f"   FAILED: {type(exc).__name__}: {str(exc)[:100]}")
    print()
