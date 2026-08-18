# 📰 PortfolioNewsUpdater

A personal stock-news monitor that actually finds the news that matters for
**US-listed Chinese companies**. It searches **Chinese-language sources**
(Google News zh-CN, 东方财富/Eastmoney, the **Eastmoney + Sina 7x24
real-time news wires**, Tavily) using each company's **Chinese name and its
subsidiaries** (e.g. LX → 乐信/分期乐/Fenqile, plus their official websites),
stores everything in a small database, translates + scores it with AI (with a
"what this means" impact sentence), and pushes only the most important items
to Telegram — **three times a day**, pinned to US market time (DST-aware),
including a **23:00 ET run that catches the Chinese morning news burst**.

**The edge:** a penalty to 分期乐, a HK subsidiary's license news, an
official-site announcement, or a flash item on the Chinese wire — the stuff US
English media never covers, delivered hours earlier.

- Everything runs on a **free** Google Cloud e2-micro VM ($0).
- Cost is **$0/month** (see below). No spam: importance floors, per-ticker
  caps, AI dedup of recycled news, regulatory news force-pushed.

---

## What your friend needs (all free)

| Item | Cost | How |
|------|------|-----|
| Google account + Google Cloud free tier | **$0** | console.cloud.google.com — accept terms, enable billing (free tier never charges; new accounts get $300 trial credit too) |
| Google Cloud CLI (on the Windows PC) | **$0** | https://cloud.google.com/sdk/docs/install |
| Telegram bot token + chat id | **$0** | @BotFather creates the bot; @userinfobot gives your chat id |
| Tavily API key | **$0** | tavily.com free plan = 1,000 searches/month (the app budgets itself: max 15/day, 850/month) |
| AI key — **DeepSeek (only paid item) OR Gemini free tier** | **$0–$2** | DeepSeek: platform.deepseek.com (tiny top-up, lasts months). **Gemini: aistudio.google.com/apikey → free tier is enough.** Pick it in the panel — AI provider → Gemini |

---

## Setup (10 minutes)

1. **Get the code**: `git clone https://github.com/indiefunda/PortfolioNewsUpdater`
2. **One-time Google step**: open https://console.cloud.google.com once with
   your account, accept the terms and create/enable a project + billing
   (the Always-Free tier keeps it $0).
3. **Install the Google Cloud CLI** and reopen your terminal.
4. **Start the panel**: double-click `start_cloud.bat` (or
   `python cloud_manager.py`), open **http://localhost:8001**.
5. In the panel:
   - **Connect to Google** → Authenticate (browser login).
   - **Your server** → **Create/update free server** (creates the free VM,
     enables Compute Engine automatically).
   - **Configuration** → add **your** tickers, pick **Gemini (free tier)** or
     DeepSeek, paste your Telegram bot token/chat id, your Tavily key and
     your AI key.
   - **Upload config to server**, then **Run now (test)**.
6. Check **Step 5 (stored news)** and **Step 6 (company lookup)** — the
   updater auto-discovers each company's Chinese name, subsidiaries and
   websites, and alerts you on Telegram when it finds new ones.

> Your personal `config_local.json` (tickers/holdings) is created by the
> panel on first upload and is **git-ignored — never committed**. For local
> CLI testing, copy `config_local.example.json` → `config_local.json` and
> edit it.

Full details, troubleshooting and all knobs: **GUIDE-NEWS.md**.

---

## Cost & sustainability

- **VM / network / storage: $0** (Google Cloud Always-Free e2-micro).
- **Tavily: $0** — free plan, hard-budgeted by the app (meter in the panel).
- **AI: $0 with Gemini free tier**, or ~$1–2/year with DeepSeek (the app
  makes ~1 AI call per ticker per run and caches everything in the DB).
- **Telegram: $0.**
- The DB self-cleans (rolling ~3 weeks) and never grows unbounded.

## Security

- Keys live in `secrets_local.json`, which is **git-ignored** — never commit
  or share it. Every user uses their own keys and their own VM.
- Your **personal config** (`config_local.json` — tickers/holdings) is also
  **git-ignored**; the repo ships a blank `config_local.example.json`
  template instead.
- The control panel binds to `127.0.0.1` only (never exposed to the network).
