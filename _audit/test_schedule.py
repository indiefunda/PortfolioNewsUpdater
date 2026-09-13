#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the cron slots + schedule guard produce exactly two runs per day in
each DST season, and that every real slot is off-peak for DeepSeek."""
import importlib.util
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

UTC = ZoneInfo("UTC")
PEAK = [(1, 4), (6, 10)]           # DeepSeek double-price windows (UTC, Mon-Fri)
CRON_SLOTS = [(13, 15), (21, 0), (14, 15), (22, 0)]   # what cron now fires

print("cron slots (UTC) -> behavior")
print("-" * 72)
fail = 0
for season, month, day in (("EDT", 7, 1), ("EST", 1, 1)):
    real = []
    for hh, mm in CRON_SLOTS:
        now = datetime(2026, month, day, hh, mm, tzinfo=UTC).astimezone(nu.EASTERN)
        # The guard exits(0) when the time is NOT a scheduled slot; reproduce
        # its decision without exiting.
        ok = any(abs((datetime.combine(now.date(), now.time())
                      - datetime.combine(now.date(), t)).total_seconds())
                 <= nu.SCHEDULE_TOLERANCE_MIN * 60 for t in nu.SCHEDULE_RUN_TIMES)
        peak = any(lo <= hh < hi for lo, hi in PEAK)
        mark = "RUN " if ok else "skip"
        if ok:
            real.append(now.strftime("%H:%M ET"))
            if peak:
                fail += 1
                mark = "PEAK!"
        print(f"  {season} {hh:02d}:{mm:02d} UTC = {now.strftime('%H:%M')} ET "
              f"-> {mark:5s} {'(peak pricing!)' if peak else ''}")
    print(f"  {season}: real runs = {real}")
    if len(real) != 2:
        fail += 1
        print(f"  {season}: FAIL - expected exactly 2 runs, got {len(real)}")
    print()

print("=" * 72)
# Weekly sanity: simulate a full week of cron firing in each season.
for label, month, day in (("EDT week", 7, 6), ("EST week", 1, 5)):
    start = datetime(2026, month, day, 0, 0, tzinfo=nu.EASTERN)
    fired, peak_fired = 0, 0
    for d in range(7):
        for hh, mm in CRON_SLOTS:
            slot_utc = datetime(2026, month, day + d, hh, mm, tzinfo=UTC)
            now = slot_utc.astimezone(nu.EASTERN)
            if now.weekday() >= 5:      # cron is 1-5 (Mon-Fri)
                continue
            ok = any(abs((datetime.combine(now.date(), now.time())
                          - datetime.combine(now.date(), t)).total_seconds())
                     <= nu.SCHEDULE_TOLERANCE_MIN * 60
                     for t in nu.SCHEDULE_RUN_TIMES)
            if ok:
                fired += 1
                if any(lo <= hh < hi for lo, hi in PEAK):
                    peak_fired += 1
    expected = 2 * 5                # two runs x five weekdays
    good = fired == expected and peak_fired == 0
    if not good:
        fail += 1
    print(f"  {label}: {fired} runs/week (expected {expected}), "
          f"{peak_fired} during peak pricing -> {'OK' if good else 'FAIL'}")

print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
