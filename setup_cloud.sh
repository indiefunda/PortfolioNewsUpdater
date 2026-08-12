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
#   2. Installs a cron job that runs news_updater.py twice a day
#   3. Runs news_updater.py once to confirm it works
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

# --- 3. Set up the 2x-daily cron job
echo "[3/3] Setting up the 2x-daily schedule (cron)..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
MONITOR="$PROJECT_DIR/news_updater.py"
LOG="$PROJECT_DIR/news_updater.log"
PYTHON="$(command -v python3)"

CRONTAB="$(command -v crontab || echo /usr/bin/crontab)"
MKTEMP="$(command -v mktemp || echo /usr/bin/mktemp)"
GREP="$(command -v grep || echo /bin/grep)"

# Run twice a day: 08:00 UTC and 20:00 UTC (04:00 / 16:00 ET).
CRON_LINE="0 8,20 * * * cd $PROJECT_DIR && $PYTHON $MONITOR >> $LOG 2>&1"

TMPCRON="$("$MKTEMP")"
"$CRONTAB" -l 2>/dev/null | "$GREP" -v 'news_updater.py' > "$TMPCRON" || true
echo "$CRON_LINE" >> "$TMPCRON"
if "$CRONTAB" "$TMPCRON"; then
  echo "  ✅ Cron job installed (2x daily)."
else
  echo "  ❌ FAILED to install cron job. Try: $CRONTAB $TMPCRON"
  exit 1
fi
rm -f "$TMPCRON"

echo "  Cron: $CRON_LINE"
echo "  Installed job(s):"
"$CRONTAB" -l 2>/dev/null | "$GREP" 'news_updater.py' || echo "  (not found)"

# --- Run once to confirm it works
echo "  Running news_updater.py once to test..."
python3 "$MONITOR"

echo ""
echo "=============================================="
echo " DONE! The news updater will run at 08:00 and 20:00 UTC daily."
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
