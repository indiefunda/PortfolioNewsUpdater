#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prototype: batch-translate stored Chinese headlines with ONE AI call.
Measures cost/latency so the feature can be sized honestly."""
import importlib.util
import json
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("nu", "news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

secrets = nu.load_secrets()
config = nu.load_config()
key = secrets.get("ai_api_key", "")
base = config.get("ai_base_url") or nu.DEFAULT_AI_BASE
model = config.get("ai_model") or nu.DEFAULT_AI_MODEL

conn = sqlite3.connect("news.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, title_raw, title_en FROM news "
    "WHERE title_raw IS NOT NULL AND title_raw != '' "
    "AND (title_en IS NULL OR title_en = '' OR title_en = title_raw) "
    "ORDER BY id DESC LIMIT 12").fetchall()
print(f"rows needing translation (sample of 12): {len(rows)}")

items = [{"n": i, "title": r["title_raw"]} for i, r in enumerate(rows, 1)]
prompt = (
    "Translate each Chinese headline below into concise natural English. "
    "Keep company names, tickers and numbers as-is. "
    'Return ONLY a JSON array of objects with keys "n" and "en".\n\n'
    "HEADLINES:\n" + json.dumps(items, ensure_ascii=False)
)
t0 = time.time()
content = nu._chat(base, model, key,
                   "You are a precise JSON-returning translator.", prompt,
                   timeout=60)
elapsed = time.time() - t0
print(f"one batched call: {elapsed:.1f}s, response {len(content or '')} chars")
parsed = nu._parse_json_array(content) if content else None
if not parsed:
    print("could not parse the reply:")
    print((content or "")[:400])
    sys.exit(1)
by_n = {}
for obj in parsed:
    try:
        by_n[int(obj.get("n"))] = str(obj.get("en") or "").strip()
    except Exception:
        continue
print(f"translations returned: {len(by_n)} of {len(items)}")
print()
for i, r in enumerate(rows, 1):
    print(f"  {i:2d}. {(r['title_raw'] or '')[:52]}")
    print(f"      -> {by_n.get(i, '(none)')}")
conn.close()
