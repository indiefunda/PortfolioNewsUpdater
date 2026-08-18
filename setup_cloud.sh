#!/usr/bin/env bash
# ============================================================
# PortfolioNewsUpdater - setup for the Google Cloud e2-micro VM
# Run this ONCE on the VM (it can share the same free VM as the
# price monitor - it just adds a separate cron job).
#
#   bash setup_cloud.sh
#
# What it does:
#   1. Installs Python deps (requests, feedparser, edgartools)
#   2. Installs TWO DST-aware cron jobs that run news_updater.py
#      twice a day, pinned to US market time (9:15 ET and 16:45 ET)
#   3. Runs news_updater.py once to confirm it works
#
# Why TWO cron jobs? US Eastern time shifts by 1 hour between DST
# seasons, but cron fires at fixed UTC times. So we install one job
# per season:
#     Summer (EDT, UTC-4): 9:15 ET = 13:15 UTC, 16:45 ET = 20:45 UTC
#     Winter (EST, UTC-5): 9:15 ET = 14:15 UTC, 16:45 ET = 21:45 UTC
# news_updater.py carries a tiny DST-aware guard that makes the
# out-of-season job an instant no-op, so exactly two real runs happen
# per day. This is reliable because cron does the exact timing and the
# correct job is always already installed - no re-setup at DST flips.
# ============================================================

set -e

echo "=============================================="
echo " PortfolioNewsUpdater - Cloud setup"
echo "=============================================="

# --- 1. Install Python + cron (idempotent - safe to run alongside the
#     price monitor's setup)
echo "[1/3] Installing Python and cron..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip cron || true
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true
echo "  cron daemon: $(systemctl is-active cron 2>/dev/null || echo 'not running')"

# --- 2. Install Python dependencies system-wide (so cron can import them)
echo "[2/3] Installing Python packages (requests, feedparser, edgartools)..."
sudo python3 -m pip install --break-system-packages --upgrade pip 2>/dev/null \
  || sudo python3 -m pip install --upgrade pip
sudo python3 -m pip install --break-system-packages -r requirements.txt 2>/dev/null \
  || sudo python3 -m pip install -r requirements.txt
python3 -c "import requests, feedparser; print('  deps OK')" 2>/dev/null \
  || python3 -c "import requests; print('  (edgartools optional)')"

# --- 3. Set up the 3x-daily DST-aware schedule (cron)
echo "[3/3] Setting up the DST-aware 3x-daily schedule (cron)..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="$PROJECT_DIR/news_updater.py"
LOG="$PROJECT_DIR/news_updater.log"
PYTHON="$(command -v python3)"

# Remove any stale /etc/cron.d/ copies from an earlier run (they are not
# touched by the user-crontab cleanup below and could otherwise linger with
# an old or broken schedule).
sudo rm -f /etc/cron.d/news-summer /etc/cron.d/news-winter 2>/dev/null || true

# Compute both seasons' UTC firing times from the SAME source of truth as
# news_updater.py (America/New_York DST rules). This keeps the schedule
# correct for every year automatically. Runs (ET): 9:15 (15 min before the
# 9:30 open), 16:45 (just after the 16:00 close), and 23:00 (Beijing noon -
# the Chinese MORNING news burst that the 16:45 run misses entirely).
SCHEDULE_JSON="$("$PYTHON" - <<'PYEOF'
from datetime import datetime
from zoneinfo import ZoneInfo
EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
def cron_fields(month, day, hour, minute):
    """Return the CRON minute and hour fields for a given ET wall-clock time.

    cron needs the minute and hour as SEPARATE fields (e.g. "15 13"). We must
    NOT emit "13:15" with a colon - that makes cron read a single invalid
    field, which shifts everything right and causes "bad day-of-week".
    """
    dt = datetime(2026, month, day, hour, minute, tzinfo=EASTERN)
    u = dt.astimezone(UTC)
    return u.strftime("%M"), u.strftime("%H")
# July 1 = EDT (summer), Jan 1 = EST (winter). Each run gets its OWN line
# because the minutes differ (23:00 is at minute 0, not 15).
RUNS = [(9, 15), (16, 45), (23, 0)]
for season, month, day in (("summer", 7, 1), ("winter", 1, 1)):
    for i, (h, m) in enumerate(RUNS):
        mn, hr = cron_fields(month, day, h, m)
        print(f"{season}_{i}_min={mn}")
        print(f"{season}_{i}_hour={hr}")
PYEOF
)"

# Build the cron lines for a season (one per run; minute/hour from the JSON).
build_cron() {
  local season="$1" lines="" mn hr
  for i in 0 1 2; do
    mn="$(printf '%s\n' "$SCHEDULE_JSON" | grep "^${season}_${i}_min=" | cut -d= -f2)"
    hr="$(printf '%s\n' "$SCHEDULE_JSON" | grep "^${season}_${i}_hour=" | cut -d= -f2)"
    lines+="$mn $hr * * 1-5 cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"$'\n'
  done
  printf '%s' "$lines"
}
SUMMER_LINES="$(build_cron summer)"
WINTER_LINES="$(build_cron winter)"
echo "  Summer (EDT) cron: $(printf '%s' "$SUMMER_LINES" | tr '\n' '; ')"
echo "  Winter (EST) cron: $(printf '%s' "$WINTER_LINES" | tr '\n' '; ')"

CRONTAB="$(command -v crontab || echo /usr/bin/crontab)"
MKTEMP="$(command -v mktemp || echo /usr/bin/mktemp)"
GREP="$(command -v grep || echo /bin/grep)"

# Install six cron lines (3 runs x 2 seasons). We filter out old copies by
# matching "news_updater.py" (the marker unique to our jobs) - matching the
# script name is the reliable way to remove all previous copies.
TMPCRON="$("$MKTEMP")"
"$CRONTAB" -l 2>/dev/null | "$GREP" -v 'news_updater.py' > "$TMPCRON" || true
printf '%s%s' "$SUMMER_LINES" "$WINTER_LINES" >> "$TMPCRON"
if "$CRONTAB" "$TMPCRON"; then
  echo "  ✅ Cron jobs installed (user crontab)."
else
  echo "  ⚠️  User crontab failed - trying /etc/cron.d/ instead..."
  # Fallback: install a system cron file (needs root). Format is the same
  # as a crontab but with the username field inserted after the schedule.
  USERNAME="$(id -un 2>/dev/null || echo root)"
  printf '%s' "$SUMMER_LINES" | sed "s/^/$USERNAME /" > /tmp/news-summer
  printf '%s' "$WINTER_LINES" | sed "s/^/$USERNAME /" > /tmp/news-winter
  sudo mv /tmp/news-summer /etc/cron.d/news-summer 2>/dev/null || true
  sudo mv /tmp/news-winter /etc/cron.d/news-winter 2>/dev/null || true
  if sudo test -f /etc/cron.d/news-summer; then
    echo "  ✅ Cron jobs installed (/etc/cron.d/)."
    rm -f "$TMPCRON"
  else
    echo "  ❌ FAILED to install the cron jobs (both methods returned an error)."
    echo "     crontab path: $CRONTAB"
    echo "     Try manually: $CRONTAB $TMPCRON"
    exit 1
  fi
fi
rm -f "$TMPCRON"

echo "  Installed job(s):"
"$CRONTAB" -l 2>/dev/null | "$GREP" 'news_updater.py' || echo "  (user crontab: none)"
for f in /etc/cron.d/news-summer /etc/cron.d/news-winter; do
  sudo test -f "$f" && { echo "  $f:"; sudo cat "$f"; }
done

# --- Run once to confirm it works
echo "  Running news_updater.py once to test..."
python3 "$MONITOR"

echo ""
echo "=============================================="
echo " DONE! The news updater runs three times a day, pinned to US market time:"
echo "   Run 1: 9:15 ET (15 min before the 9:30 ET open)"
echo "   Run 2: 16:45 ET (just after the 16:00 ET close)"
echo "   Run 3: 23:00 ET (Beijing noon - catches the Chinese MORNING news)"
echo " It auto-adjusts for summer (EDT) and winter (EST)."
echo ""
echo " To check it's working:"
echo "   cat $LOG"
echo ""
echo " To see the schedule:"
echo "   crontab -l | grep news_updater"
echo ""
echo " To stop it (if you ever need to):"
echo "   crontab -l | grep -v news_updater | crontab -"
echo "=============================================="
