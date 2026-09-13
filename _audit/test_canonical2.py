#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check the base64 payloads: do ANY contain a URL, and how do Google News
links relate to the direct outlet links for the same story?"""
import base64
import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = sqlite3.connect(r"F:\MyRepository\PortfolioNewsUpdater\_audit\news.db")
conn.row_factory = sqlite3.Row

URL_RE = re.compile(rb"https?://")
print("=== do the payloads contain a URL at all? ===")
n_payload = n_url = 0
for r in conn.execute("SELECT url FROM news WHERE url LIKE '%news.google.com%' LIMIT 40"):
    url = r["url"]
    try:
        seg = url.split("/articles/", 1)[1].split("?", 1)[0]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg)
    except Exception:
        continue
    n_payload += 1
    if URL_RE.search(raw):
        n_url += 1
print(f"  payloads decoded: {n_payload}; containing an http(s) URL: {n_url}")
print("  -> Google's newer /rss/articles/CBMi... links are opaque ids; the target")
print("     is only obtainable by calling news.google.com (a redirect/API).")
print()

print("=== the same story from Google News vs the outlet itself ===")
# Find stories where the SAME ticker/run has both a google-news URL and a
# non-google URL (a real duplicate the canonical decode would have merged).
rows = conn.execute(
    "SELECT ticker, first_seen, title_raw, title_en, url, source FROM news "
    "WHERE url != '' ORDER BY ticker, first_seen").fetchall()
groups = {}
for r in rows:
    key = (r["ticker"], r["first_seen"][:16])
    groups.setdefault(key, []).append(r)
mixed = 0
for key, items in groups.items():
    has_g = any("news.google.com" in (i["url"] or "") for i in items)
    has_o = any("news.google.com" not in (i["url"] or "") for i in items)
    if has_g and has_o:
        mixed += 1
        if mixed <= 5:
            print(f"  {key[0]} @ {key[1]}:")
            for i in items[:4]:
                print(f"     {i['source']:13s} {i['url'][:80]}")
print(f"  run/ticker groups containing BOTH google and outlet links: {mixed}")
print()
print("=== how many stored rows are google-news links at all? ===")
tot = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
g = conn.execute("SELECT COUNT(*) FROM news WHERE url LIKE '%news.google.com%'").fetchone()[0]
print(f"  {g} of {tot} rows ({100*g/tot:.1f}%)")
conn.close()
