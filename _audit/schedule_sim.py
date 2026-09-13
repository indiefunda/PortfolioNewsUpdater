"""Simulate the DST-aware cron generation in setup_cloud.sh.

Reads the REAL constants out of news_updater.py (SCHEDULE_RUN_TIMES) and the
REAL RUNS list out of setup_cloud.sh, so the simulation cannot silently drift
from the code it is auditing.
"""
import re
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "news_updater.py"
SH = ROOT / "setup_cloud.sh"
E = ZoneInfo("America/New_York")
U = ZoneInfo("UTC")

text = SRC.read_text(encoding="utf-8")

# SCHEDULE_RUN_TIMES = (dtime(9, 15), dtime(17, 0))
line = next(l for l in text.splitlines() if l.strip().startswith("SCHEDULE_RUN_TIMES"))
runs = [(int(a), int(b)) for a, b in re.findall(r"dtime\(\s*(\d+)\s*,\s*(\d+)\s*\)", line)]
tol = int(re.search(r"SCHEDULE_TOLERANCE_MIN\s*=\s*(\d+)", text).group(1))

# RUNS = [(9, 15), (17, 0)]
sh_line = re.search(r"^RUNS\s*=\s*\[(.*?)\]", SH.read_text(encoding="utf-8"), re.M).group(1)
sh_runs = [(int(a), int(b)) for a, b in re.findall(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", sh_line)]

print(f"news_updater.py SCHEDULE_RUN_TIMES = {runs}   (tolerance {tol} min)")
print(f"setup_cloud.sh RUNS               = {sh_runs}")
print(f"the two agree                     = {runs == sh_runs}")
assert runs == sh_runs, "run lists disagree between updater and installer!"

year = datetime.now().year
cron = []  # (season, utc_dt_on_reference_day)
for season, mo, day in (("summer", 7, 1), ("winter", 1, 1)):
    for h, mn in runs:
        cron.append((season, datetime(year, mo, day, h, mn, tzinfo=E).astimezone(U)))

print("\nET wall clock -> UTC cron field")
all_offpeak = True
for season, u in cron:
    peak = (1 <= u.hour < 4) or (6 <= u.hour < 10)
    all_offpeak &= not peak
    print(f"  {season:6} -> {u:%H:%M} UTC   cron \"{u.minute:02d} {u.hour:02d} * * 1-5\"   "
          f"{'*** PEAK - double cost ***' if peak else 'off-peak OK'}")

print(f"\ncron lines generated              = {len(cron)} (expect 4)")
print(f"all off-peak                      = {all_offpeak}")


def passes_guard(fire_utc: datetime) -> bool:
    """Mirror of news_updater._schedule_guard()."""
    et = fire_utc.astimezone(E)
    return any(
        abs((datetime.combine(et.date(), et.time().replace(tzinfo=None))
             - datetime.combine(et.date(), dtime(th, tm))).total_seconds()) <= tol * 60
        for th, tm in runs
    )


print("\nguard behaviour - which cron jobs actually run (both seasons, plus the")
print("DST transition Mondays, which are the days a naive schedule breaks):")
probe_days = [
    ("summer", datetime(year, 7, 15, 12, 0, tzinfo=U)),
    ("winter", datetime(year, 1, 15, 12, 0, tzinfo=U)),
]
# 2nd Sunday March / 1st Sunday Nov = US DST switch; test the Monday after.
mar = [d for d in range(8, 15) if datetime(year, 3, d).weekday() == 6][0]
nov = [d for d in range(1, 8) if datetime(year, 11, d).weekday() == 6][0]
probe_days.append(("DST->EDT", datetime(year, 3, mar + 1, 12, 0, tzinfo=U)))
probe_days.append(("DST->EST", datetime(year, 11, nov + 1, 12, 0, tzinfo=U)))

ok = True
for name, day in probe_days:
    survivors = []
    for season, ref in cron:
        fire = datetime(day.year, day.month, day.day, ref.hour, ref.minute, tzinfo=U)
        if fire.weekday() < 5 and passes_guard(fire):  # cron is 1-5
            survivors.append(f"{season} {ref:%H:%M}UTC -> {fire.astimezone(E):%H:%M} ET")
    seasons = {s.split()[0] for s in survivors}
    print(f"  {name:9} {day:%a %Y-%m-%d}: {len(survivors)} run(s) -> {survivors}")
    # Exactly 2 real runs (pre-open + post-close), and every one of them must
    # come from a SINGLE season - if both seasons passed, the day would get 4.
    if len(survivors) != 2 or len(seasons) != 1:
        ok = False

print()
if ok and all_offpeak:
    print("RESULT: PASS - exactly one real run per day in every season and across "
          "both DST transitions; all four cron lines land off-peak")
    sys.exit(0)
print("RESULT: FAIL - the generated schedule does not deliver exactly two "
      "off-peak runs per day")
sys.exit(1)
