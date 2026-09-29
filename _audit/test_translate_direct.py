#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Can we build the translate.goog URL directly, skipping Google's redirect?

translate.google.com/translate?u=<url> works by 302-ing to
    <host with dots replaced by dashes>.translate.goog<path>?_x_tr_...
Extra hops are exactly what an in-app browser (Telegram's, for instance)
mishandles, so if the direct form is reliable it is the better link.

Tests the construction against real hosts from the archive and verifies the
proxy actually serves translated content, not an error shell.
"""
import random
import sqlite3
import sys
import time
import urllib.parse

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TR_SL = "auto"
TR_TL = "en"
TR_HL = "en"


def direct_proxy_url(url):
    """Build the translate.goog URL the way Google's redirect does."""
    p = urllib.parse.urlsplit(url)
    host = p.netloc.replace(".", "-")
    return (f"https://{host}.translate.goog{p.path}"
            f"?_x_tr_sl={TR_SL}&_x_tr_tl={TR_TL}&_x_tr_hl={TR_HL}")


conn = sqlite3.connect(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT DISTINCT url FROM news WHERE lang='zh' AND url LIKE 'http%' "
    "AND url NOT LIKE '%news.google.com%'").fetchall()
conn.close()
urls = [r["url"] for r in rows]
random.seed(7)
sample = random.sample(urls, min(14, len(urls)))

print(f"testing {len(sample)} of {len(urls)} stored Chinese URLs\n")
print(f"{'constructed host':44} {'HTTP':5} {'english':8} verdict")
print("-" * 84)
ok = bad = 0
for u in sample:
    d = direct_proxy_url(u)
    host = urllib.parse.urlsplit(d).netloc
    try:
        r = requests.get(d, headers={"User-Agent": UA}, timeout=25,
                         allow_redirects=True)
        body = r.text or ""
        # Reject Google's "page cannot be translated" shells.
        dead = any(w in body.lower() for w in
                   ("cannot be translated", "could not be translated",
                    "page not found", "unsupported"))
        english = any(w in body for w in (" the ", " and ", " of ")) and not dead
        verdict = "ok" if (r.status_code == 200 and english) else (
            "dead shell" if dead else f"no english (HTTP {r.status_code})")
        print(f"{host[:44]:44} {r.status_code:<5} {str(english):8} {verdict}")
        if verdict == "ok":
            ok += 1
        else:
            bad += 1
    except Exception as exc:
        print(f"{host[:44]:44} {'--':5} {'--':8} FAILED {type(exc).__name__}")
        bad += 1
    time.sleep(0.4)   # be polite; Google rate-limits aggressive loops

print()
print(f"direct translate.goog works: {ok}/{ok + bad}")
print()
print("example of the derived form:")
print("  original : " + sample[0])
print("  direct   : " + direct_proxy_url(sample[0]))
