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
