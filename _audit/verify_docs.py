#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the documentation's factual claims against the code, mechanically.
Each check asserts one claim that was previously wrong."""
import importlib.util
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
B = r"F:\MyRepository\PortfolioNewsUpdater"
nu_src = io.open(B + r"\news_updater.py", encoding="utf-8").read()
cm_src = io.open(B + r"\cloud_manager.py", encoding="utf-8").read()
sh_src = io.open(B + r"\setup_cloud.sh", encoding="utf-8").read()
readme = io.open(B + r"\README.md", encoding="utf-8").read()
guide = io.open(B + r"\GUIDE-NEWS.md", encoding="utf-8").read()

spec = importlib.util.spec_from_file_location("nu", B + r"\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))

# 1. schedule times
check("run times are 09:15 and 17:00 ET",
      nu.SCHEDULE_RUN_TIMES == (nu.dtime(9, 15), nu.dtime(17, 0)),
      str(nu.SCHEDULE_RUN_TIMES))
check("docs no longer mention 16:45 ET",
      "16:45" not in readme and "16:45" not in guide)

# 2. Mon-Fri
check("cron is Mon-Fri (1-5)", "1-5" in sh_src)
check("docs state Mon-Fri / weekends skipped",
      "Monday to Friday" in readme and "Weekends are skipped" in guide)

# 3. EXA per-ticker off by default and documented
check("EXA_PER_TICKER_DEFAULT is False", nu.EXA_PER_TICKER_DEFAULT is False)
check("docs say per-ticker EXA is off",
      "Per-ticker EXA news search is OFF by default" in guide
      and "off** by default" in readme)
check("exa_per_ticker knob documented", "exa_per_ticker" in guide)

# 4. dry-run semantics
check("dry-run implies no-write",
      'NO_WRITE = ("--no-write" in sys.argv) or ("--dry-run" in sys.argv)' in nu_src)
check("paid calls refused under no-write",
      nu_src.count("if NO_WRITE:") >= 2
      and "no credits used" in nu_src)
check("docs describe dry-run as no paid calls",
      "no paid calls" in guide and "no paid calls" in readme)
check("docs no longer claim dry-run only suppresses telegram",
      "run but DON'T send Telegram" not in guide)

# 5. AI history source
check("AI history reads pushed_stories",
      "FROM pushed_stories" in nu_src and "def get_recent_pushed_titles" in nu_src)
check("dead get_recent_seen_titles removed", "def get_recent_seen_titles" not in nu_src)
check("docs describe pushed_stories as the history",
      "pushed_stories` ledger" in guide)

# 6. Tavily caps agree everywhere
import json
ex = json.load(io.open(B + r"\config_local.example.json", encoding="utf-8"))
cm = importlib.util.spec_from_file_location("cm", B + r"\cloud_manager.py")
cmm = importlib.util.module_from_spec(cm)
cm.loader.exec_module(cmm)
vals = {
    "updater": (nu.TAVILY_MAX_DAILY_SEARCHES, nu.TAVILY_MAX_MONTHLY_SEARCHES),
    "example": (ex["tavily_max_daily_searches"], ex["tavily_max_monthly_searches"]),
    "panel": (cmm.DEFAULT_CONFIG["tavily_max_daily_searches"],
              cmm.DEFAULT_CONFIG["tavily_max_monthly_searches"]),
}
check("Tavily caps identical in all three artifacts",
      len(set(vals.values())) == 1, str(vals))
check("docs state 30/day and 900/month",
      "max 30/day, 900/month" in readme and "(default **30**)" in guide)
check("push_max_per_ticker default 2 everywhere",
      cmm.DEFAULT_CONFIG["push_max_per_ticker"] == 2
      and ex["push_max_per_ticker"] == 2
      and 'id="pushMaxPerTicker" type="number" min="1" value="2"' in cm_src)

# 7. Deep search documented
check("GUIDE documents Deep search",
      "Deep search — one-off look at ANY ticker" in guide and "--adhoc-scan" in guide)
check("README documents the tabs and Deep search",
      "Deep search" in readme and "four tabs" in readme)

# 8. panel defaults for the new toggles are at least honest
check("docs explain sector_watch/global_markets are config-file settings",
      "no panel control" in guide or "hand-edit" in guide
      or "cannot be enabled from the panel" in guide)

# 9. stopping instructions cover cron.d
check("stopping instructions include /etc/cron.d",
      "/etc/cron.d/news-summer" in guide)

# 10. runtime data list
for f in ("exa_usage.json", "news_run_history.json", "sec_validate.json",
          "news_updater.lock", "panel_url.txt"):
    check(f"runtime data doc mentions {f}", f in guide)

# 11. stale comments fixed
check("setup_cloud.sh header no longer claims it runs the pipeline",
      "Runs news_updater.py once (--force --dry-run)" not in sh_src)
check("module docstring no longer says 16:45", "16:45" not in nu_src)

print(f"{'check':62s} result")
print("-" * 78)
bad = 0
for name, ok, detail in results:
    if not ok:
        bad += 1
    print(f"{name:62s} {'OK' if ok else 'FAIL ' + detail}")
print()
print(f"{len(results) - bad}/{len(results)} checks pass")
sys.exit(1 if bad else 0)
