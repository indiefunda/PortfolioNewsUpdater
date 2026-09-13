# 📰 PortfolioNewsUpdater

A personal stock-news monitor that actually finds the news that matters for
**US-listed Chinese companies**. It searches **Chinese-language sources**
(Google News zh-CN, 东方财富/Eastmoney, the **Eastmoney + Sina 7x24
real-time news wires**, **EXA neural search**, Tavily) using each company's
**Chinese name and its brands/subsidiaries** (e.g. LX → 乐信/分期乐/Fenqile, plus
their official websites), stores everything in a small database, translates + scores it with AI (with a
"what this means" impact sentence), and pushes only the most important items
to Telegram — **twice a day, Monday to Friday**, pinned to US market time
(DST-aware): **09:15 ET** before the open and **17:00 ET** an hour after the
close, both deliberately placed **outside DeepSeek's peak-priced hours** so
every AI call costs half price. (Weekends are skipped — cron is Mon–Fri.)

**Relevance first:** an item must really mention the company (name, alias,
subsidiary or brand) — sector news that never names it, recycled coverage of
an event you already have, and cross-outlet copies of the same story are
dropped or collapsed before they can reach you. A story about the insurance
industry is not a HUIZ alert.

**Two ways to use it:** the scheduled watchlist above; and the panel's
**🔎 Deep search** tab for a one-off look at *any* ticker — it discovers that
company's brands on the fly, searches the last 1–6 months, scores each article,
and gives a short bullish/bearish read. **It saves nothing.**

**The edge:** a penalty to 分期乐, a HK subsidiary's license news, an
official-site announcement, a flash item on the Chinese wire, a **semantic
EXA find** ("the Shenzhen-based insurer" → Huize) — or a **huge China macro
move (rate cuts, stimulus, assisted-loan regulation) pushed in a dedicated
📢 CHINA MACRO section** — the stuff US English media never covers, delivered
hours earlier. (Per-ticker EXA is **off** by default: it cost a credit per
ticker per run and almost never returned an item about the company. EXA is
still used for the macro tier and for company discovery.)

- Everything runs on a **free** Google Cloud e2-micro VM ($0).
- Cost is **$0/month** (see below). No spam: company-relevance gate, age gate,
  same-story collapse, a per-event guard (one earnings release reaches you
  once, however many outlets cover it), importance floors, per-ticker seat
  allocation, and regulatory news force-pushed.

---

## What your friend needs (all free)

| Item | Cost | How |
|------|------|-----|
| Google account + Google Cloud free tier | **$0** | console.cloud.google.com — accept terms, enable billing (free tier never charges; new accounts get $300 trial credit too) |
| Google Cloud CLI (on the Windows PC) | **$0** | https://cloud.google.com/sdk/docs/install |
| Telegram bot token + chat id | **$0** | @BotFather creates the bot; @userinfobot gives your chat id |
| Tavily API key | **$0** | tavily.com free plan = 1,000 searches/month (the app budgets itself: max 30/day, 900/month) |
| EXA AI key | **$0** | exa.ai free plan ≈ 1,000 semantic searches/month (the app budgets itself: max 32/day, 980/month) |
| AI key — **DeepSeek (only paid item) OR Gemini free tier** | **$0–$2** | DeepSeek: platform.deepseek.com (tiny top-up, lasts months). **Gemini: aistudio.google.com/apikey → free tier is enough.** Pick it in the panel — AI provider → Gemini |

---

## Setup (10 minutes)

1. **Get the code**: `git clone https://github.com/indiefunda/PortfolioNewsUpdater`
2. **One-time Google step**: open https://console.cloud.google.com once with
   your account, accept the terms and create/enable a project + billing
   (the Always-Free tier keeps it $0).
3. **Install the Google Cloud CLI** and reopen your terminal.
4. **Start the panel**: double-click `start_cloud.bat` (or
   `python cloud_manager.py`). It opens the panel in your browser
   (**http://localhost:8001**; if that port is taken it uses 8002/8003 and the
   launcher opens the right one — a second copy refuses to start, so there is
   never more than one panel).
5. In the panel — it is split into four tabs:
   - **🔎 Deep search** — a one-off scan of any ticker. Nothing is saved; this
     is the tab it opens on.
   - **⚙️ Setup & schedule** — **Connect to Google** → Authenticate, then
     **Your server** → **Create/update free server** (creates the free VM and
     enables Compute Engine automatically). The schedule and run history are
     here too.
   - **🧩 Configuration** — add **your** tickers, pick **Gemini (free tier)** or
     DeepSeek, paste your Telegram bot token/chat id, your Tavily key and your
     AI key, then **Upload config to server** and **Run now (test)**.
     The names/subsidiaries editor lives here as well.
   - **📰 News & data** — the stored-news browser and the company lookup.
6. Check the stored news and the company lookup — the
   updater auto-discovers each company's Chinese name, subsidiaries and
   websites, and alerts you on Telegram when it finds new ones. In Step 5 every
   stored row has an ✕ to delete it (with confirmation) if it is not useful.

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
- **The control panel is hardened:** every request must come from localhost
  (Host + Origin validated, mutating routes are POST-only) and `/api/config`
  returns masked placeholders, never your real keys. It binds to
  `127.0.0.1` only, so it is never exposed to the network.
- Run `--dry-run` freely: it makes **no paid calls at all** (no Tavily, no EXA)
  and writes nothing.
