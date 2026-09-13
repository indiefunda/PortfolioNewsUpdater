#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify: with NO_WRITE set, Tavily/EXA refuse to make a paid call; with it
clear (the ad-hoc scan path), they do make it."""
import importlib.util
import os
import sys
import types

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

calls = []
# Stub the transport so no real request leaves this machine.
def fake_post(url, **kw):
    calls.append(url)
    raise RuntimeError("stopped before the network")
nu.requests = types.SimpleNamespace(post=fake_post, get=fake_post)

cfg = {"tavily_max_daily_searches": 30, "tavily_max_monthly_searches": 900,
       "exa_max_daily_searches": 32, "exa_max_monthly_searches": 980}
sec = {"tavily_api_key": "x", "exa_api_key": "y"}

print("=== NO_WRITE = True (--dry-run / --no-write) ===")
nu.NO_WRITE = True
calls.clear()
r1 = nu.fetch_tavily("test", sec, cfg)
r2 = nu.fetch_exa("test", sec, cfg)
print("  tavily result:", r1, "| exa result:", r2)
print("  HTTP calls made:", len(calls), "(want 0)")
ok1 = (len(calls) == 0 and r1 is None and r2 is None)

print()
print("=== NO_WRITE = False (a real run / the ad-hoc scan) ===")
nu.NO_WRITE = False
calls.clear()
try:
    nu.fetch_tavily("test", sec, cfg)
except RuntimeError:
    pass
n_tav = len(calls)
try:
    nu.fetch_exa("test", sec, cfg)
except RuntimeError:
    pass
print("  HTTP calls made:", len(calls), "(want 2: tavily + exa)")
ok2 = len(calls) == 2

print()
print("RESULT:", "PASS - dry run spends nothing, real runs still call out"
      if ok1 and ok2 else f"FAIL (dry={ok1}, real={ok2})")
sys.exit(0 if ok1 and ok2 else 1)
