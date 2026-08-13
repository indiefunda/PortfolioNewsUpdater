# 📰 PortfolioNewsUpdater — setup

Searches your tickers for **new** SEC filings, news, and RSS items **twice a
day**, filters/dedupes/ranks them with **DeepSeek AI**, and sends a concise
Telegram digest. Runs on the same **free** Google Cloud VM as your price
monitor, so it costs **$0** for the VM/network/storage (only DeepSeek's tiny
API fee, ~$0.01–0.05/month).

## Schedule (pinned to US market time, DST-aware)

The digest runs **twice a day**, tied to the US stock market's open/close and
**automatically adjusting for summer (EDT) and winter (EST)**:

| Run | Time (US Eastern) | Why |
|-----|-------------------|-----|
| 1 | **9:15 ET** | 15 minutes before the 9:30 ET market open |
| 2 | **16:45 ET** | 7.5 hours later, just after the 16:00 ET close |

Because cron fires at fixed UTC times but US Eastern shifts by 1 hour between
seasons, the installer puts **two** cron jobs on the VM (one per DST season).
A tiny DST-aware guard inside `news_updater.py` makes the out-of-season job an
instant no-op, so exactly **two real runs happen per day** — never more.

## What it checks (per ticker, per run — only the "delta" since last time)
- **SEC EDGAR** filings (8-K, 10-Q, 10-K, 6-K, 20-F, SCHEDULE 13D/13G, etc.)
- **Google News RSS** (works for any ticker, no config)
- **Company RSS / press-release feeds** (optional, per-ticker in config)

New items → SQLite dedup → **DeepSeek** dedup/filter/rank/translate → Telegram.

## One-time setup

1. **Install the Google Cloud CLI** once (if not already done):
   `https://cloud.google.com/sdk/docs/install` — then reopen your terminal.

2. **Start the panel** (double-click `start_cloud.bat`, or `python cloud_manager.py`).
   Open **`http://localhost:8001`**.

3. In the panel:
   - **Connect to Google** → Authenticate (your Google login opens).
   - **Your server** → click **Create/update free server** (uses your existing
     `stock-monitor` VM; just adds the news files + a 2x-daily cron job).
   - **Configuration** → add your tickers, pick the AI provider, and paste
     your Telegram bot token/chat id and **DeepSeek** (or Gemini) API key.
   - **Upload config to server**.
   - **Run now (test)** to confirm, then check **Schedule & run history**.

## Important security note

Your **AI API key** and **Telegram token** live in `secrets_local.json`, which
is **git-ignored** — never commit or share this file. If you share the app
with friends, each person uses their **own** keys and their own VM.

## Stopping it (if you ever need to)

```bash
crontab -l | grep -v news_updater | crontab -
```

## Troubleshooting

| Problem | What to do |
|---------|------------|
| "Schedule not installed" | Click **Upload config to server**, then **Check schedule**. |
| No digest arrives | Click **Run now (test)** — check the output for errors. Make sure Telegram + AI key are set. |
| "No AI API key" in output | Add your key in the panel (**Configuration**) and re-upload. |
| Too many/too few items | Adjust `max_items_per_run` / `max_digest_items` in config. |
| Out-of-season run skipped | Expected — the DST guard makes the wrong-season cron job a fast no-op so you only get 2 real runs/day. |
| Manual run skipped | The panel's **Run now (test)** always forces a run (`--force`), so you can test any time. |

## How the schedule stays reliable across DST

- **Two cron jobs** are installed, one for summer (EDT) and one for winter
  (EST), each firing at the correct UTC times for 9:15 ET and 16:45 ET.
- `news_updater.py` has a **DST-aware guard** that skips the out-of-season job
  instantly (before any DB/network work), so exactly **two** real runs per day.
- The installer also enables the cron daemon at boot and installs Python deps
  with `sudo` (so cron can import them) — the two most common silent causes of
  "it never runs unattended."
