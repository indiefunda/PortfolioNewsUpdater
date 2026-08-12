#!/usr/bin/env python3
"""
PortfolioNewsUpdater - free stock news monitor.

Runs on a Google Cloud "Always Free" e2-micro VM. Twice a day (08:00 and
20:00 UTC) it checks the configured tickers for NEW information since the
last run.

Pipeline:
  TICKERS -> [SEC/EDGAR, RSS/News, Company IR] -> NORMALIZE
          -> DEDUPLICATE (SQLite + AI semantic) -> AI FILTER (importance)
          -> important? -> TELEGRAM digest (others -> DB for later browsing)

Sources (v1):
  - SEC EDGAR filings (8-K, 10-Q, 10-K, 6-K, 20-F, DEF 14A, Form 4) via edgartools
  - Google News RSS (any ticker, no config)
  - Company RSS / press-release feeds (configured per ticker) via feedparser

New items are deduplicated (exact via SQLite, near-duplicates via AI),
relevance-filtered and translated to English by DeepSeek, then a concise
Telegram digest is sent.

Runs via cron (twice daily). Reads config_local.json and secrets_local.json.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests

# edgartools - SEC EDGAR access. If it's missing, RSS + AI still work.
try:
    from edgar import Company, set_identity
    SEC_AVAILABLE = True
except Exception:
    SEC_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
DB_FILE = os.path.join(BASE_DIR, "news.db")
RUN_HISTORY_FILE = os.path.join(BASE_DIR, "news_run_history.json")

EASTERN = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": "PortfolioNewsUpdater/1.0 (personal stock news monitor)",
    "Accept-Encoding": "gzip, deflate",
}

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# AI provider default (DeepSeek is OpenAI-compatible).
DEFAULT_AI_BASE = "https://api.deepseek.com"
DEFAULT_AI_MODEL = "deepseek-v4-flash"

RUN_HISTORY_LIMIT = 200

# How long to keep already-seen items in the dedup DB before pruning, and the
# max DB file size before we force a VACUUM. This keeps the free-tier VM disk
# from growing without bound (Google News re-publishes a lot of items).
SEEN_RETENTION_DAYS = 60
DB_SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB

# SEC identity (required by edgartools / SEC fair-access policy).
SEC_IDENTITY = "PortfolioNewsUpdater personal-use news monitor"


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config():
    return _read_json(CONFIG_FILE, {})


def load_secrets():
    return _read_json(SECRETS_FILE, {})


# ---------------------------------------------------------------------------
# Run history (same pattern as the cloud price monitor)
# ---------------------------------------------------------------------------
def load_run_history():
    records = _read_json(RUN_HISTORY_FILE, [])
    return records if isinstance(records, list) else []


def append_run_record(record):
    try:
        records = load_run_history()
        records.insert(0, record)
        records = records[:RUN_HISTORY_LIMIT]
        _write_json(RUN_HISTORY_FILE, records)
    except Exception as exc:
        print(f"  [error] could not write run history: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SQLite dedup database (the "delta" - only genuinely new items are reported)
# ---------------------------------------------------------------------------
def _db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            item_hash TEXT NOT NULL,
            title TEXT,
            url TEXT,
            first_seen TEXT,
            PRIMARY KEY (ticker, source, item_hash)
        )"""
    )
    # Per-ticker, per-source "when was this data last fetched". This is what
    # drives the delta: each run fetches only what's new since this timestamp.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS last_fetched (
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (ticker, source)
        )"""
    )
    return conn


def get_last_fetched(conn, ticker, source):
    """Return the last-fetched timestamp for this ticker/source, or None."""
    cur = conn.execute(
        "SELECT fetched_at FROM last_fetched WHERE ticker=? AND source=?",
        (ticker, source),
    )
    row = cur.fetchone()
    return row[0] if row else None


def set_last_fetched(conn, ticker, source, when):
    """Record that this ticker/source was fetched at 'when' (ISO string)."""
    conn.execute(
        "INSERT OR REPLACE INTO last_fetched (ticker, source, fetched_at) VALUES (?,?,?)",
        (ticker, source, when),
    )
    conn.commit()


def item_hash(source, item_id, title=None):
    """
    A stable hash for an item so we can detect what's new vs already seen.

    Hashes on source + item_id (the URL / accession / link), NOT the title.
    The title is intentionally excluded because the same article can appear
    with slightly different titles (or garbled text from Google News), which
    would otherwise create false "duplicates". The item_id is the canonical
    unique identifier for an item.
    """
    raw = f"{source}|{item_id}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_new(conn, ticker, source, item_id, title):
    """Return True if this item has not been reported before."""
    h = item_hash(source, item_id, title)
    cur = conn.execute(
        "SELECT 1 FROM seen WHERE ticker=? AND source=? AND item_hash=?",
        (ticker, source, h),
    )
    return cur.fetchone() is None


def mark_seen(conn, ticker, source, item_id, title, url):
    h = item_hash(source, item_id, title)
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO seen (ticker, source, item_hash, title, url, first_seen) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, source, h, title, url, now),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Semantic dedup (AI-assisted)
# ---------------------------------------------------------------------------
# How many recently-seen items (per ticker) the AI compares new items against.
# This is what catches near-duplicates: two headlines about the SAME event
# (e.g. "BlackRock bought 10,000 shares" vs "BlackRock entered") that have
# different wording but the same underlying meaning.
SEMANTIC_DEDUP_HISTORY = 20


def get_recent_seen(conn, ticker, limit=SEMANTIC_DEDUP_HISTORY):
    """Return the most recent seen titles for a ticker, newest first."""
    cur = conn.execute(
        "SELECT title FROM seen WHERE ticker=? "
        "ORDER BY first_seen DESC LIMIT ?",
        (ticker, limit),
    )
    return [row[0] for row in cur.fetchall() if row[0]]


def semantic_dedup(items, conn, config, secrets):
    """
    Drop items that are semantically the SAME event as something already
    reported for that ticker (different wording, same meaning). This catches
    near-duplicates that the exact hash-based dedup misses.

    Cost-efficient: one batched AI call PER TICKER. For each ticker we give the
    model the new candidate headlines + the recent history headlines, and it
    returns which candidates are genuinely NEW events. Items that are just
    re-wordings of already-reported news are dropped.

    Falls back to returning all items unchanged on any error (never drop news
    just because the AI call failed).
    """
    if not items or not conn:
        return items
    base = config.get("ai_base_url") or DEFAULT_AI_BASE
    model = config.get("ai_model") or DEFAULT_AI_MODEL
    key = secrets.get("ai_api_key", "")
    if not key:
        return items  # no AI -> keep everything (stage 1 already ran)

    kept = []
    # Group candidates by ticker so we make one call per ticker.
    by_ticker = {}
    for it in items:
        by_ticker.setdefault(it.get("ticker", ""), []).append(it)

    for ticker, cands in by_ticker.items():
        if not ticker:
            kept.extend(cands)
            continue
        history = get_recent_seen(conn, ticker)
        if not history:
            kept.extend(cands)
            continue
        cand_lines = [f"{i+1}. {c.get('title','')}" for i, c in enumerate(cands)]
        hist_lines = [f"- {t}" for t in history]
        prompt = (
            "You are a news deduplicator. Below are NEW headlines for stock "
            f"{ticker}, and a list of news ALREADY REPORTED for {ticker}.\n"
            "For each NEW headline, decide whether it describes the SAME "
            "underlying event as any ALREADY REPORTED item (just worded "
            "differently). Examples of the SAME event:\n"
            "  'BlackRock bought 10,000 shares' vs 'BlackRock entered' -> SAME\n"
            "  'Company X beats Q1 earnings' vs 'X reports record profit' -> SAME\n"
            "Return ONLY a JSON array of the numbers (1-based) of the NEW "
            "headlines that are GENUINELY NEW events (not duplicates). If all "
            "are duplicates, return [].\n\n"
            "ALREADY REPORTED:\n" + "\n".join(hist_lines) + "\n\n"
            "NEW HEADLINES:\n" + "\n".join(cand_lines)
        )
        content = _chat(base, model, key,
                        "You are a precise JSON-returning assistant.",
                        prompt)
        if content is None:
            kept.extend(cands)  # on error, keep everything (don't miss news)
            continue
        keep_nums = set()
        try:
            match = re.search(r"\[.*\]", content, re.DOTALL)
            if match:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, list):
                    keep_nums = {int(x) for x in parsed if str(x).strip().isdigit()}
        except Exception as exc:
            print(f"  [warn] semantic dedup parse failed for {ticker}: {exc}",
                  file=sys.stderr)
            keep_nums = set()
        # Keep only the candidates the AI said are genuinely new (1-based).
        kept_count = 0
        for i, c in enumerate(cands):
            if (i + 1) in keep_nums:
                kept.append(c)
                kept_count += 1
        print(f"  Semantic dedup {ticker}: {len(cands)} candidate(s) -> "
              f"{kept_count} genuinely new. "
              f"(compared against {len(history)} recent item(s))")
    return kept


def prune_db(conn):
    """
    Prevent unbounded growth of the dedup DB on the free-tier VM.

    - Deletes 'seen' rows older than SEEN_RETENTION_DAYS. Once an item is far
      past the lookback window it can never be re-detected, so keeping it only
      wastes disk.
    - Runs VACUUM when the DB file exceeds DB_SIZE_LIMIT_BYTES so the file
      physically shrinks (deletes alone don't reclaim space in SQLite).
    Returns (rows_deleted, vacuumed) for logging.
    """
    rows_deleted = 0
    vacuumed = False
    try:
        cutoff = (datetime.now(EASTERN) - timedelta(days=SEEN_RETENTION_DAYS)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(
            "DELETE FROM seen WHERE first_seen < ?", (cutoff,)
        )
        rows_deleted = cur.rowcount
        conn.commit()

        # VACUUM only when the file is large enough to matter (it rewrites the
        # whole DB, so we don't want to run it on every cron tick).
        if os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > DB_SIZE_LIMIT_BYTES:
            conn.execute("VACUUM")
            vacuumed = True
    except Exception as exc:
        print(f"  [warn] DB prune failed: {exc}", file=sys.stderr)
    return rows_deleted, vacuumed


# ---------------------------------------------------------------------------
# SEC EDGAR (via edgartools)
# ---------------------------------------------------------------------------
# SEC form types that matter to an investor. This includes the company's own
# reports (8-K, 10-Q, 10-K, 6-K, 20-F) AND ownership filings (SCHEDULE 13D/13G
# and amendments), which signal changes in who holds large positions.
SEC_FORMS = [
    "8-K", "10-Q", "10-K", "6-K", "20-F", "F-1", "424B4",
    "DEF 14A", "SCHEDULE 13D", "SC 13D", "SCHEDULE 13D/A", "SC 13D/A",
    "SCHEDULE 13G", "SC 13G", "SCHEDULE 13G/A", "SC 13G/A",
]


def fetch_sec_filings(ticker, since_dt):
    """
    SEC filings for this ticker filed since since_dt, using edgartools.

    Returns a list of items, or None if the fetch FAILED (so the caller knows
    NOT to advance the delta timestamp and risk missing news).
    """
    if not SEC_AVAILABLE:
        print("  [warn] edgartools not installed - skipping SEC.")
        return None  # can't fetch -> don't advance the delta
    items = []
    try:
        set_identity(SEC_IDENTITY)
        company = Company(ticker)
        filings = company.get_filings(form=SEC_FORMS)
        since_str = since_dt.strftime("%Y-%m-%d")
        cik = str(getattr(company, "cik", "") or "").strip()
        for f in filings:
            filed = str(getattr(f, "filing_date", "") or "")
            if filed and filed < since_str:
                continue
            form = getattr(f, "form", "") or ""
            company_name = getattr(f, "company", "") or ticker
            acc = str(getattr(f, "accession_no", "") or "").strip()
            # Build a real SEC URL when we have a CIK + accession number.
            # SEC archives use: .../data/{CIK}/{accession_without_dashes}/
            if acc and cik:
                url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                       f"{acc.replace('-', '')}/")
            else:
                url = ""
            title = f"{company_name} - {form} filed {filed}"
            items.append({
                "source": "SEC",
                "ticker": ticker,
                "id": acc or f"{form}-{filed}",
                "title": title,
                "url": url,
                "date": filed,
                "form": form,
                "company": company_name,
            })
    except Exception as exc:
        print(f"  [error] SEC {ticker}: {exc}", file=sys.stderr)
        return None  # fetch failed -> don't advance the delta
    return items


# ---------------------------------------------------------------------------
# RSS / press-release feeds (via feedparser)
# ---------------------------------------------------------------------------
def fetch_rss(url, ticker, since_dt=None):
    items = []
    try:
        parsed = feedparser.parse(url)
        for entry in parsed.entries[:30]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "") or url
            pub = entry.get("published", "") or entry.get("updated", "")
            if not title:
                continue
            # If a lookback window is given, drop items older than it using the
            # parsed publish time. feedparser gives a time.struct_time in UTC,
            # so build the datetime as UTC and compare against the (ET) since_dt
            # by converting it to UTC to keep the comparison consistent.
            if since_dt is not None:
                pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_parsed:
                    pub_dt = datetime(*pub_parsed[:6], tzinfo=ZoneInfo("UTC"))
                    if pub_dt < since_dt.astimezone(ZoneInfo("UTC")):
                        continue
            items.append({
                "source": "RSS",
                "ticker": ticker,
                "id": link or title,
                "title": title,
                "url": link,
                "date": pub,
                "feed": url,
            })
    except Exception as exc:
        print(f"  [error] RSS {url}: {exc}", file=sys.stderr)
        return None  # fetch failed -> don't advance the delta
    return items


# ---------------------------------------------------------------------------
# AI filter + dedupe + translate (two-stage to save cost)
# ---------------------------------------------------------------------------
def _chat(base, model, key, system, user, timeout=60):
    """One chat completion call. Returns the raw content string or None."""
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        print(f"  [error] AI call failed: {exc}", file=sys.stderr)
        return None


def ai_filter(items, config, secrets, conn=None):
    """
    Multi-stage AI filtering to save cost:
      Stage 1 (cheap): ask for a YES/NO per item title - is it worth an
                       investor's attention?
      Stage 1.5 (semantic dedup): drop items that are the SAME event as
                       something already reported for that ticker (catches
                       near-duplicates with different wording).
      Stage 2 (detailed): only for the survivors, do the full analysis
                       (dedupe, translate, summarize, categorize, explain).
    Returns a list of dicts (the kept, ranked items).
    """
    if not items:
        return []
    base = config.get("ai_base_url") or DEFAULT_AI_BASE
    model = config.get("ai_model") or DEFAULT_AI_MODEL
    key = secrets.get("ai_api_key", "")

    if not key:
        print("  [warn] No AI API key set - skipping AI filter, sending all new items.")
        return items

    # ---- Stage 1: cheap YES/NO gate on every item ----
    title_lines = [f"{i+1}. [{it.get('ticker','')}] {it.get('title','')}" for i, it in enumerate(items)]
    stage1_prompt = (
        "You are an investor's news filter. For each line below, answer with ONLY "
        "'YES' or 'NO' (one per line, same order) - YES if the item is genuinely "
        "material for an investor in that stock (earnings, SEC filings, big news, "
        "analyst moves, regulatory changes), NO if it's routine, spam, or noise.\n\n"
        "Lines:\n" + "\n".join(title_lines)
    )
    print(f"  Stage 1: filtering {len(items)} item(s) with AI...")
    stage1_raw = _chat(base, model, key,
                       "You are a precise YES/NO-only assistant.",
                       stage1_prompt)
    if stage1_raw is None:
        return items  # fall back to sending everything on error

    keep_idx = []
    for i, line in enumerate(stage1_raw.strip().splitlines()):
        if i >= len(items):
            break
        if line.strip().upper().startswith("Y"):
            keep_idx.append(i)
    kept = [items[i] for i in keep_idx]
    print(f"  Stage 1 kept {len(kept)} item(s) for detailed analysis.")

    if not kept:
        return []

    # ---- Stage 1.5: semantic dedup against already-reported news ----
    # This is the AI dedup that catches "same event, different wording" items.
    if conn is not None:
        kept = semantic_dedup(kept, conn, config, secrets)
        if not kept:
            print("  Semantic dedup removed all candidates - nothing new.")
            return []

    # ---- Stage 2: detailed analysis on the survivors ----
    prompt = (
        "You are an investor's news assistant. Below is a JSON list of newly "
        "discovered items (SEC filings, press releases, news) for a set of stocks.\n"
        "Tasks:\n"
        "1. DEDUPE: if several items are about the same underlying event "
        "(e.g. multiple articles about the same earnings call), keep only the "
        "single most authoritative one.\n"
        "2. TRANSLATE: if any title/summary is not in English, translate it to English.\n"
        "3. RANK: order the remaining items by investor relevance (most important first).\n"
        "4. For each kept item, add a category: one of filing, earnings, press_release, "
        "analyst, regulatory, other, and a one-sentence summary.\n"
        "Return ONLY a JSON array of objects, each with keys: "
        "ticker, title (English, concise), url, category, summary, "
        "reason (one short sentence why it matters to an investor).\n"
        "If nothing is worth reporting, return an empty JSON array [].\n\n"
        "ITEMS:\n" + json.dumps(kept, ensure_ascii=False)
    )
    print(f"  Stage 2: analyzing {len(kept)} item(s) in detail...")
    content = _chat(base, model, key, "You are a precise JSON-returning assistant.", prompt)
    if content is None:
        return kept  # fall back to the kept set on error

    match = re.search(r"\[.*\]", content, re.DOTALL)
    if not match:
        print("  [warn] AI returned no parseable JSON.")
        return kept
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else kept
    except Exception as exc:
        print(f"  [error] AI JSON parse failed: {exc}", file=sys.stderr)
        return kept


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(token, chat_id, message):
    if not token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=token)
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"  [error] Telegram send failed: {exc}", file=sys.stderr)
        return False


def format_digest(filtered, ticker_count):
    if not filtered:
        return None
    lines = [f"📰 Portfolio News Digest ({ticker_count} ticker(s))", ""]
    for item in filtered:
        ticker = item.get("ticker", "")
        title = item.get("title", "")
        url = item.get("url", "")
        reason = item.get("reason", "")
        category = item.get("category", "")
        header = f"• [{ticker}] {title}"
        if category:
            header += f"  ({category})"
        lines.append(header)
        if reason:
            lines.append(f"    {reason}")
        if url:
            lines.append(f"    {url}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    start_time = datetime.now(EASTERN)
    record = {
        "timestamp": start_time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "status": "ran",
        "tickers_checked": 0,
        "new_items": 0,
        "sent_items": 0,
        "duration_sec": None,
        "error": None,
    }

    config = load_config()
    if not config:
        print("No config_local.json found.")
        record["status"] = "error"; record["error"] = "No config."
        append_run_record(record); return

    enabled = config.get("enabled", False)
    if not enabled:
        print("News updater is DISABLED.")
        record["status"] = "disabled"
        append_run_record(record); return

    tickers = [t.strip().upper() for t in config.get("tickers", []) if t.strip()]
    if not tickers:
        print("No tickers configured.")
        record["status"] = "error"; record["error"] = "No tickers."
        append_run_record(record); return

    secrets = load_secrets()
    token = secrets.get("telegram_bot_token", "")
    chat_id = secrets.get("telegram_chat_id", "")

    conn = _db()
    # Housekeeping: keep the dedup DB from growing without bound on the
    # free-tier disk. Safe to run every run.
    pruned, vacuumed = prune_db(conn)
    if pruned:
        print(f"  [db] pruned {pruned} old seen item(s).")
    if vacuumed:
        print("  [db] VACUUM ran (DB file shrank).")
    all_new = []  # raw new items (pre-AI)
    record["tickers_checked"] = len(tickers)

    # On the very first run there is no "last fetched" timestamp yet. Default
    # the initial delta to this many hours back so the first run catches recent
    # items. Configurable via config_local.json -> initial_lookback_hours.
    initial_hours = int(config.get("initial_lookback_hours", 24))
    now_utc_str = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{start_time.strftime('%Y-%m-%d %H:%M %Z')}] Checking {len(tickers)} ticker(s) for new news...")

    for ticker in tickers:
        # Determine the delta for this ticker: fetch only what's new since the
        # last time we fetched it. Each source is tracked independently.
        for source in ("SEC", "GoogleNews"):
            last = get_last_fetched(conn, ticker, source)
            if last:
                since_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
            else:
                since_dt = datetime.now(EASTERN) - timedelta(hours=initial_hours)

            if source == "SEC":
                items = fetch_sec_filings(ticker, since_dt)
            else:
                gnews_url = "https://news.google.com/rss/search?q=" + ticker + "+stock"
                items = fetch_rss(gnews_url, ticker, since_dt)
                if items is not None:
                    for it in items:
                        it["source"] = "GoogleNews"

            # 'items' is None only if the fetch FAILED. In that case we do NOT
            # advance last_fetched, so nothing published during the failed
            # window is missed - the next run re-fetches from the old delta.
            if items is None:
                print(f"  [warn] {ticker} {source} fetch failed - NOT advancing "
                      f"delta (will retry from {since_dt.strftime('%Y-%m-%d %H:%M')}).")
                continue

            for item in items:
                if is_new(conn, ticker, item["source"], item["id"], item["title"]):
                    all_new.append(item)
                    mark_seen(conn, ticker, item["source"], item["id"], item["title"], item.get("url", ""))

            # Update the delta timestamp for this ticker/source to now.
            set_last_fetched(conn, ticker, source, now_utc_str)

        # Company RSS feeds (configured per ticker) - track under source "RSS".
        feeds = config.get("rss_feeds", {}).get(ticker, [])
        last = get_last_fetched(conn, ticker, "RSS")
        since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                    if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
        rss_ok = True
        for feed_url in feeds:
            feed_items = fetch_rss(feed_url, ticker, since_dt)
            if feed_items is None:
                rss_ok = False
                print(f"  [warn] {ticker} RSS {feed_url} failed - NOT advancing delta.")
                continue
            for item in feed_items:
                if is_new(conn, ticker, item["source"], item["id"], item["title"]):
                    all_new.append(item)
                    mark_seen(conn, ticker, item["source"], item["id"], item["title"], item.get("url", ""))
        # Only advance the RSS delta if ALL configured feeds fetched OK.
        if rss_ok:
            set_last_fetched(conn, ticker, "RSS", now_utc_str)

    record["new_items"] = len(all_new)
    print(f"  {len(all_new)} new item(s) found.")

    # Keep only the most recent items for the AI filter, so we never drop the
    # newest news when there's a flood (e.g. first run after a long gap).
    # Sort by date (newest first) so the trim keeps the freshest items.
    def _sort_key(it):
        # Prefer a parsed date; fall back to the raw string; empty = oldest.
        d = it.get("date") or ""
        try:
            # Handles both 'YYYY-MM-DD' (SEC) and RFC822-ish RSS publish strings.
            return datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except Exception:
            return datetime.min
    all_new.sort(key=_sort_key, reverse=True)

    max_to_filter = int(config.get("max_items_per_run", 40))
    if len(all_new) > max_to_filter:
        print(f"  Trimming to the {max_to_filter} most recent for AI filtering.")
        all_new = all_new[:max_to_filter]

    # AI filter + dedupe + translate. conn stays open so the semantic-dedup
    # stage can compare new items against the already-seen history.
    filtered = ai_filter(all_new, config, secrets, conn)
    conn.close()

    # Cap the digest size so Telegram doesn't get a wall of text.
    max_digest = int(config.get("max_digest_items", 10))
    filtered = filtered[:max_digest]
    record["sent_items"] = len(filtered)

    digest = format_digest(filtered, len(tickers))
    if digest:
        if token and chat_id:
            if send_telegram(token, chat_id, digest):
                print(f"  Digest sent with {len(filtered)} item(s).")
            else:
                record["alerts_failed"] = record.get("alerts_failed", []) + ["digest"]
        else:
            print("  Digest ready but Telegram not configured.")
    else:
        print("  No meaningful new items - nothing sent.")

    record["duration_sec"] = round((datetime.now(EASTERN) - start_time).total_seconds(), 2)
    append_run_record(record)


if __name__ == "__main__":
    main()
