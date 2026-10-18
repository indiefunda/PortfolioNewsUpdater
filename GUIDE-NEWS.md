# 📰 PortfolioNewsUpdater — setup

Searches your tickers for **new** SEC filings, **Chinese** news (Google News
zh-CN, Eastmoney, Sina/Eastmoney 7x24 wires, Tavily), English news, and RSS
items **twice a day**. Everything found is stored in a SQLite news
database (rolling ~3 weeks), translated to English and scored 1–10 by
**DeepSeek AI**; only the **top items by importance** are pushed to your
Telegram. Runs on the same **free** Google Cloud VM as your price monitor,
so it costs **$0** for VM/network/storage.

## Schedule (pinned to US market time, DST-aware)

| Run | Time (US Eastern) | UTC summer (EDT) | UTC winter (EST) |
|-----|-------------------|------------------|------------------|
| 1 | **09:15 ET** (15 min before the 09:30 open) | 13:15 | 14:15 |
| 2 | **17:00 ET** (one hour after the 16:00 close) | 21:00 | 22:00 |

All four UTC times sit **outside DeepSeek's peak-pricing windows**
(01:00–04:00 & 06:00–10:00 UTC Mon–Fri — tokens cost **double** there), so
**every AI call is billed at the half price** in both seasons.

A third run at **19:00 ET** (= 07:00 Beijing, the Chinese pre-open) was tried and
removed at the user's request. If you want it back it is one line in
`SCHEDULE_RUN_TIMES` plus a re-run of the installer; note that without it nothing
is read between 17:00 ET and 09:15 ET, so Chinese news breaking during the Asian
session is picked up by the pre-open run (the wires are paginated back to their
delta window, so it is captured — just hours later).

Which run a digest came from is printed in its header
(`📰 Portfolio News Digest (6 ticker(s)) · pre-open` / `· post-close`).

The installer puts cron jobs on the VM for both DST seasons and a tiny
DST-aware guard inside `news_updater.py` makes the out-of-season jobs an
instant no-op, so exactly **two real runs happen per weekday**. The old third run
(23:00 ET / Beijing noon) fired at 03:00–04:00 UTC — deep inside peak — and was
removed purely for AI cost.

> **Weekends are skipped.** The cron entries carry day-of-week `1-5`, so
> Saturday and Sunday get no runs at all. If you want a weekend run, change
> `1-5` to `*` in `setup_cloud.sh`'s `build_cron()` and re-run the installer.

## 🔎 Deep search — one-off look at ANY ticker (nothing is saved)

The panel's first tab is for a stock you do **not** track — a quick "what is
going on with this name lately?":

1. Type any ticker (e.g. `BABA`, `PDD`, `FUTU`) and pick a window (1 / 2.5 / 4 /
   6 months).
2. It **discovers that company's Chinese name, brands and overseas entities on
   the fly** — the same discovery the watchlist uses.
3. It deep-searches the window across Google News zh + EN, Eastmoney, Tavily and
   **DeepSeek Search** (its own Chinese index), drops anything unrelated, and
   collapses the same story from multiple sources.
4. Each article gets an ⭐ importance score, a **▲ positive / ▼ negative /
   ● neutral** marker, a category and a one-line "what this means".
5. Finally a short overall read: **bullish / bearish / neutral / mixed**, a
   confidence 1–10, the dominant themes and the strongest drivers.

**Nothing about a deep search is stored** — no database rows, no saved company
profile, no dedup memory, no Telegram, and no effect on your watchlist. That is a
property of the code path, not a setting. It is also available from the CLI:

```bash
python3 news_updater.py --adhoc-scan=FUTU        # default window
python3 news_updater.py --adhoc-scan=FUTU:120    # last 4 months
```

Cost when budgets are available: ~4–6 Tavily credits + one cheap DeepSeek
Search request + ~4 AI calls. If the Tavily daily cap is already reached it
falls back to the free sources (Google News zh + EN, Eastmoney) **and DeepSeek
Search** — still enough to find the company and its brands.

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
5. **EXA (neural search)** — *semantic* search: finds pages ABOUT the
   concept, not just containing the keywords. Used three ways: per-ticker
   **big/impact news**, the **macro tier** (semantic — "助贷的生死时刻" style
   coverage, no regex needed), and **company discovery** (finds related
   entities keyword search never surfaces, e.g. 陆金申华 for LU, Poni Insurtech
   for HUIZ). Free ~1,000 searches/month, hard-budgeted (max 32/day,
   980/month).
   **Per-ticker EXA news search is OFF by default** (`"exa_per_ticker": true` to
   turn it on). Measured over 616 stored EXA rows, 610 never mentioned the
   company: its neural query pulled in the whole sector, so a paid credit per
   ticker per run bought almost nothing. What still runs is EXA for the
   **macro tier** and for **discovery**, where it genuinely earns its keep.
6. **Baidu News** — best-effort Chinese news search (off by default: Baidu
   serves a CAPTCHA to server IPs, so it rarely returns anything — you can
   re-enable it via `sources.baidu`)
7. **Tavily** — agent-grade news search (free plan: 1,000 credits/month;
   budgeted hard: max 30/day, max 900/month, and skipped entirely for a
   ticker when the free sources already found enough items that run)
8. **DeepSeek Search** (paid, ~$0.30/month; budgeted: max 100/day,
   2,000/month) — DeepSeek's in-house search engine, the **only paid source
   with its own Chinese index**. Runs in parallel (one query per ticker, plus
   a macro query), so it keeps working when Tavily/EXA hit their caps. Chinese
   tickers query `<name> 新闻 after:<date> lang:zh`; English ones use the short
   name (`Toyota news after:…`). $0.75 per 1,000 **successful** searches —
   empty results are free. See the budget section below
9. **Google News EN** — kept as a low-priority backup, but now also searches
   the company's English brand names (e.g. Fenqile, Temu)
10. **Google News `site:`** — searches the company's OWN discovered websites
    (official site, news page, subsidiary sites) so official announcements and
    press releases are caught even when no news outlet covers them. Note: the
    news index covers publishers far better than corporate IR pages, so expect
    little from this source — real IR/newsroom **RSS** feeds (item 13) are the
    reliable way to catch a company's own announcements
11. **Primary regulator sources** (`regulators`, on by default) — the
    **NFRA (国家金融监督管理总局)**, **CSRC** and **PBoC** penalty announcements,
    reached through their published documents. These are the highest-signal
    items for lending and insurance names: a fine is news when the regulator
    publishes it, not when a portal writes it up. They land in the 📢 CHINA
    MACRO section
12. **Industry regulation by THEME** (`sector_regulation`, on by default) — the
    fix for sector-wide rules that name no issuer, which every company-name
    query misses: *"中互金协会：15家助贷平台提交专项审计报告"*, *"互联网贷款自律
    筹备组将扩容"*, *"消费金融公司迎来最严检查"*. Searched by theme (助贷 专项审计 /
    互联网贷款 自律 / 消费金融 监管 处罚 / 小额贷款 利率 新规 …) and admitted only
    when a **sector noun AND a policy/action word** are both present, so
    *"助贷概念股大涨"* or a product launch does not qualify. They feed the
    CHINA MACRO section, where the AI writes the impact line for your names
13. **Company RSS / press-release feeds** (optional, per-ticker — empty by
    default; add the company's newsroom feed to catch its own announcements)

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
moves, tariffs/sanctions** — is caught in real time from the 7x24 wires, a
macro Google News query, **EXA's semantic macro search**, **DeepSeek Search's Chinese macro query**, the
**primary regulator sources**, and **industry-theme regulation** (see the source
list above), and pushed in its **own digest section at the top**, always (never
floor-capped), capped at
`macro_max_per_run` (default 3).

**The AI is the filter, not a keyword list.** Each run sends up to
`macro_triage_pool_max` (default 60) candidates from the tape to ONE AI call that
answers a direct relevance question — *"could this change what I think about my
holdings or their sector?"* — with the reject categories spelled out (another
country's domestic news, geopolitics, industrial/energy pilots, index recaps
listing constituent moves, a single A-share company's announcement, a foreign
data point with no rate implication, central-bank small talk). Only items it
accepts are stored and scored, then **one row per event**: on a live run of 45
candidates, 33 were rejected and the remaining copies of the same event merged
into 2 rows. If the AI key is present but the response is unusable (an outage),
the old keyword gate screens the items and they are **stored only, never
pushed**; with **no AI key at all** the macro tier is skipped entirely. A
mechanical tape filter drops price-move listings that the model should never
have to read.

**Cost:** one call with ~50 items ≈ 3–4k tokens (~0.2¢ off-peak), against the
few hundred tokens the old 12-item call spent — the difference buys the removal
of roughly 30 stored junk rows per run. Set `macro_translate: false` for the
deliberate zero-AI path on macro (raw Chinese headline + English tag like
`[RATE CUT]`, stored and pushed under the strict keyword gate); note this only
works when an AI key exists, because no key means nothing is pushed anywhere.
Add your own watchwords via `macro_keywords` (e.g. `["房地产新政", "平台经济"]`).

**DeepSeek Search in the macro tier:** one extra Chinese query per run
(`中国 助贷 消费金融 监管 降准 降息 after:<date> lang:zh`) goes through the same
relevance test. It is keyword-driven (EXA is semantic), dated, and costs about
$0.00075 when it returns anything — the cheap backstop when EXA's credit is
gone. Toggle: `sources.deepseek_macro`.

## Company lookup & auto-discovery (the "alpha" config)

Real alpha is never on the ticker symbol — it's on the **subsidiaries**
(e.g. LX → 分期乐/Fenqile, Indonesia companies). The updater keeps a company
knowledge base in `company_lookup.json` (on the server):

- Each **configured** ticker's profile: `name_zh`, `name_en`, `aliases_zh`,
  **`aliases_en`**, `subsidiaries_zh`, `subsidiaries_other` (non-Chinese brands),
  **`website` / `news_url` / `subsidiary_websites`** (official sites — used
  for `site:` news searches), and `keywords`.
- **Whenever a new stock is added to the configuration**, the updater checks
  the lookup; if the ticker isn't there (or the profile is stale/too sparse),
  it **searches the web and populates it automatically** (Tavily reference
  lookup → AI extraction → written back), then runs the news search with the
  discovered names, subsidiaries and websites.
- **How often**: every **`lookup_refresh_days`** (default **30** — monthly)
  each ticker's profile is re-searched. Budget up to **~5 Tavily searches +
  ~3 EXA searches + ~3 AI calls per ticker** on a refresh: discovery runs two
  passes over different query angles (Chinese operations/brands, then overseas
  entities) and each pass asks the AI twice — once for the profile, once for a
  brand-only extraction. New tickers are discovered on their first run; a
  failed lookup retries within ~a week.
- **DeepSeek Search adds a Chinese angle** (`<name> 子公司 旗下品牌 业务 平台
  lang:zh`, plus an overseas pass). Measured on 乐信, its first results were the
  company's **own site pages** ("业务与产品", "集团简介") rather than registry
  duplicates. Discovery then uses the **Fetch API** to read that official site
  as Markdown (one call per discovery) before the brand-only AI pass, so brands
  can be grounded in the company's own words. Budget impact: ~2 searches + 1
  fetch per discovery, i.e. well under a cent.
- **Brands, not just registry names**: discovery deliberately prefers the short
  brand names that headlines use (分期乐, 平安普惠, Poni Insurtech, 富途牛牛) over
  legal entity names (深圳市分期乐网络科技有限公司), because only the former are
  useful as search terms. The term budget is 8, and short brand-length names are
  ranked first.
- **New-subsidiary alert**: when discovery finds names the lookup didn't
  have (e.g. Temu, LU Global, a Hong Kong broker), you get a **Telegram
  alert** so you know your searches just expanded.
- **The panel** shows the live lookup (including websites) and has a
  **"Re-discover subsidiaries now"** button to force an immediate re-search.
  The Configuration tab's names editor shows exactly what is being searched for
  each ticker — everything is merged (your config **plus** what discovery
  found). **Applying the editor makes that list authoritative**: delete a wrong
  auto-discovered name and it is removed from the search (an empty line clears
  the list). Without pressing Apply, list fields are unioned, so config entries
  only ever add.

## Swapping the whole watchlist

The app is built Chinese-first. It runs for any tickers, but the quality depends
on the market, so expect this when you replace the list:

| New holdings | What you get |
|---|---|
| **US-listed Chinese ADRs** | The full design: Chinese wires/portals, English + SEC, Chinese macro. |
| **HK-listed Chinese names** | Good: Chinese sources + English + SEC-less fundamentals; **no HKEX filings** (documented gap). |
| **US / UK / EU blue chips** | SEC (US only) + Google News EN + Tavily. English-name matching now derives the headline form, but add `aliases_en` for rebrands. The macro tier is China-specific and is **skipped automatically** when no holding has a Chinese name. |
| **Japan / other non-English, non-Chinese** | Thin: there is no `hl=ja`/`hl=de` edition, so only English and Chinese coverage is searched. |

Checklist for a swap:

1. **Set the new tickers**, then open **👁 Show what this ticker searches** for
   each one after the first run. If the derived names look wrong, edit the box
   and press **Apply** (that makes the list authoritative and removable).
2. **Add `aliases_en`** for anything whose press name differs from its legal
   name (Google/Alphabet, LVMH, Toyota).
3. **Turn off the Chinese-only sources** when the portfolio has no Chinese
   names: `eastmoney`, `eastmoney_724`, `sina_724`, `regulators`,
   `sector_regulation`. `macro_enabled` is inferred, but an explicit value in
   the config always wins.
4. **Delete stale `ticker_meta` entries** (the panel's JSON box keeps them
   unless you remove them). Upload now drops metadata for tickers that are no
   longer in the list, and the lookup ignores removed tickers, so this is a
   tidy-up rather than a correctness requirement.
5. **Smoke-test one new ticker with 🔎 Deep search** — a ticker you know is in
   the news. Zero or one article means the profile/matching needs an alias.
6. Old archives linger for the retention window (~21 days) and are labelled
   "(old)" on the hosted page; the repeat/event ledgers for removed tickers are
   ignored from the next run on.

## One-time setup

1. **Install the Google Cloud CLI** once:
   `https://cloud.google.com/sdk/docs/install` — then reopen your terminal.

2. **Start the panel** (double-click `start_cloud.bat`, or `python cloud_manager.py`).
   It opens the panel for you — **`http://localhost:8001`**, or 8002/8003 if
   that port is taken (the launcher reads the real URL from `panel_url.txt`).
   A second copy refuses to start, so you can never end up with two panels.

3. In the panel — it has four tabs: **🔎 Deep search**, **⚙️ Setup & schedule**,
   **📰 News & data**, **🧩 Configuration**:
   - **Setup & schedule** → **Connect to Google** → Authenticate.
   - **Setup & schedule** → **Your server** → **Create/update free server**
     (creates the free e2-micro VM; re-runs safely if it already exists).

     **After the VM exists you do not need this button again.** *Upload config to
     server* already copies the code **and re-runs `setup_cloud.sh` on the VM**,
     which reinstalls the cron schedule and the logrotate rule. Use
     *Create/update free server* only to create the VM, or to repair it (it also
     starts a stopped VM).
   - **Configuration** → add tickers, pick AI provider, paste Telegram +
     AI keys, and paste your free **Tavily key** (optional but recommended).
   - **Configuration** → **Chinese names (ticker_meta)** → per-ticker JSON with
     `name_zh`, `aliases_zh`, `aliases_en`, `subsidiaries_zh`. **This is where
     the alpha comes from.** The **👁 Show what this ticker searches** button
     fills in everything discovered so far plus your own entries. Example:
     ```json
     {
       "LX": {
         "name_zh": "乐信",
         "name_en": "LexinFintech Holdings",
         "aliases_zh": ["乐信集团"],
         "aliases_en": ["Lexin", "Fenqile"],
         "subsidiaries_zh": ["分期乐", "桔子理财"]
       }
     }
     ```
     `aliases_en` matters for non-Chinese stocks: the app derives the obvious
     short form ("Toyota" from Toyota Motor Corporation) but cannot guess a
     rebrand ("Google" for Alphabet Inc.) — that is what this field is for.
   - **Push mode**: `all` pushes the top-N by AI importance; `score` only
     pushes items ≥ the min score. **Both modes apply the importance floor**
     (`push_min_importance`, default 4) and the per-ticker cap.
     `push_min_score` is **only read in `score` mode** — the panel greys the box
     out in `all` mode for that reason. `max_digest_items` caps the digest.
   - **Upload config to server** — this is the one step that ships everything:
     it copies `news_updater.py` / `news_web.py` / your config + keys to the VM
     **and re-runs the setup script there**, reinstalling the schedule. It takes
     2–5 minutes (apt + pip run on the VM) and the **Configuration** tab shows a
     live timer plus the command output, so you can see it start and finish.
   - Then **Run now (test)** — from the Configuration tab or
     **News & data → Schedule & run history** — and check **Stored news (Step 5)**
     to see everything it found.

## Relevance & duplicates — why the digest is short

Every source now passes the same gates **before** anything is stored or sent
to the AI, and each gate is visible in the run log
(`HUIZ: 6 kept, 7 filtered out (sector_off=5, unrelated=2).`):

1. **Company relevance.** An item must really mention the company — its
   Chinese/English name, an alias, a **subsidiary or brand** (分期乐, Fenqile,
   奇富借条), its ticker as a whole word, or one of its domains. Matched with
   the anti-substring matcher, so 元保 inside 元保险 still does not count.
2. **Sector news is not company news.** A story about the *insurance industry*
   that never names HUIZ is classified `sector` and dropped (an "El Niño is
   redrawing the insurance industry's risk map" item can no longer reach you
   as a HUIZ alert). Turn on **`sector_watch`** if you still want that class of
   item — it then arrives in its own, capped 🏭 SECTOR CONTEXT section instead
   of being mixed into the stock's items.
3. **Same-story collapse.** One earnings release used to enter the pipeline
   4–6 times (GoogleNewsZH + GoogleNews + Tavily + EXA) and fill the digest.
   Items whose headlines are the same story are now clustered and only the
   best-sourced copy can compete for a seat; the Chinese original or a filing
   beats an aggregator. The AI prompt also sees the current batch as a batch,
   so it flags cross-outlet copies as `known_event`/`duplicate_of`.
4. **Age gate (`push_max_age_hours`, default 72).** Nothing published longer
   ago than this is *ever* pushed, whatever the AI scored it — the "Huize
   results arrived today but the results were days ago" case. It stays in the
   DB for browsing. Regulatory items are exempt (a penalty is news whenever it
   surfaces).
5. **Repeat gate.** Every pushed story is recorded per ticker; a re-report of
   the same event inside the retention window (new URL, different outlet,
   near-identical headline) is stored but not pushed again.
6. **Event guard (`event_repeat_window_days`, default 7).** A corporate EVENT
   is news once. The updater derives an event key from the ticker, the event
   type and the **fiscal period** stated in the headline (`earnings:2026Q2`,
   `dividend`, `connote` …) and pushes it once — every later article about that
   same event is suppressed for a week, even under a different URL, from a
   different outlet, or in another language. `Q2 2026`, `2026 Q2`,
   `Second Quarter 2026`, `第二季度` and `financial results for the quarter ended
   June 30, 2026` all resolve to the same event, while last quarter's report can
   never block this quarter's. 0 disables the guard.
7. **Fair seats.** Seats are handed out **round-robin per ticker**
   (`push_max_per_ticker`, default 2), so one busy name can no longer take the
   whole digest while another name with real news gets nothing.

Items the pipeline defers because a run was busy are automatically re-queued
and analysed by the next run (bounded, so nothing is stranded forever).

## 📢 CHINA MACRO and 🌍 GLOBAL MARKETS are different sections

* **📢 CHINA MACRO** — China policy/market news that moves your names
  (rate cuts 降息/降准/LPR, stimulus, 助贷/消费金融 regulation, 中概股).
  The free regex gate needs a **strong China-policy anchor** (or two distinct
  hits), so a headline that merely says 加息 no longer qualifies.
* **🌍 GLOBAL MARKETS** — systemic US/global news: the Fed, CPI, payrolls,
  tariffs, **and other central banks (ECB/BoJ/BoE)**. Off by default
  (`global_markets`) because it can be chatty; when enabled it is capped
  (`global_markets_max_per_run`, default 2) and a routine daily index recap is
  scored low and dropped.

Both sections are sent as their own Telegram message, on top of the per-stock
digest, so they never eat the digest's 10 seats.

## Fighting macro noise (routine market chatter)

The 7x24 wires carry the whole tape, not just news. Filtering only on "is this
macro-ish" let the database fill with tick-by-tick market chatter that could
never be pushed — and because only the top few macro items get an AI score, most
of those rows also showed up in the panel with **no importance at all**:

```
US three major stock index futures extend gains, all up
Nasdaq 100 futures extend gains to 1%.
US stock fear index VIX falls 1.49 points.
SK Hynix up over 2% in U.S. premarket, last at $192.12
Spot silver rises 1.34%, hitting a new intraday high.
美国财政部8周期国库券中标利率3.845%
【美元指数10日上涨】…上涨0.23%
```

Two things now prevent that:

* **Quality gate** (`market_noise()`): an item that looks like price/market
  reporting **and** carries no policy-grade subject is dropped before it is
  stored. Real macro news survives — a data *release* ("US August CPI rises
  3.4%"), a policy *act* ("央行下调LPR"), a regulatory move, sanctions — while
  the market's *reaction* to it and its *odds* on it do not ("traders raise
  Fed rate-hike bets", "After inflation data, euro extends decline…").
* **Storage cap** (`macro_store_max_per_run`, default 12): the macro tier used
  to store every candidate it collected and score only the top few. It now
  stores at most 12 per run, so the browsable DB stays a record of notable
  macro news instead of a dump of the tape. Typical effect: ~48 → 12 rows/run.

To clean out a backlog collected before the gate existed:

```bash
python3 news_updater.py --purge-macro-noise-dry   # show what would go
python3 news_updater.py --purge-macro-noise       # delete it
```

It removes stored macro/global chatter plus unscored leftovers, and **never**
touches a row that was pushed to Telegram (those are the record of what you
were actually sent).

## Reading Chinese items (translation)

Every stored item carries an English title (the AI translates it), shown above
the original Chinese line in the Stored-news table. Three ways to handle
Chinese:

* **In the Telegram digest** — a Chinese item carries **tappable labelled
  links**, not bare URLs:
  ```
  • [LX] Lexin Q2 net profit falls 80% ... (earnings) ⭐8 📅2026-09-11
      → bearish, funding tightening
      📄 finance.sina.com.cn · 🔤 English
  ```
  *📄 finance.sina.com.cn* opens the original, *🔤 English* opens it through
  Google Translate. The digest shows the AI's *English* title but links to the
  *original* page; for a Chinese source that lands you on text you cannot read,
  which is what made it feel like a dead end. Emitted for every section (macro,
  sector, global markets, the per-stock digest). Detection uses `lang`, falling
  back to CJK characters in the raw title, so rows with no `lang` still get it.

  The digest is sent with Telegram's **HTML** parse mode so links can carry a
  short label — a translate URL is ~150 characters and there is one per Chinese
  item, so bare URLs made the message mostly URL. HTML was chosen over
  MarkdownV2 because it only needs `&`, `<` and `>` escaped, while MarkdownV2
  requires escaping a long list of punctuation and rejects the **entire**
  message if one is missed. If Telegram ever does reject the markup, the chunk
  is automatically re-sent as plain text with the URLs expanded, so a formatting
  bug degrades the digest instead of silencing it.
* **Automatically** — the per-ticker AI call translates as it scores, so
  anything that reaches the digest is already English. A run also back-fills
  English titles for stored items that missed the AI (a busy run trims to
  `max_items_per_run`; those rows used to stay Chinese and unreadable forever).
* **On demand, one row** — the **EN** button next to a 🇨🇳 item translates that
  headline (a single tiny AI call, nothing written to the DB).
* **On demand, the whole article** — the **译** link opens the article through
  Google Translate (`translate.google.com/translate?...&u=<url>`), which needs
  no API key and works on any Chinese site.
* **Bulk back-fill** — the **🌐 Translate missing** button fills in English
  titles for every stored row that has none (one AI call per ~25 headlines).

Cost is negligible: one batched call translates ~12–25 headlines in a single
request (~17s measured), using your own AI key on the server.

### The links open in Telegram's own browser — turn it off

Telegram added an **in-app browser** in July 2024, and it opens links from chats
inside the messenger. That browser handles the Google Translate redirect badly,
so tapping **🔤 English** appears to do nothing. The links themselves are fine —
the same URL works in a normal browser.

**No link can force this.** Telegram decides how to open its own links; the fix
is a client setting, and it applies to every link you tap in Telegram, not just
these.

**Android** — Settings → **Chat Settings** → turn the **In-App Browser** toggle
**off**. Links then open in your device's default browser.

**iOS** — Settings → **Data and Storage** → **Other** → **Open Links in** → pick
the browser you want.

For a one-off, long-press the link and choose *Open in browser* / *Copy link*.

```bash
python3 news_updater.py --translate            # back-fill up to 200 stored rows
python3 news_updater.py --translate=50         # ... a specific number
python3 news_updater.py --translate-pushed     # only rows that were pushed
python3 news_updater.py --translate=LX         # only one ticker
```

## No-spam behavior

- **Junk-domain gate.** EXA and Tavily both return **SEO content farms**: a page
  that scrapes one real story, stuffs every company name it can find into the
  body, and publishes it on a disposable domain. The precision filter above
  CANNOT catch these, because the keyword genuinely is present — that is the
  whole trick. The symptom is the same article arriving tagged for five
  different tickers. When this was measured, **165 of 1,214 stored rows (14%)
  sat on such domains** (`constanta.fmufjl.cyou`, `jingmen.visualstudio-cn.top`,
  `novara.whatswebap.com`) and **10 of them had been pushed to Telegram**.
  These are now dropped at fetch time (`is_junk_host()`), with a second check in
  `insert_news()` so every source — including RSS, Google News and the wires —
  is covered. The rule is deliberately conservative, because a false positive
  silently deletes a real story: free/abused TLDs (`.cyou`, `.icu`, `.top`,
  `.cam`, …) are rejected outright, ambiguous ones (`.info`, `.biz`, `.site`)
  need a machine-generated label as well, and `.com`/`.cn`/`.net` offenders are
  matched against a curated list — a generic heuristic there would have thrown
  away real outlets such as `fx168news.com` and `stocktitan.net`.
  Run `--purge-junk-domains` once after upgrading to clear history.
- **Duplicate purge (`🧹 Purge junk & duplicates`).** The live gate above is
  deliberately strict, because it also decides what may occupy a **digest seat**,
  where a false positive silently drops real news. The price of that strictness
  is that a *reworded* headline for the same event still lands in the database
  twice — e.g. `Citi expects Fed to hike in September, cut before mid-2027` and
  `Citi forecasts September Fed hike, then cuts by 2027` score containment 0.571
  against a required 0.82. Both are `stored`, so the digest was fine but the
  browse view showed the same story twice.
  The button therefore does two things, in order:
  1. deletes stored, never-pushed items scored **<= 2** (the old filter gaps);
  2. **dry-runs** the duplicate pass, shows you how many rows it found and what
     they are, and only deletes after you confirm (`--purge-duplicates`).
  It keeps the best copy of each story — **a row that was pushed to Telegram is
  never deleted**, since it records something actually delivered. Guards stop it
  merging things that merely look alike: different ticker symbols
  (`Torrid (CURV)` vs `Maase (MAAS)` — a generic quote-page template scores
  0.667), different actors (`Citi downgrades Qifu` vs `BofA cuts Qifu`), and
  opposite directions (`Fed to hike` vs `Fed to cut`).
- Exact dedup (SQLite hash per source+URL).
- **Precision filters**: every source result must really mention one of the
  ticker's Chinese names/subsidiaries (with a smarter matcher that rejects
  coincidental substrings like 元保 inside 元保险 / 美元保证金) — Tavily,
  which previously passed through unrelated filler (Heineken buybacks for
  LX), is filtered too.
- **Semantic dedup folded into the analysis call**: the per-ticker AI call
  also sees **what has already been SENT to you** and marks recycled /
  same-event stories (`known_event`) — no separate dedup call, so **half the AI
  calls** per run.
- **Recycled news**: news rows and dedup hashes are both kept ~21 days
  (`news_retention_days` / `seen_retention_days`). Inside the window a
  re-publication is recognized and not re-pushed; after the window it is
  treated as fresh again and reaches you — the behavior you asked for.
  The AI's "already reported" history comes from the **`pushed_stories` ledger**
  (what was actually delivered), not from `seen` (everything ever fetched, ~91%
  of which was never sent — feeding that to the model made it suppress
  genuinely new items). `pushed_stories` is pruned at 3× the event window, so
  `seen_retention_days` no longer affects the model's memory.
- **Importance floor** (`push_min_importance`, default 4): nothing below it is
  pushed, even in "push everything" mode — kills ⭐1–3 noise.
- **AI veto**: the AI's per-item `push` flag is honored — it can keep an item
  stored-only.
- **Per-ticker cap** (`push_max_per_ticker`, default 2): seats are allocated
  round-robin per ticker, so one ticker can't eat every digest slot while
  another name has news.
- **Regulatory force-push**: headlines containing subsidiary-penalty /
  regulatory keywords (处罚/罚款/立案/约谈/退市… or delisting/fraud/
  investigation) get boosted to ⭐8+ so they're never buried by generic
  scoring — this is the real alpha.
- Chinese-language news about the company or its **subsidiaries** is weighed
  heavily by the AI.
- Digests are **split into ≤4,000-char messages** so long Google News URLs
  never make Telegram drop the whole digest.

## Opening the archive from your phone (the `web_url` setting)

The digest ends with a count of items that were **stored but not pushed** — the
ones the AI judged below the push bar. That count used to be a dead end.

Set **`web_url`** to the read-only web view (see `SETUP-WEB.md`) and the line
becomes a link:

```
…and 14 more stored, not pushed. Read them all: https://your-project.web.app
```

Leave `web_url` empty and you get a plain count instead — no broken link, no
reference to a local panel step. It is set in the panel's **Configuration** tab
(under the push settings) or directly in `config_local.json`:

```json
"web_url": "https://keen-wavelet-275120.web.app"
```

### Finding the link again (three easy ways)

`web_url` is the **first key** in `config_local.json`, so it is the first thing
you see when the file opens. You should not need to open the file at all:

| Way | What it does |
|-----|--------------|
| **Panel header** | The **🌐 Open news web app** button (top of the panel, on every tab) opens the page; **📋 Copy link** copies it; the address is printed right next to them |
| **`web_url.txt`** | A one-line file in the project folder with just the address — written by the panel at startup and on every config upload (`type web_url.txt`) |
| **`open_web.cmd`** | Double-click it to open the page. It reads `web_url.txt`, falling back to the config; `python open_web.py --print` just prints the link |

If the button says no link is set, the config has an empty `web_url` — paste the
address into the panel's Configuration tab once and it is remembered everywhere.

**Set it in the panel, not just on the server.** The panel's `config_local.json`
is the source of truth and is uploaded to the VM; editing only the VM's copy gets
silently reverted the next time you click *Upload config to server*.

### Filtering the view from a link

The web page reads filters from the query string, so you can bookmark - or paste
into Telegram - a specific slice of the archive:

```
https://your-project.web.app/?ticker=LX&minImp=6&sort=importance
```

| Parameter | Values |
|---|---|
| `ticker` | any ticker in the archive, e.g. `LX` |
| `minImp` | importance floor: `4`, `6`, `8` |
| `sentiment` | `positive`, `negative`, `neutral` |
| `status` | `pushed` or `stored` |
| `sort` | `newest`, `oldest`, `importance` |
| `q` | free text over titles and summaries |

URL parameters win over whatever was last saved in your browser, so a shared
link always shows exactly what it says.

## Rolling database (always ~3 weeks of news)

Every run prunes the DB:
- `news` rows older than **`news_retention_days`** (default **21**) are deleted
  — the DB always holds about three weeks of news + useful data.
- `seen` dedup hashes are kept the same length (**`seen_retention_days`**,
  default **21**), so a recycled re-publication is treated as fresh again
  after the window — while semantic dedup still stops re-pushes *inside* it.
- `VACUUM` runs automatically when the DB file passes 50 MB.
- `pushed_stories` (the repeat/event gate's memory) is pruned at 3× the event
  window (**21 days** by default), so it cannot grow without bound.

## Tavily budget (free plan: 1,000 searches/month)

1 basic search = 1 credit. The updater budgets hard so you can never blow it:
- **Daily cap** `tavily_max_daily_searches` (default **30**) → at most ~900/month.
- **Monthly cap** `tavily_max_monthly_searches` (default **900**) → insurance.
- **Adaptive skip**: if GoogleNews zh + Eastmoney + Baidu already found
  `tavily_min_free_items` (default **4**) new items for a ticker in a run,
  Tavily is skipped for it entirely.
- **Discovery** uses Tavily only for new/stale tickers (a few one-time searches).

Typical usage: ~5–15 searches/day (~150–300/month), leaving most of the free
allowance unused. Usage is tracked in `tavily_usage.json`.

## DeepSeek Search budget (≈$0.30/month)

DeepSeek's Search API (`POST /v1/tools/search`) uses an **in-house search
engine** and the **same key as the DeepSeek model API**. It is the cheapest
paid source by two orders of magnitude:

- **$0.75 per 1,000 successful requests** (¥5). A search that returns **no
  results is free**, and failures/timeouts are not billed — so the budget counts
  only billable calls, not attempts.
- **Caps:** `deepseek_max_daily_searches` (default **100**) and
  `deepseek_max_monthly_searches` (default **2,000**) — at worst ~$1.50/month,
  in practice ~$0.30–0.80 depending on the fan-out. The panel shows the live
  meter (DeepSeek: n/100 today…).
- **Where it runs:** a fan-out per ticker, all on the same cheap budget:
  1. **baseline** — `<name> 新闻 after:<date> lang:zh` (fresh news);
  2. **corporate actions** — `合股 并股 拆股 ADS比例变更` over a **rolling
     21-day window**. Announcements are made once, so a 24 h delta never sees
     them; this is what found the LU 1-for-10 reverse split;
  3. **HK listing code** — `<code> <name>` over the same 21-day window when
     `ticker_meta` has an `hk_code` (LU `06623`, QFIN `03660`). HKEX
     announcements and the Chinese portals' summaries are indexed there;
  4. **rotating press site** — one of 证券时报/财联社/第一财经/21世纪经济报道/
     中国证券报 per day, over the last 7 days (`site:` + `after:` works;
     a multi-site OR with `before:`/`lang:` returned almost nothing).
  Plus one macro query and up to two discovery queries.
- **Aliases matter:** the HK press writes 陆控, not 陆金所 — that alias is in
  `ticker_meta` and is what lets those summaries through the relevance gate.
- **Query shapes:** Chinese tickers use the Chinese name; English tickers use
  the derived short name (`Toyota news after:…`, **not** `AAPL stock`, which
  returned quote pages). Ticker-only queries are avoided: `LX` matched LXEH
  (丽翔教育) and `YB` matched CNFinance.
- **Why parallel:** it has its own Chinese index, so it adds outlets and
  official-site pages the other sources miss. It also has its **own budget**, so
  when Tavily or EXA is capped it keeps working instead of the pipeline going
  quiet.
- **Key:** reuses `ai_api_key` when the AI provider is DeepSeek. On Gemini/
  OpenAI, paste a DeepSeek key in the panel's optional **DeepSeek Search key**
  field (stored as `deepseek_api_key`), or switch the source off with
  `sources.deepseek_search: false`.
- **Failure behaviour:** HTTP 402 (empty balance) disables the source for the
  rest of that run; 429/5xx leave the delta unadvanced so the window is retried.
- Usage is tracked in `deepseek_usage.json` (`count` = billable, `attempts`,
  `fetch_count`).

## CLI modes (for testing / the panel)

```bash
python3 news_updater.py --force           # run now (bypass schedule guard)
python3 news_updater.py --force --dry-run  # dry run: prints the digest, sends
                                            # nothing, writes nothing AND makes
                                            # no paid calls (Tavily/EXA/DeepSeek)
python3 news_updater.py --force --snapshot # real run; if nothing NEW, send a 📊
                                            # snapshot of the current picture instead
python3 news_updater.py --adhoc-scan=FUTU      # one-off deep search, nothing saved
python3 news_updater.py --adhoc-scan=FUTU:120  # ... a 4-month window
python3 news_updater.py --dump-news       # print stored news as JSON (panel browse)
python3 news_updater.py --dump-news=LX    # ... only for LX
python3 news_updater.py --dump-lookup     # print the company lookup as JSON (panel)
python3 news_updater.py --dump-effective-meta  # what is ACTUALLY searched per ticker
python3 news_updater.py --dump-usage      # Tavily + EXA + DeepSeek usage (panel meter)
python3 news_updater.py --rediscover      # force re-discovery of ALL tickers now
python3 news_updater.py --rediscover=LU   # ... only for LU
python3 news_updater.py --purge-junk      # delete stored junk (never-pushed, importance<=2)
python3 news_updater.py --purge-junk=3    # ... with a custom junk bar
python3 news_updater.py --purge-junk-domains  # delete rows on content-farm/spam domains
python3 news_updater.py --dry-run --purge-junk-domains  # ... list them, delete nothing
python3 news_updater.py --purge-duplicates     # delete same-story duplicates (keeps the best copy)
python3 news_updater.py --dry-run --purge-duplicates   # ... list them, delete nothing
python3 news_updater.py --purge-duplicates=LX  # ... only for LX
python3 news_updater.py --purge-macro-noise-dry  # show macro chatter that would go
python3 news_updater.py --purge-macro-noise      # delete it (never touches pushed rows)
python3 news_updater.py --translate            # fill in English titles for stored rows
python3 news_updater.py --translate=LX         # ... only for LX
python3 news_updater.py --translate-pushed     # ... only rows that were pushed
python3 news_updater.py --delete-news=LX|Tavily|<hash>         # delete ONE stored row
python3 news_updater.py --delete-news-pushed=LX|Tavily|<hash>  # ... and forget it was pushed
```

> `--dry-run` implies `--no-write`: it opens an in-memory DB, writes no
> lookup/history/usage files, sends no Telegram **and skips the paid Tavily/EXA
> searches entirely**, so you can run it as often as you like. Use `--no-write`
> alone only if you also want a real run's side effects suppressed.

**Deleting one item from the panel (Step 5).** Each row in the stored-news
table has an ✕ button. It asks for confirmation, then deletes that single row on
the server. If the row was already **pushed** to Telegram, the delete also
clears the repeat/event-gate memory for that story — deleting your copy is an
explicit "I don't want this", so the same story can reach you again if it is
re-reported. For a stored-only row there is nothing to forget, so only the row
goes. (The `item_hash` shown/used by the panel comes from `--dump-news`.)

## Important security note

Your **AI key**, **Telegram token**, and **Tavily key** live in
`secrets_local.json`, which is **git-ignored** — never commit or share it.
Your **tickers/holdings** live in `config_local.json` (also git-ignored; the
repo ships a blank `config_local.example.json` template). Runtime data —
`news.db`, `company_lookup.json`, `tavily_usage.json`, `exa_usage.json`,
`news_run_history.json`, `sec_validate.json`, `news_updater.lock`,
`panel_url.txt` — is git-ignored too.

The panel itself is hardened: it binds to `127.0.0.1` only, rejects any request
whose `Host` is not localhost or whose `Origin` is cross-site, makes all
mutating routes POST-only, and `/api/config` returns masked placeholders rather
than your real keys.

## Stopping it (if you ever need to)

```bash
crontab -l | grep -v news_updater | crontab -          # user crontab
sudo rm -f /etc/cron.d/news-summer /etc/cron.d/news-winter   # fallback install
```

The second line matters if the installer fell back to `/etc/cron.d/` (it does
when the user crontab is not writable) — the first command alone would leave
those jobs running.

## Troubleshooting

| Problem | What to do |
|---------|------------|
| "Schedule not installed" | Click **Upload config to server**, then **Check schedule**. |
| An OLD story (weeks/months old) appeared in the digest | Fixed by the freshness gates: every EXA result is now date-verified against the per-source delta window, and ANY item whose publish time is older than `max_news_age_hours` (default **96h**, set in `config_local.json`; 0 disables) is dropped at ingestion no matter which source returned it. The digest also shows each item's 📅 publish date so anything stale is visible at a glance. |
| New stock added but no Chinese news appears | Check Step 6 (Company lookup) after the next run — the updater auto-discovers the company's Chinese names/subsidiaries and searches them. You can also add them manually in the Step 3 JSON (overrides always win). |
| No digest arrives | Click **Run now (test)** and read the output. Check Telegram + AI keys. With **no AI key** the run deliberately stores items and pushes nothing (fail closed); an AI outage behaves the same way. |
| Hosted page says **"This account is not on the access list"** | Add that Google email under **Configuration → Hosted page access**, upload, then run `news_web.py --sync-firebase` on the VM. The page prints the signed-in address so you can copy it. Rules only need re-deploying if you changed `firestore.rules` itself. |
| Tavily not used | Add your Tavily key (free tier) in the panel and re-upload; the run log shows `Tavily daily/monthly cap reached` or `skipping Tavily (saving credits)` when it's budget-skipped. |
| DeepSeek Search stops / says **402** | The DeepSeek account balance is empty. Top up at platform.deepseek.com; the source disables itself for that run and resumes next time. (A search with no results is free, so an empty meter is not a spend problem.) |
| Too many/too few items | Switch `push_mode` to `score` and raise `push_min_score`, or lower `push_min_importance`/`push_max_per_ticker` (all editable in the panel). `max_items_per_run` and `max_digest_items` have **no panel input** — hand-edit `config_local.json` on the server if you need them. |
| Out-of-season run skipped | Expected — the DST guard makes the wrong-season cron job a fast no-op. |
| Manual run skipped | The panel's **Run now (test)** always forces a run (`--force`). |
| Double-clicking `start_cloud.bat` does nothing / a window flashes and closes | Was caused by **LF-only line endings and unbalanced quotes in `REM` comments**: cmd.exe counts double quotes before deciding a line is a comment, so it swallowed `setlocal`, `set "URL="` and the `if not defined` checks — the panel never started and the browser was asked to open a file literally named `!URL!`. Fixed, and now enforced by `.gitattributes` (`*.bat text eol=crlf`) plus `python _audit/check_batch_quotes.py`. If you copy the batch files by hand, keep them **CRLF**. |
| A run shows as **interrupted** | Not a failure. The run stopped because whatever was reading its output went away — you closed the window, or the output was piped into `head`/`tail`. Nothing is wrong with the app. It used to be reported as a red **error** (and Ctrl-C too), which made the badge meaningless. The row's tickers/new/sent/duration show how far it got before it stopped. |
| A run shows as **error** | The reason is printed on the line under the row in **4. Schedule & run history**. `unhandled: <Type>: <msg>` is a real crash — the full traceback is in the run log on the server. Rows written before this existed show no reason, because the old code did not store one. |
| The Setup/Config tabs are blank | They fill from `/api/config`. If the panel server errors, `api()` now reports `HTTP 500` instead of a cryptic JSON parse error, and `load()` names the missing config object rather than throwing on `undefined`. |

## Checking your changes before shipping them

```bash
python _audit/run_all.py          # 18 offline checks, one verdict (add --net for AI tests)
```

Runs every test suite, the panel structure/JS/DOM-id/batch-file checkers and the
documentation check. It verifies the **exit code of each script *and* scans its
output for failure markers**, because two suites here used to print
`RESULT: N FAILURE(S)` while still exiting `0` — so a green run could hide a
real failure.

## How the schedule stays reliable across DST

- **Four cron jobs** are installed (two runs × two seasons), firing at the
  correct UTC times for **09:15 and 17:00 ET** in summer (EDT) and winter
  (EST): 13:15/21:00 UTC and 14:15/22:00 UTC.
- `news_updater.py` has a **DST-aware guard** that skips the out-of-season
  jobs instantly, so exactly **two** real runs per day.
- The installer also enables the cron daemon at boot and installs Python deps
  with `sudo` (so cron can import them).
