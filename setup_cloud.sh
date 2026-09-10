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
#   2. Installs FOUR DST-aware cron jobs that run news_updater.py
#      twice a day, pinned to US market time:
#        Run 1  09:15 ET - 15 minutes before the 09:30 ET open
#        Run 2  17:00 ET - one hour after the 16:00 ET close
#   3. Runs news_updater.py once (--force --dry-run) to confirm it works
#
# Why a cron job per season? US Eastern time shifts by 1 hour between DST
# seasons, but cron fires at fixed UTC times. So we install one job
# per run per season:
#     Summer (EDT, UTC-4): 09:15 ET = 13:15 UTC, 17:00 ET = 21:00 UTC
#     Winter (EST, UTC-5): 09:15 ET = 14:15 UTC, 17:00 ET = 22:00 UTC
# news_updater.py carries a tiny DST-aware guard that makes the
# out-of-season job an instant no-op, so exactly two real runs happen
# per day. This is reliable because cron does the exact timing and the
# correct job is always already installed - no re-setup at DST flips.
#
# Both runs deliberately land OUTSIDE DeepSeek's peak-priced windows
# (01:00-04:00 / 06:00-10:00 UTC Mon-Fri, when API tokens cost DOUBLE):
# all four UTC times above are off-peak, so every AI call is half price.
# ============================================================

# NOTE on `set -e`: it is deliberately NOT set. This script runs unattended on
# every "Upload config to server" click, and the interesting failures (a package
# that will not install, a crontab that is not writable) are handled explicitly
# below with || true and clear messages. With `set -e` an unrelated late failure
# aborted the script AFTER cron was already installed, so the panel reported
# "❌ failed" for a deploy that had largely succeeded.
# ============================================================

echo "=============================================="
echo " PortfolioNewsUpdater - Cloud setup"
echo "=============================================="

# Resolve the project directory ONCE, before anything that needs it: the pip
# install used to reference requirements.txt relative to the caller's CWD while
# PROJECT_DIR was only defined later, so running the script from anywhere else
# failed to find it.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
MONITOR="$PROJECT_DIR/news_updater.py"
LOG="$PROJECT_DIR/news_updater.log"
PYTHON="$(command -v python3)"

# --- 1. Install Python + cron (idempotent - safe to run alongside the
#     price monitor's setup)
echo "[1/3] Installing Python and cron..."
sudo apt-get update -y || true
# tzdata is NOT optional: the updater and this script's own schedule block both
# need the IANA timezone database through zoneinfo. Debian 12 happens to ship
# it, but relying on that is fragile.
sudo apt-get install -y python3 python3-pip cron tzdata || true
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true
echo "  cron daemon: $(systemctl is-active cron 2>/dev/null || echo 'not running')"

# --- 2. Install Python dependencies system-wide (so cron can import them)
echo "[2/3] Installing Python packages (requests, feedparser, edgartools)..."
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  sudo python3 -m pip install --break-system-packages --upgrade pip 2>/dev/null \
    || sudo python3 -m pip install --upgrade pip || true
  sudo python3 -m pip install --break-system-packages -r "$PROJECT_DIR/requirements.txt" 2>/dev/null \
    || sudo python3 -m pip install -r "$PROJECT_DIR/requirements.txt" || true
else
  echo "  ⚠️  requirements.txt not found in $PROJECT_DIR - skipping dependency install."
fi
python3 -c "import requests, feedparser; print('  deps OK')" 2>/dev/null \
  || python3 -c "import requests; print('  (edgartools optional)')"

# --- 3. Set up the 2x-daily DST-aware schedule (cron)
echo "[3/3] Setting up the DST-aware 2x-daily schedule (cron)..."

# Remove any stale /etc/cron.d/ copies from an earlier run (they are not
# touched by the user-crontab cleanup below and could otherwise linger with
# an old or broken schedule).
sudo rm -f /etc/cron.d/news-summer /etc/cron.d/news-winter 2>/dev/null || true

# Compute both seasons' UTC firing times from the SAME source of truth as
# news_updater.py (America/New_York DST rules). This keeps the schedule
# correct for every year automatically. Runs (ET): 09:15 (15 min before the
# 09:30 open) and 17:00 (one hour after the 16:00 close).
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
    # Derive the year rather than hardcoding it: only the UTC OFFSET on that
    # date matters, so today's year is always correct (and it stops looking like
    # a bug in six months).
    dt = datetime(datetime.now().year, month, day, hour, minute, tzinfo=EASTERN)
    u = dt.astimezone(UTC)
    return u.strftime("%M"), u.strftime("%H")
# July 1 = EDT (summer), Jan 1 = EST (winter). Each run gets its OWN line
# because the minutes differ between runs.
RUNS = [(9, 15), (17, 0)]
for season, month, day in (("summer", 7, 1), ("winter", 1, 1)):
    for i, (h, m) in enumerate(RUNS):
        mn, hr = cron_fields(month, day, h, m)
        print(f"{season}_{i}_min={mn}")
        print(f"{season}_{i}_hour={hr}")
PYEOF
)"

# Build the cron lines for a season (one per run; minute/hour from the JSON).
# NOTE: the loop bound is derived from the RUNS list length (2 runs -> i in
# 0 1). A hardcoded "0 1 2" silently emitted a THIRD line with empty
# minute/hour fields, i.e. a malformed cron entry, on every install.
build_cron() {
  local season="$1" lines="" mn hr
  for i in $(seq 0 $(($(printf '%s\n' "$SCHEDULE_JSON" | grep -c "^${season}_[0-9]*_min=") - 1))); do
    mn="$(printf '%s\n' "$SCHEDULE_JSON" | grep "^${season}_${i}_min=" | cut -d= -f2)"
    hr="$(printf '%s\n' "$SCHEDULE_JSON" | grep "^${season}_${i}_hour=" | cut -d= -f2)"
    [ -n "$mn" ] && [ -n "$hr" ] || continue
    # Quote every path: a home directory or install path containing a space
    # would otherwise split the cron command into invalid arguments.
    lines+="$mn $hr * * 1-5 cd \"$PROJECT_DIR\" && \"$PYTHON\" \"$MONITOR\" >> \"$LOG\" 2>&1"$'\n'
  done
  printf '%s' "$lines"
}
SUMMER_LINES="$(build_cron summer)"
WINTER_LINES="$(build_cron winter)"
# NOTE: "$( ... )" strips trailing newlines, so re-terminate explicitly with
# printf '%s\n' - cron IGNORES (or warns on) a final line without a newline,
# which would silently drop the LAST run of each season (the 17:00 ET job).
echo "  Summer (EDT) cron: $(printf '%s' "$SUMMER_LINES" | tr '\n' '; ')"
echo "  Winter (EST) cron: $(printf '%s' "$WINTER_LINES" | tr '\n' '; ')"

CRONTAB="$(command -v crontab || echo /usr/bin/crontab)"
MKTEMP="$(command -v mktemp || echo /usr/bin/mktemp)"
GREP="$(command -v grep || echo /bin/grep)"

# Assert we actually built a schedule. build_cron() silently skips malformed
# pairs, so a failed SCHEDULE_JSON parse used to produce an EMPTY schedule that
# still got "installed" and reported as success - leaving the VM with no jobs.
SCHEDULE_COUNT="$(printf '%s\n%s\n' "$SUMMER_LINES" "$WINTER_LINES" | "$GREP" -c 'news_updater' || true)"
if [ "${SCHEDULE_COUNT:-0}" -lt 4 ]; then
  echo "  ❌ Built only ${SCHEDULE_COUNT:-0} cron line(s); expected 4 (2 runs x 2 seasons)."
  echo "     The schedule could not be derived - nothing was installed."
  exit 1
fi
echo "  Built $SCHEDULE_COUNT cron line(s) (expected 4)."

# Install four cron lines (2 runs x 2 seasons). We filter out old copies by
# matching "news_updater" (the marker unique to our jobs) - matching the
# script name is the reliable way to remove all previous copies. The pattern
# is a FIXED string: as a regex, "." would match any character.
TMPCRON="$("$MKTEMP")"
"$CRONTAB" -l 2>/dev/null | "$GREP" -vF 'news_updater' > "$TMPCRON" || true
printf '%s\n%s\n' "$SUMMER_LINES" "$WINTER_LINES" >> "$TMPCRON"
if "$CRONTAB" "$TMPCRON"; then
  echo "  ✅ Cron jobs installed (user crontab)."
else
  echo "  ⚠️  User crontab failed - trying /etc/cron.d/ instead..."
  # Fallback: install a system cron file. Format is the same as a crontab but
  # with the username field inserted after the schedule.
  #
  # `sudo mv` PRESERVES the file's owner, and Debian's cron refuses to run an
  # /etc/cron.d file that is not owned by root ("WRONG FILE OWNER"). The old
  # code used mv and then only checked that the file EXISTED, so it printed
  # "✅ Cron jobs installed" for a VM that would never run anything.
  USERNAME="$(id -un 2>/dev/null || echo root)"
  printf '%s\n' "$SUMMER_LINES" | sed "s|^|$USERNAME |" | sudo tee /tmp/news-summer >/dev/null
  printf '%s\n' "$WINTER_LINES" | sed "s|^|$USERNAME |" | sudo tee /tmp/news-winter >/dev/null
  sudo install -o root -g root -m 0644 /tmp/news-summer /etc/cron.d/news-summer
  sudo install -o root -g root -m 0644 /tmp/news-winter /etc/cron.d/news-winter
  rm -f /tmp/news-summer /tmp/news-winter
  if sudo test -f /etc/cron.d/news-summer \
     && [ "$(sudo stat -c '%U' /etc/cron.d/news-summer 2>/dev/null)" = "root" ]; then
    echo "  ✅ Cron jobs installed (/etc/cron.d/, owned by root)."
    rm -f "$TMPCRON"
  else
    echo "  ❌ FAILED to install the cron jobs (both methods returned an error)."
    echo "     crontab path: $CRONTAB"
    echo "     Try manually: $CRONTAB $TMPCRON"
    exit 1
  fi
fi
rm -f "$TMPCRON"

# Report what is actually installed, and whether the daemon will run it.
echo "  Installed job(s):"
"$CRONTAB" -l 2>/dev/null | "$GREP" -F 'news_updater' || echo "  (user crontab: none)"
for f in /etc/cron.d/news-summer /etc/cron.d/news-winter; do
  sudo test -f "$f" && { echo "  $f:"; sudo cat "$f"; }
done
if [ "$(systemctl is-active cron 2>/dev/null || echo unknown)" != "active" ]; then
  echo "  ⚠️  The cron daemon is NOT running - the schedule exists but will not fire."
  echo "     Fix: sudo systemctl enable --now cron"
fi

# --- Verify the deployment WITHOUT running the pipeline
#
# This script is executed on EVERY "Upload config to server" click. It used to
# end with `python3 news_updater.py --force --dry-run`, which is NOT safe:
# --dry-run suppresses only the Telegram message, while every database write is
# gated on --no-write. So that "test" marked the items it would have sent as
# pushed=1, wrote them into the repeat/event ledger, and spent Tavily/EXA
# credits - without ever sending anything. The next real run then reported
# "no new items" and those stories were suppressed for a week.
#
# A deploy must never consume news, so the pipeline is no longer run here. The
# import check below verifies the interpreter and dependencies (a syntax or
# import error would otherwise only surface at the next cron run), and the
# panel's "Run now (test)" button is the supported way to exercise the run -
# it has --snapshot so it always delivers something instead of silently eating
# the window.
echo "  Verifying the updater imports cleanly (no pipeline run, no state written)..."
if python3 -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('nu','$MONITOR'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)" 2>/tmp/news_import_err; then
  echo "  ✅ news_updater.py imports cleanly (deps present, no syntax errors)."
else
  echo "  ⚠️  news_updater.py failed to import:"
  sed 's/^/      /' /tmp/news_import_err | tail -5
  echo "      Fix the import error above - cron runs will fail the same way."
  IMPORT_FAILED=1
fi
rm -f /tmp/news_import_err

echo ""
echo "=============================================="
echo " DONE! The news updater runs twice a day, pinned to US market time:"
echo "   Run 1: 09:15 ET (15 min before the 09:30 ET open)"
echo "   Run 2: 17:00 ET (one hour after the 16:00 ET close)"
echo " Both runs sit outside DeepSeek's peak-priced hours (half-price AI)."
echo " It auto-adjusts for summer (EDT) and winter (EST)."
echo ""
echo " Nothing was fetched or sent by this setup - use the panel's"
echo " \"Run now (test)\" button if you want to exercise a real run now."
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

# Only exit non-zero for a real failure (the import check), so the panel does
# not report "❌ failed" for a deploy that installed the schedule correctly.
if [ "${IMPORT_FAILED:-0}" = "1" ]; then
  exit 1
fi
