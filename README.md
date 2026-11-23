# 📰 PortfolioNewsUpdater

A personal stock-news monitor that actually finds the news that matters for
**US-listed Chinese companies** (and any US/HK watchlist). It combines
**ticker-scoped primary feeds** (**SEC EDGAR official JSON** with exact
to-the-second filing timestamps and 6-K/8-K exhibit text, **official HKEX
filings** for dual-listed ADRs like QFIN `03660.HK` / LU `06623.HK` and HK
stocks, **Eastmoney 股吧/Guba**, **Yahoo Finance** & **Seeking Alpha** ticker
RSS) with **Chinese-language search & wires** (per-name Google News zh-CN/HK,
the **Eastmoney + Sina 7x24 real-time news wires**, **EXA neural search**,
Tavily, and **DeepSeek Search** — DeepSeek's own Chinese index) using each
company's **Chinese name, discovered brands, and subsidiaries** (e.g. LX →
乐信/分期乐/Fenqile, plus their official websites). Everything found is stored
in a small database, entity-verified, translated and scored with AI (with a
"what this means" impact sentence), and only the most important items are pushed
to Telegram — **three times a day, Monday to Friday**, timed for both markets:
**00:00 ET** (the Chinese news morning — 12:00–13:00 Beijing), **09:15 ET**
before the US open and **17:00 ET** an hour after the close, all deliberately
placed **outside DeepSeek's peak-priced hours** so every AI call costs half
price. (Weekends are skipped — cron is Mon–Fri.)

**Relevance first:** every item passes both mechanical company-mention /
homonym guards (blocking collisions like South Korea's LX Semicon / LX Pantos
for `LX` or surname "Lu" for `LU`, plus law-firm claim-farming and quote-page
templates) **and** an AI entity-relevance check in ticker mode. Recycled
coverage of an event you already have and cross-outlet copies of the same story
are dropped or collapsed before they can reach you.

**Any watchlist, not just this one:** each ticker's Chinese name, English short
form (including compound stems like `LexinFintech` → `Lexin`), discovered
brands, and `aliases_en` are used to match headlines, so a legal name like
"Toyota Motor Corporation" still matches "Toyota". Swapping the whole portfolio
works — with Chinese/HK names the Chinese sources + HKEX carry it; for US names
you get SEC + Yahoo Finance + Seeking Alpha + Google News + Tavily and the
China-macro tier switches itself off. See **Swapping the whole watchlist** in
GUIDE-NEWS.md for the checklist.

**Two ways to use it:** the scheduled watchlist above; and the panel's
**🔎 Deep search** tab for a one-off look at *any* ticker — it discovers that
company's brands on the fly, searches the last 1–6 months, scores each article,
and gives a short bullish/bearish read. **It saves nothing.**

**The edge:** an official **HKEX** earnings-date, buyback, or inside-information
filing hours before US markets open, a penalty to 分期乐, an official-site
announcement, a flash item on the Chinese 7x24 wire, a **Guba** Chinese
breakdown of a filing, a **DeepSeek Search** rolling corporate-action find — or
a **huge China macro move (rate cuts, stimulus, assisted-loan regulation)
pushed in a dedicated 📢 CHINA MACRO section** — the stuff US English media
never covers, delivered hours earlier. (Per-ticker EXA is **off** by default:
it cost a credit per ticker per run and almost never returned an item about the
company. EXA is still used for the macro tier and for company discovery.)

- Everything runs on a **free** Google Cloud e2-micro VM ($0).
- Cost is **under $1/month** (see below): free sources + a tiny DeepSeek Search
  bill ($0.75 per 1,000 *successful* searches; empty results are free). No
  spam: company-relevance gate, age gate,
  same-story collapse, a per-event guard (one earnings release reaches you
  once, however many outlets cover it), importance floors, per-ticker seat
  allocation, and regulatory news force-pushed.

---

## What your friend needs (all free or near-free)

| Item | Cost | How |
|------|------|-----|
| Google account + Google Cloud free tier | **$0** | console.cloud.google.com — accept terms, enable billing (free tier never charges; new accounts get $300 trial credit too) |
| Google Cloud CLI (on the Windows PC) | **$0** | https://cloud.google.com/sdk/docs/install |
| Telegram bot token + chat id | **$0** | @BotFather creates the bot; @userinfobot gives your chat id |
| Tavily API key | **$0** | tavily.com free plan = 1,000 searches/month (the app budgets itself: max 30/day, 900/month) |
| EXA AI key | **$0** | exa.ai free plan ≈ 1,000 semantic searches/month (the app budgets itself: max 32/day, 980/month) |
| DeepSeek Search | **≈$0.30–0.80/month** | No extra key: it uses your DeepSeek AI key. $0.75 per 1,000 **successful** searches; empty results are free. Budgeted at 100/day, 2,000/month in the panel. Fan-out: news + corporate actions + HK code + one rotating paper |
| AI key — **DeepSeek (only paid item) OR Gemini free tier** | **$0–$2** | DeepSeek: platform.deepseek.com (tiny top-up, lasts months). **Gemini: aistudio.google.com/apikey → free tier is enough.** Pick it in the panel — AI provider → Gemini. *(Gemini users: the Search source needs a DeepSeek key too; paste one in the panel's "DeepSeek Search key" field or leave it off.)* |

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
     enables Compute Engine automatically). You only need this to create the VM
     or repair it — **Upload config to server** already reinstalls the schedule.
   - **🧩 Configuration** — add **your** tickers, pick **Gemini (free tier)** or
     DeepSeek, paste your Telegram bot token/chat id, your Tavily key and your
     AI key, then **Upload config to server** (a live status line and the command
     output are shown right there, since it also runs the setup script on the VM
     and takes a few minutes) and **Run now (test)**.
     The names/subsidiaries editor lives here as well.
   - **📰 News & data** — the stored-news browser, the run history/schedule and
     the company lookup.
6. Check the stored news and the company lookup — the
   updater auto-discovers each company's Chinese name, subsidiaries and
   websites, and alerts you on Telegram when it finds new ones. In Step 5 every
   stored row has an ✕ to delete it (with confirmation) if it is not useful.

**Opening the hosted news page:** set `web_url` in the panel's Configuration tab
once, then use the **🌐 Open news web app** button at the top of the panel — or
double-click **`open_web.cmd`**. The address is also kept on its own line in
`web_url.txt` and is the **first key** of `config_local.json`, so it is easy to
find without digging through the config.

> Your personal `config_local.json` (tickers/holdings) is created by the
> panel on first upload and is **git-ignored — never committed**. For local
> CLI testing, copy `config_local.example.json` → `config_local.json` and
> edit it.

Full details, troubleshooting and all knobs: **GUIDE-NEWS.md**.

---

## Cost & sustainability

- **VM / network / storage: $0** (Google Cloud Always-Free e2-micro).
- **Tavily: $0** — free plan, hard-budgeted by the app (meter in the panel).
- **DeepSeek Search: ≈$0.30–0.80/month** — 100/day and 2,000/month caps, and a
  search that returns no results is **free**. It runs a small fan-out per
  ticker (news, corporate actions, HK code, one rotating paper), plus one macro
  query, so it keeps working when Tavily/EXA are capped.
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
- **The hosted archive is an email allowlist**, not "any Google account":
  `web_allowed_emails` is published with the sync and enforced by
  [firestore.rules](firestore.rules). Empty means nobody can read it — fail
  closed. See SETUP-WEB.md step 9 before deploying the rules.
- Run `--dry-run` freely: it makes **no paid calls at all** (no Tavily, EXA or
  DeepSeek requests) and writes nothing.
