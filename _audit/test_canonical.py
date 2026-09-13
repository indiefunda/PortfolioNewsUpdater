#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Can a Google News RSS link be decoded back to the real article URL?
Google News links look like:
  https://news.google.com/rss/articles/CBMi<base64-ish>?oc=5
The payload is a protobuf whose bytes contain the original URL as a string.
If the original URL is recoverable, duplicates across sources become a simple
string comparison instead of fuzzy title matching."""
import base64
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = sqlite3.connect(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
conn.row_factory = sqlite3.Row

URL_RE = re.compile(rb"https?://[^\s\"'<>\x00-\x1f]{6,600}")


def canonical_url(url):
    """Return the real article URL behind a Google News RSS link, else url."""
    url = str(url or "").strip()
    if "news.google.com" not in url or "/articles/" not in url:
        return url
    try:
        seg = url.split("/articles/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg)
    except Exception:
        return url
    found = URL_RE.findall(raw)
    if not found:
        return url
    # The payload can hold several strings; the longest http(s) run is the URL.
    best = max(found, key=len).decode("utf-8", "replace")
    return best.rstrip("\\\x01\x02\x03 ")


rows = conn.execute("SELECT url FROM news WHERE url LIKE '%news.google.com%' "
                    "AND url != '' LIMIT 12").fetchall()
print(f"google-news URLs sampled: {len(rows)}")
ok = 0
for r in rows:
    real = canonical_url(r["url"])
    decoded = real != r["url"]
    if decoded:
        ok += 1
    print(f"  [{'OK ' if decoded else 'no '}] {real[:96]}")
print()
print(f"decoded {ok} of {len(rows)}")
conn.close()
