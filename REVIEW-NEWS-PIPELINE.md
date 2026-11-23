# News-mining & filtering review — 2026-10-16

Scope: the whole discovery → fetch → filter → score → push path
(`news_updater.py`), the macro/global tiers, and the one-off Deep search.
Method: read the code paths end to end, then **probe them with real inputs** —
fake profiles for different kinds of stock, the user's own junk reports, the live
tape, and the VM's real 147-row archive. Every claim below has a reproduced case
or a measurement behind it.

Question asked: *is this robust for any type of stock, and does it catch what
matters while avoiding crap?* Short answer: **for the current portfolio it is now
good; for other kinds of stock it was broken in several ways, all fixed and
pinned by tests.** Two structural gaps remain (listed at the end).

---

## A. What the review found

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| F1 | **High** | **The paid English search never ran for non-Chinese stocks.** The Tavily block was nested inside `if zh_terms:`, so any ticker without a Chinese name (every US/European stock, and any Chinese company whose name discovery had not run yet) fell back to SEC + Google News EN only. | Fixed |
| F2 | **High** | **Word-like tickers matched ordinary English.** Only symbols of ≤2 characters required a market-context word, so `OPEN` (Opendoor), `KEY` (KeyCorp), `ALL`, `NOW`, `FIVE`, `PLAY`, `LOVE`, `TRUE`, `MOVE` classified any headline containing that word as company news — "Markets open higher", "Key question for markets", "Five reasons the rally may stall" — and stored/pushed it. The **name** path leaked the same way for brands that are common words ("Five Below"). | Fixed |
| F3 | **High** | **The prompts hardcoded one portfolio.** The relevance test said "US-listed Chinese ADRs, mainly consumer-lending fintech (QFIN/奇富科技, LX/乐信, LU/陆金所)" and the impact framing named the same three tickers. Any other watchlist got its relevance test and impact lines written about the wrong companies; even for this portfolio it omitted YB/TIGR/CAAS and the removed HUIZ. | Fixed |
| F4 | Med | **EDGAR was queried for every ticker.** A Hong Kong/Shanghai/Tokyo symbol has no US filings; the lookup returned `None`, which the fetch layer reads as "failed" and therefore never advances the delta — so a non-US ticker logged a SEC warning and burned a request **on every run, forever**. | Fixed |
| F5 | Med | **A crashed run's news waited up to 48 h to be recovered.** `promote_stale_staging` only promoted rows older than 48 h, and `prune_staging` deletes after 3 days — an uncomfortably narrow window given runs are 8–14 h apart. | Fixed |
| F6 | **High** | (Earlier the same day) **The macro tape was never filtered at all** — the 7×24 wires bypassed the macro gate entirely, which is why a hydrogen-pilot notice and a Colombo RMB clearing bank were stored. Replaced by AI relevance triage over a 60-item pool. | Fixed |
| F7 | Med | (Earlier the same day) **Routine tape was being stored** — index recaps listing constituent moves and single-stock quotes, including one that rode a CPI mention past a strict prompt. | Fixed |

## B. Coverage by market (after the fixes)

| Market | Sources that actually run | Verdict |
|--------|--------------------------|---------|
| **US-listed Chinese ADR** (current portfolio) | 7×24 Chinese wires (Eastmoney, Sina), Google News zh (CN+HK editions), Eastmoney search API, Google News EN, official-site `site:` search, SEC/EDGAR incl. 6-K exhibit text, China regulator documents (NFRA/CSRC/PBoC), industry-theme regulation queries, Tavily, optional EXA | **Strong** — Chinese alpha plus the English/regulatory backstop |
| **US domestic stock** | SEC/EDGAR, Google News EN, `site:`, Tavily, optional EXA. No Chinese sources (correctly — no Chinese name). | **Adequate** — but see G3 (no PR wires) |
| **Hong Kong-listed** | Google News zh, Eastmoney search, Tavily, EN names. No HKEX filings (G2). | **Weak** for filings-driven news |
| **Japan/Europe** | Google News EN + Tavily only; no local-language sources. | **Thin** — add `hl`/`gl` editions and a local-language name to improve |

## C. What it still does NOT do (honest gaps)

* **G1 — cross-tier duplication.** A story that names a holding is stored under
  that ticker *and*, if it also passes the macro gate, again as a MACRO row. The
  archive can show the same event twice (different sections). Not fixed: doing it
  properly means a shared title ledger across tiers, and the cost of a wrong
  merge (losing the macro framing) is higher than the annoyance.
* **G2 — HKEX filings.** Two approaches were measured and rejected (session
  handshake returns 0 records; the index returns "Untitled"/other issuers). A real
  fix needs per-document PDF text, which is its own project.
* **G3 — no press-release wire.** Business Wire / PR Newswire / GlobeNewswire are
  not sources. Companies' own announcements are reached only through `site:` and
  Google News, which is slower. Adding one free RSS feed per wire would close it.
* **G4 — no Chinese-name discovery without Tavily/AI.** If the paid search or the
  model is unavailable, a Chinese company stays English-only (the log warns).
* **G5 — Deep search is memoryless.** Read-only by design, so it cannot see what
  earlier scans found; each scan groups only within itself.
* **G6 — usage counters are not lock-protected.** The run lock prevents two runs
  overlapping, so this is theoretical today.
* **G7 — sector news that names no company relies on the theme queries.** With
  `sector_watch` off (default), the only unnamed-sector coverage is the six
  industry-regulation queries; other sector themes are invisible.
* **G8 — no retail-sentiment / social sources** (雪球, 股吧). Tested earlier and
  rejected as noise-dominated; a keyworded variant could be revisited.

## D. Bugs fixed in this review (all with regression tests)

`_audit/test_stock_types.py` (new, 40+ assertions) pins F1–F5: prose collisions
for nine word-like tickers, the real-news counterparts, CJK collision behaviour,
the EDGAR symbol rule (including the `VOD.L` vs `BRK.B` distinction), the dynamic
portfolio brief, the Tavily block's placement, and staged-row recovery.

`_audit/test_macro_triage.py` pins F6: AI relevance verdict routing, the no-verdict
and no-key fallbacks (strict screen, never the raw tape), and the pool/storage caps.

`_audit/test_market_noise.py` pins F7 (index closes/recaps are tape).

Suite: **29 pass / 0 fail.**

## E. Prioritised backlog

1. **G3 press-release feeds** — highest value per unit of work for non-Chinese
   stocks; one free RSS per wire covers all tickers at once.
2. **G1 shared title ledger across tiers** — removes the double-reporting.
3. **G2 HKEX via per-document PDF fetch** — the honest way to cover HK names.
4. **G7 more theme queries / sector_watch default** — widen unnamed-sector cover.
5. **Local-language editions** (Google News `hl=ja`, `hl=de`) for non-Chinese
   markets — cheap, but only matters if such tickers are added.

---

## F. Follow-up review — 2026-10-17

A second end-to-end pass (config I/O, panel, hosted page, and a full watchlist
swap exercised with `AAPL / 0700.HK / 7203.T / MC.PA / BRK.B / BABA`) found 15
more defects. All are fixed and pinned by tests; the suite is now **31 checks**.

| Area | What was wrong | Now |
|---|---|---|
| Config I/O | A UTF-8 **BOM** (PowerShell / VS Code) made `json.load` fail, the error was swallowed, the config looked empty and the panel's Upload could write that emptiness back over the watchlist and keys. | Read as `utf-8-sig`; parse errors are reported, the panel shows them, disables Upload, and refuses an empty watchlist. |
| Hosted access | `firestore.rules` allowed **any signed-in Google account** to read the archive. | Email allowlist in `meta/status` (from `web_allowed_emails`); empty list = nobody. Rules tested through the Firebase Rules API for allow/deny. |
| Panel | `Origin: null` and any loopback port passed the CSRF check; a stored secret could never be cleared; `gcloud` timeouts did not kill the Windows child process; two purge routes sent a second 404. | Exact scheme+host+port origin check, dirty-field secret clearing, process-tree kill, `return` after each handler. |
| Any watchlist | Official English names never matched headline forms ("Toyota Motor Corporation" vs "Toyota"): 12 of 22 tested US headlines were rejected as unrelated. | `aliases_en` plus derived short names, with guard lists; 1 of 22 now needs an explicit alias (Google/Alphabet). |
| Swapping tickers | Removed tickers' `ticker_meta` was re-seeded and could claim shared brand names; `rescue_orphans` re-queued rows for dropped tickers; the macro tier stayed China-only. | Upload prunes metadata/feeds, the lookup only seeds configured tickers, rescue is ticker-filtered, and the China macro tier auto-skips when no holding has a Chinese name. |
| No AI key | The per-ticker path pushed unscored headlines (importance 5, push=true). | Fails closed: stored for browsing, never pushed; macro collection is skipped entirely. |
| Quality | Auto-generated US filler and `javascript:` links. | Filler patterns added; only `http(s)` becomes a link in either reader. |
| Tests / docs | 67 hardcoded `F:\...` paths (the suite could not run on a clone); setup docs embedded one user's project id and home directory. | Paths derive from `__file__` (verified from a clone); docs de-personalised, with a swap checklist and the access-allowlist deploy order. |

Still open from section C: **G1** cross-tier duplication and local-language
editions for non-Chinese/non-English markets. **G2 (HKEX filings)** is now
**closed**, and **G3 (press-release coverage)** is substantially closed via
Yahoo Finance & Seeking Alpha ticker-scoped RSS (carrying GlobeNewswire / PR
Newswire releases).

---

## G. Recall, relevance & primary-source overhaul — 2026-11-23

An end-to-end audit against QFIN, LU, and LX (verified with offline regression
tests in `_audit/test_alpha_fixes.py` and a live 14-day dry run) closed 10
pipeline defects and added four free, ticker-scoped primary sources:

| Area | What was wrong | Now |
|---|---|---|
| After-hours SEC & date-only delta | `_parse_pub` anchored date-only strings at 12:00 ET, so any filing or URL-dated item published after the 17:00 ET run was dropped next morning as `older_than_delta`. | `_pub_before_since` compares date-only values by calendar day; `fetch_sec_filings` now uses SEC's official submissions JSON API (`data.sec.gov/submissions/CIK{cik}.json`) with exact `acceptanceDateTime` and clean Exhibit 99.x titles. |
| Rolling DeepSeek lookbacks | `DeepSeekCorp` / `DeepSeekHK` (21d) and `DeepSeekPress` (7d) paid for multi-week searches whose hits >4d old were immediately killed by the 96h ingest and 72h push gates. | `fetch_source` passes per-source `max_age_hours_override` and `max_push_age_hours` ceilings matching each rolling window. |
| Case-sensitive EN matching | `_term_in_text` matched ASCII terms case-sensitively, dropping uppercase headlines (`QFIN Holdings...`, `LUFAX...`, `LEXINFINTECH...`) in Tavily & DeepSeek. | Case-insensitive word-boundary matching for Latin/English terms. |
| Discovered brands & stems | `discover_brands` stored brands (`平安普惠`, `奇富借条`) in `keywords`, which `build_zh_terms` searched and `resolve_relevance` ignored; `LexinFintech` did not derive `Lexin`. | `resolve_relevance` includes safe `keywords`; `_latin_name_variants` derives compound brand stems; `CORPORATE_CONTEXT_WORDS` includes reverse split / ADS / SEC / HKEX terms. |
| `LX` / `LU` collisions & AI gate | `LX Semicon`, `LX Pantos`, `LX International`, and `Lu Zhiqiang resigns...` passed `passes_relevance` as `company`, and ticker-mode `ai_analyze` had no `relevant` question or today's date. | Negative homonym guard + ALL-CAPS requirement for 1–2 letter tickers; ticker-mode `ai_analyze` injects `TODAY'S DATE (ET)` and enforces `relevant: true/false` in `_event_groups` and `select_push_items`. |
| Staging, grouping & trim loss | `_event_groups` keyed `_number` globally across tickers; macro `promote_run` marked all `run_id` staging rows `promoted=1`; items trimmed past `max_items_per_run` were lost behind an advanced delta. | `_event_groups` keys by `(ticker, _number)`; `promote_run` scopes staging updates to handled tickers; trimmed items are saved unscored into `news` for `rescue_orphans`. |
| 7×24 wires & macro GNews | Per-ticker wires skipped company mentions that also matched `is_macro()`; `MACRO_GNEWS_QUERY` AND-joined 9 words; Eastmoney `_em` discarded all terms on 1 failure. | Explicit company mentions are always kept on per-ticker wires; `MACRO_GNEWS_QUERY` uses `OR` + `when:7d`; `_em` only fails when all terms fail. |
| Wordlists & fail-closed | `#` comments lived inside triple-quoted wordlists; `\bLLP\b` never matched lowercased text; regulatory keyword boost overrode AI fail-closed when no key was set. | `_wordlist()` strips comments; `is_junk_content` matches case-insensitively and filters live law-firm/Xueqiu/Yahoo quote templates; `_ai_unavailable` strictly blocks pushes. |
| **New primary sources (G2 closed)** | HKEX returned 0 because `titleSearchServlet` needs internal `stockId`; no ticker-scoped English RSS. | Added free **HKEX** (`prefix.do` → `stockId` → `titleSearchServlet.do`, auto-mapped via `KNOWN_HK_CODES` for `QFIN`/`LU`/ADRs, treated as `is_primary_event`), **Yahoo Finance** & **Seeking Alpha** ticker RSS, and Guba `stockbar_name` matching. |
