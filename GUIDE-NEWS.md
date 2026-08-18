# 📰 PortfolioNewsUpdater — setup

Searches your tickers for **new** SEC filings, **Chinese** news (Google News
zh-CN, Eastmoney, Sina/Eastmoney 7x24 wires, Tavily), English news, and RSS
items **three times a day**. Everything found is stored in a SQLite news
database (rolling ~3 weeks), translated to English and scored 1–10 by
**DeepSeek AI**; only the **top items by importance** are pushed to your
Telegram. Runs on the same **free** Google Cloud VM as your price monitor,
so it costs **$0** for VM/network/storage.

## Schedule (pinned to US market time, DST-aware)

| Run | Time (US Eastern) | Why |
|-----|-------------------|-----|
| 1 | **9:15 ET** | 15 minutes before the 9:30 ET market open |
| 2 | **16:45 ET** | just after the 16:00 ET close |
| 3 | **23:00 ET** | **Beijing noon** — catches the Chinese **morning** news burst (alpha breaks 9:00–12:00 Beijing time; run 2 misses it entirely) |

The installer puts cron jobs on the VM for both DST seasons and a tiny
DST-aware guard inside `news_updater.py` makes the out-of-season jobs an
instant no-op, so exactly **three real runs happen per day**.

## What it checks (per ticker, per run — only the "delta" since last time)

1. **SEC EDGAR** filings — 8-K/8-K/A, 10-Q, 10-K, **6-K/6-K/A** (the
   workhorse for Chinese ADRs), 20-F, F-1, 424B3/424B4, DEF 14A, **Form 3/4**
   (insider ownership), 144, SCHEDULE 13D/13G + amendments, SC 13E-3/13E-4
   (going-private), **25** (delisting), 13F-HR. For 6-K and 8-K the updater
   reads the filing text and extracts what actually happened (the "INFORMATION
   CONTAINED IN THIS REPORT" paragraph / the 8-K Item codes) so the AI scores
   substance, not just the form name. (Chinese ADRs are foreign private
   issuers: they file 6-K/20-F, not 8-K/10-K, and don't file Form 4 — form 3
   catches new insiders.)
2. **Google News zh-CN** — searches the company's **Chinese name and its
   subsidiaries** (e.g. LX → `乐信 OR 分期乐 OR 桔子理财`)
3. **Eastmoney (东方财富)** — Chinese financial news search API, one query per
   Chinese name/subsidiary
4. **Eastmoney / Sina 7x24 fast-news wires** — the real-time Chinese
   financial "tape" (东财快讯 + 新浪7x24). Fetched once per run, filtered by
   each ticker's Chinese names/subsidiaries — this is where the alpha breaks
   first, before search engines even index it
5. **Baidu News** — best-effort Chinese news search (off by default: Baidu
   serves a CAPTCHA to server IPs, so it rarely returns anything — you can
   re-enable it via `sources.baidu`)
6. **Tavily** — agent-grade news search (free plan: 1,000 credits/month;
   budgeted hard: max 15/day, max 850/month, and skipped entirely for a
   ticker when the free sources already found enough items that run)
7. **Google News EN** — kept as a low-priority backup, but now also searches
   the company's English brand names (e.g. Fenqile, Temu)
8. **Google News `site:`** — searches the company's OWN discovered websites
   (official site, news page, subsidiary sites) so official announcements and
   press releases are caught even when no news outlet covers them
9. **Company RSS / press-release feeds** (optional, per-ticker)

All new items are stored in `news.db`, translated + summarized + scored by AI
(one batched call per ticker to keep token usage tiny), then the **top-N by
importance** (Chinese-language items tie-break higher) are pushed to Telegram.
The AI also writes a short **impact sentence** ("what this means for the
company's business or stock") — shown in the digest with a → arrow, and stored
with each item. Everything else stays in the DB — browse it from the panel
(Step 5).

## 📢 China Macro watch — the "I HAVE TO KNOW" tier

Huge policy / market news — **rate cuts (降息/降准/LPR), stimulus (万亿/特别国债),
assisted-loan regulation (助贷/消费金融/利率上限), 金融监管总局 actions, 中概股
moves, tariffs/sanctions** — is caught in real time from the 7x24 wires plus a
macro Google News query and pushed in its **own digest section at the top**,
always (never floor-capped), capped at `macro_max_per_run` (default 3).

**Cost:** the gate is a **free regex** — no AI is spent filtering. Only the
few matched items (typically 0–3 per run) get ONE tiny batched AI call that
translates and scores **impact on your fintech names (奇富科技 QFIN, 乐信 LX,
陆金所 LU)**. Set `macro_translate: false` for zero AI on macro entirely (raw
Chinese headline + English tag like `[RATE CUT]`). Add your own watchwords via
`macro_keywords` (e.g. `["房地产新政", "平台经济"]`).

## Company lookup & auto-discovery (the "alpha" config)

Real alpha is never on the ticker symbol — it's on the **subsidiaries**
(e.g. LX → 分期乐/Fenqile, Indonesia companies). The updater keeps a company
knowledge base in `company_lookup.json` (on the server):

- Each **configured** ticker's profile: `name_zh`, `name_en`, `aliases_zh`,
  `subsidiaries_zh`, `subsidiaries_other` (non-Chinese brands),
  **`website` / `news_url` / `subsidiary_websites`** (official sites — used
  for `site:` news searches), and `keywords`.
- **Whenever a new stock is added to the configuration**, the updater checks
  the lookup; if the ticker isn't there (or the profile is stale/too sparse),
  it **searches the web and populates it automatically** (Tavily reference
  lookup → AI extraction → written back), then runs the news search with the
  discovered names, subsidiaries and websites.
- **How often**: every **`lookup_refresh_days`** (default **30** — monthly)
  each ticker's profile is re-searched for new subsidiaries (~1–2 Tavily
  searches + 1 AI call per ticker per month). New tickers are discovered on
  their first run; a failed lookup retries within ~a week.
- **New-subsidiary alert**: when discovery finds names the lookup didn't
  have (e.g. Temu, LU Global, a Hong Kong broker), you get a **Telegram
  alert** so you know your searches just expanded.
- **Step 6 in the panel** shows the live lookup (including websites) and has
  a **"Re-discover subsidiaries now"** button to force an immediate re-search.
  Step 3 is your explicit override (`ticker_meta`) — it always wins over
  discovered data. Both views stay in sync on the next run.

## One-time setup

1. **Install the Google Cloud CLI** once:
   `https://cloud.google.com/sdk/docs/install` — then reopen your terminal.

2. **Start the panel** (double-click `start_cloud.bat`, or `python cloud_manager.py`).
   Open **`http://localhost:8001`**.

3. In the panel:
   - **Connect to Google** → Authenticate.
   - **Your server** → **Create/update free server** (creates the free
     e2-micro VM; re-runs safely if it already exists).
   - **Configuration** → add tickers, pick AI provider, paste Telegram +
     AI keys, and paste your free **Tavily key** (optional but recommended).
   - **Chinese names (ticker_meta)** → per-ticker JSON with `name_zh`,
     `aliases_zh`, `subsidiaries_zh`. **This is where the Chinese edge comes
     from.** Example:
     ```json
     {
       "LX": {
         "name_zh": "乐信",
         "name_en": "LexinFintech Holdings",
         "aliases_zh": ["乐信集团"],
         "subsidiaries_zh": ["分期乐", "桔子理财"]
       }
     }
     ```
   - **Push mode**: `all` pushes the top-N by AI importance; `score` only
     pushes items ≥ the min score. **Both modes apply the importance floor**
     (`push_min_importance`, default 4) and the per-ticker cap.
     `max_digest_items` caps the digest.
   - **Upload config to server**, then **Run now (test)**, then check
     **Stored news (Step 5)** to see everything it found.

## No-spam behavior

- Exact dedup (SQLite hash per source+URL).
- **Precision filters**: every source result must really mention one of the
  ticker's Chinese names/subsidiaries (with a smarter matcher that rejects
  coincidental substrings like 元保 inside 元保险 / 美元保证金) — Tavily,
  which previously passed through unrelated filler (Heineken buybacks for
  LX), is filtered too.
- **Semantic dedup folded into the analysis call**: the per-ticker AI call
  also sees the recent `seen` history and marks recycled / same-event stories
  (`known_event`) — no separate dedup call, so **half the AI calls** per run.
- **Recycled news**: news rows and dedup hashes are both kept ~21 days
  (`news_retention_days` / `seen_retention_days`). Inside the window a
  re-publication is recognized and not re-pushed; after the window it is
  treated as fresh again and reaches you — the behavior you asked for.
- **Importance floor** (`push_min_importance`, default 4): nothing below it is
  pushed, even in "push everything" mode — kills ⭐1–3 noise.
- **AI veto**: the AI's per-item `push` flag is honored — it can keep an item
  stored-only.
- **Per-ticker cap** (`push_max_per_ticker`, default 3): one ticker can't eat
  every digest slot while another name has news (relaxes when it's the only
  name with news).
- **Regulatory force-push**: headlines containing subsidiary-penalty /
  regulatory keywords (处罚/罚款/立案/约谈/退市… or delisting/fraud/
  investigation) get boosted to ⭐8+ so they're never buried by generic
  scoring — this is the real alpha.
- Chinese-language news about the company or its **subsidiaries** is weighed
  heavily by the AI.
- Digests are **split into ≤4,000-char messages** so long Google News URLs
  never make Telegram drop the whole digest.

## Rolling database (always ~3 weeks of news)

Every run prunes the DB:
- `news` rows older than **`news_retention_days`** (default **21**) are deleted
  — the DB always holds about three weeks of news + useful data.
- `seen` dedup hashes are kept the same length (**`seen_retention_days`**,
  default **21**), so a recycled re-publication is treated as fresh again
  after the window — while semantic dedup still stops re-pushes *inside* it.
- `VACUUM` runs automatically when the DB file passes 50 MB.

## Tavily budget (free plan: 1,000 searches/month)

1 basic search = 1 credit. The updater budgets hard so you can never blow it:
- **Daily cap** `tavily_max_daily_searches` (default **15**) → at most ~450/month.
- **Monthly cap** `tavily_max_monthly_searches` (default **850**) → insurance.
- **Adaptive skip**: if GoogleNews zh + Eastmoney + Baidu already found
  `tavily_min_free_items` (default **4**) new items for a ticker in a run,
  Tavily is skipped for it entirely.
- **Discovery** uses Tavily only for new/stale tickers (a few one-time searches).

Typical usage: ~5–15 searches/day (~150–300/month), leaving most of the free
allowance unused. Usage is tracked in `tavily_usage.json`.

## CLI modes (for testing / the panel)

```bash
python3 news_updater.py --force           # run now (bypass schedule guard)
python3 news_updater.py --force --dry-run  # run but DON'T send Telegram; print digest
python3 news_updater.py --force --dry-run --no-write  # full test: no DB/lookup/history writes, no Tavily credits used
python3 news_updater.py --dump-news       # print stored news as JSON (panel browse)
python3 news_updater.py --dump-news=LX    # ... only for LX
python3 news_updater.py --dump-lookup     # print the company lookup as JSON (panel)
python3 news_updater.py --dump-usage      # print Tavily usage counters (panel meter)
python3 news_updater.py --rediscover      # force re-discovery of ALL tickers now
python3 news_updater.py --rediscover=LU   # ... only for LU
```

## Important security note

Your **AI key**, **Telegram token**, and **Tavily key** live in
`secrets_local.json`, which is **git-ignored** — never commit or share it.
Your **tickers/holdings** live in `config_local.json` (also git-ignored; the
repo ships a blank `config_local.example.json` template). Runtime data —
`news.db`, `company_lookup.json`, `tavily_usage.json` — is git-ignored too.

## Stopping it (if you ever need to)

```bash
crontab -l | grep -v news_updater | crontab -
```

## Troubleshooting

| Problem | What to do |
|---------|------------|
| "Schedule not installed" | Click **Upload config to server**, then **Check schedule**. |
| New stock added but no Chinese news appears | Check Step 6 (Company lookup) after the next run — the updater auto-discovers the company's Chinese names/subsidiaries and searches them. You can also add them manually in the Step 3 JSON (overrides always win). |
| No digest arrives | Click **Run now (test)** and read the output. Check Telegram + AI keys. |
| Tavily not used | Add your Tavily key (free tier) in the panel and re-upload; the run log shows `Tavily daily/monthly cap reached` or `skipping Tavily (saving credits)` when it's budget-skipped. |
| Too many/too few items | Adjust `max_items_per_run` / `max_digest_items`, or switch `push_mode` to `score` and raise `push_min_score`. |
| Out-of-season run skipped | Expected — the DST guard makes the wrong-season cron job a fast no-op. |
| Manual run skipped | The panel's **Run now (test)** always forces a run (`--force`). |

## How the schedule stays reliable across DST

- **Six cron jobs** are installed (three runs × two seasons), firing at the
  correct UTC times for 9:15, 16:45 and 23:00 ET in summer (EDT) and winter
  (EST).
- `news_updater.py` has a **DST-aware guard** that skips the out-of-season
  jobs instantly, so exactly **three** real runs per day.
- The installer also enables the cron daemon at boot and installs Python deps
  with `sudo` (so cron can import them).
