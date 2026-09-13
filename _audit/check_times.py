#!/usr/bin/env python3
"""Verify the two target runs are DST-correct and outside DeepSeek peak pricing."""
from datetime import datetime
from zoneinfo import ZoneInfo

E = ZoneInfo("America/New_York")
U = ZoneInfo("UTC")

# DeepSeek peak-priced (double-cost) windows, Mon-Fri, in UTC: 01:00-04:00, 06:00-10:00
PEAK = [(1, 4), (6, 10)]


def is_peak(hour):
    return any(lo <= hour < hi for lo, hi in PEAK)


print("target runs: 09:15 ET (pre-open) and 17:00 ET (1h after close)")
print()
for season, month, day in (("EDT (summer)", 7, 1), ("EST (winter)", 1, 1)):
    for h, m in ((9, 15), (17, 0)):
        et = datetime(2026, month, day, h, m, tzinfo=E)
        utc = et.astimezone(U)
        print("%-13s %02d:%02d ET -> %s UTC   peak=%s"
              % (season, h, m, utc.strftime("%H:%M"), is_peak(utc.hour)))
