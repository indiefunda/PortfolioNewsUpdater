#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which of YOUR stored articles does the translate link actually work for?

The Google proxy works in general (verified on one eastmoney article), so the
complaint must be URL-specific. This samples the real archive and reports which
sources translate and which do not.
"""
import sqlite3
import sys
import urllib.parse
from collections import defaultdict

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
conn = sqlite3.connect(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT DISTINCT url, source, title_raw FROM news "
    "WHERE lang='zh' AND url LIKE 'http%' ORDER BY id DESC LIMIT 400").fetchall()
conn.close()

# Take up to 3 distinct URLs per source so one bad outlet cannot dominate.
by_source = defaultdict(list)
for r in rows:
    host = urllib.parse.urlparse(r["url"]).hostname or "?"
    if len(by_source[host]) < 3:
        by_source[host].append(r)

print(f"sampling {sum(len(v) for v in by_source.values())} URLs "
      f"across {len(by_source)} hosts\n")
print(f"{'host':32} {'HTTP':5} {'proxy?':7} {'english':8} verdict")
print("-" * 78)

ok_hosts, bad_hosts = [], []
for host in sorted(by_source):
    for r in by_source[host]:
        enc = urllib.parse.quote(r["url"], safe="")
        link = f"https://translate.google.com/translate?sl=auto&tl=en&u={enc}"
        try:
            resp = requests.get(link, headers={"User-Agent": UA}, timeout=25)
            final = resp.url
            is_proxy = "translate.goog" in final
            body = resp.text or ""
            english = any(w in body for w in (" the ", " and ", " of "))
            consent = "consent.google.com" in final
            if consent:
                verdict = "CONSENT WALL"
            elif is_proxy and english:
                verdict = "ok"
            elif is_proxy:
                verdict = "proxy but no English"
            else:
                verdict = "NOT a translated page"
            print(f"{host[:32]:32} {resp.status_code:<5} "
                  f"{str(is_proxy):7} {str(english):8} {verdict}")
            (ok_hosts if verdict == "ok" else bad_hosts).append(host)
        except Exception as exc:
            print(f"{host[:32]:32} {'--':5} {'--':7} {'--':8} FAILED {type(exc).__name__}")
            bad_hosts.append(host)
        break  # one URL per host is enough to judge the host

print()
print(f"hosts that translate OK : {len(set(ok_hosts))}")
print(f"hosts that do NOT       : {len(set(bad_hosts))}")
if bad_hosts:
    print("  failing hosts: " + ", ".join(sorted(set(bad_hosts))[:12]))
