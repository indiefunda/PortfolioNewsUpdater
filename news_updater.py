#!/usr/bin/env python3
"""
PortfolioNewsUpdater - free stock news monitor (v2 - Chinese-first).

Runs on a Google Cloud "Always Free" e2-micro VM. Twice a day (09:15 and
16:45 ET) it checks the configured tickers for NEW information since the
last run - and for Chinese companies it searches CHINESE news sources
using the company's Chinese name and its subsidiaries (e.g. LX -> 乐信,
分期乐, 桔子理财), because that is where the real edge is for US-listed
Chinese companies. Everything found is stored in a SQLite news database,
translated to English and scored by AI, and only the most important items
are pushed to Telegram.

Pipeline (v2):
  TICKERS (+ Chinese names/aliases/subsidiaries + websites, auto-discovered)
    -> [SEC EDGAR (6-K/8-K content extracted), Google News EN/ZH, Google News
       site: (official websites), Eastmoney, Baidu, Tavily news search, RSS]
    -> NORMALIZE -> DEDUPLICATE (SQLite exact hash)
    -> store EVERY new item in the `news` table
    -> AI ANALYSIS (one batched call per ticker): translate to English,
       summarize, categorize, importance 1-10, sentiment, push/store, AND
       folded dedup (known_event vs the `seen` ledger - no separate call)
    -> PUSH selection: importance floor + AI veto + regulatory force-push,
       ranked by importance (Chinese sources weigh higher), per-ticker cap,
       max_digest_items -> TELEGRAM digest (split into <=4000-char messages)
    -> ROLLING CLEANUP: news kept 21 days, dedup hashes 21 days (configurable)

New CLI modes:
  --force          bypass the schedule guard (panel "Run now" uses this)
  --dry-run        do everything except sending Telegram (prints the digest)
  --no-write       do everything except touching ANY state (in-memory DB, no
                   writes, no alerts, no Tavily credit usage) - safe testing
  --dump-news[=TICKER]   print the stored news as JSON and exit
                         (used by the panel's "stored news" browse view)
  --dump-lookup    print the company lookup (names/subsidiaries/websites)
  --dump-usage     print the Tavily usage counters (panel meter)
  --rediscover[=TICKER]  force a re-discovery of the company lookup now

Reads config_local.json and secrets_local.json (both git-ignored), plus the
auto-grown company_lookup.json and tavily_usage.json (also git-ignored).
"""

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import feedparser
import requests

# Never crash printing Chinese titles on a non-UTF-8 console (e.g. Windows
# cp1252) - re-encode instead of raising.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
TAVILY_USAGE_FILE = os.path.join(BASE_DIR, "tavily_usage.json")

EASTERN = ZoneInfo("America/New_York")
# Wall-clock zone for the Chinese sources (Eastmoney search / 7x24 wire, Sina
# 7x24): their naive date strings are BEIJING time, not Eastern. Parsing them
# as Eastern made items look ~12h older than they are, so the delta filter
# (pub_dt < since_dt) could drop brand-new wire items inside the lookback
# window. Chinese fetchers normalize their dates via _zh_date_to_et().
BEIJING = ZoneInfo("Asia/Shanghai")

HEADERS = {
    "User-Agent": "PortfolioNewsUpdater/2.0 (personal stock news monitor)",
    "Accept-Encoding": "gzip, deflate",
}

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# AI provider default (DeepSeek is OpenAI-compatible).
DEFAULT_AI_BASE = "https://api.deepseek.com"
DEFAULT_AI_MODEL = "deepseek-v4-flash"

RUN_HISTORY_LIMIT = 200

# Rolling retention ("always ~3 weeks of news"):
#   - `news` rows (the browseable/translated database) are kept NEWS_RETENTION_DAYS.
#   - `seen` hashes are kept the SAME length: once a story leaves the window,
#     a recycled re-publication is treated as fresh again (the user WANTS
#     recycled news to reach them). Re-push of the same story INSIDE the
#     window is still prevented by semantic dedup (history = the seen ledger).
# Both configurable in config_local.json.
NEWS_RETENTION_DAYS = 21
SEEN_RETENTION_DAYS = 21
DB_SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB

# The company knowledge base: per-ticker Chinese/local names, aliases and
# subsidiaries (e.g. LX -> 乐信 + 分期乐 + Fenqile + Indonesia companies).
# Auto-grown: a ticker not present (or stale/sparse) is looked up online and
# the profile is written back here. Git-ignored runtime data (like news.db).
COMPANY_LOOKUP_FILE = os.path.join(BASE_DIR, "company_lookup.json")
# How often each ticker's company profile is re-searched for NEW subsidiaries
# (default 30 days = monthly). Each re-discovery costs ~1-2 Tavily searches +
# 1 AI call per ticker, and fires a Telegram alert when new names are found.
# New tickers are discovered on their first run; failed lookups retry weekly.
LOOKUP_REFRESH_DAYS = 30

# SEC identity (required by edgartools / SEC fair-access policy). NOTE: the
# email part is REQUIRED by edgartools for filing-level requests (f.obj(),
# f.text()); without it, enrichment silently fails. Replace with your own
# contact email if you prefer.
SEC_IDENTITY = "PortfolioNewsUpdater personal-use news monitor news@example.com"

# Eastmoney (东方财富) search API - public JSONP endpoint used to search
# Chinese financial news by company name (works for US-listed Chinese
# companies, e.g. code=LX pages). No key required.
EASTMONEY_SEARCH_API = "https://search-api-web.eastmoney.com/search/jsonp"

# Baidu news search page (best-effort source; degrades gracefully).
BAIDU_NEWS_URL = "https://www.baidu.com/s"

# Tavily - agent-grade news search (free plan: 1,000 credits/month).
# topic=news + days= recency filter finds fresh Chinese coverage.
TAVILY_API = "https://api.tavily.com/search"

# EXA - neural/semantic search (free plan ~1,000 credits/month). Finds pages
# ABOUT a concept, not just containing keywords - catches differently-worded
# big news the regex never sees, and related entities during discovery.
# Hard-budgeted (daily + monthly, tracked in exa_usage.json).
EXA_API = "https://api.exa.ai/search"
EXA_MAX_DAILY_SEARCHES = 32
EXA_MAX_MONTHLY_SEARCHES = 980
# Per-ticker EXA is skipped when the free sources already found at least this
# many NEW items for the ticker that run (EXA fills the gaps the free sources
# leave - busy days cost nothing, quiet days find the conceptually-relevant
# big news).
EXA_MIN_FREE_ITEMS = 6
EXA_USAGE_FILE = os.path.join(BASE_DIR, "exa_usage.json")
# Semantic macro query - one EXA news search per run for BIG China news
# (monetary, fiscal, fintech regulation, market risk) without relying on the
# exact keywords.
MACRO_EXA_QUERY = ("中国 重大经济政策 金融监管 助贷 消费金融 降息 降准 刺激 "
                   "对金融科技和消费信贷公司的影响")

# Matches CJK characters to tag items as Chinese-language.
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# --no-write mode: run the whole pipeline WITHOUT touching any state (no DB
# writes, no lookup writes, no run history, no Tavily credit usage). The DB
# is opened in-memory. Safe for repeated testing - combine with --dry-run.
NO_WRITE = False

# How many Chinese search terms we build per ticker (name + aliases +
# subsidiaries), capped so we stay polite to free APIs and keep Tavily
# credit usage low.
MAX_ZH_TERMS = 8
# Sub-queries per ticker per run for Eastmoney (1 per term).
EASTMONEY_MAX_QUERIES = 6
# Tavily free plan = 1,000 credits/month; 1 basic search = 1 credit.
# Defaults are conservative: a daily cap of 15 (~450/month worst case) plus a
# monthly hard cap of 850 as insurance, and an adaptive skip that avoids using
# Tavily for tickers the free sources already covered.
TAVILY_MAX_DAILY_SEARCHES = 15
TAVILY_MAX_MONTHLY_SEARCHES = 850
# If the free sources (GoogleNewsZH + Eastmoney + Baidu) already found at
# least this many NEW items for a ticker in the current run, skip Tavily for
# it (budget save - Tavily is the scarce resource).
TAVILY_MIN_FREE_ITEMS = 4

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
    """Atomic write: write to a temp file, then os.replace (never corrupts
    the target if the process dies mid-write - protects the lookup file and
    the usage tracker from turning into garbage that silently resets state)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _cfg_int(config, key, default):
    """Safe int() for config values - a hand-edited non-numeric value must
    not crash the whole run (falls back to the default instead)."""
    try:
        return int(config.get(key, default))
    except Exception:
        return default


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
    if NO_WRITE:
        return
    try:
        records = load_run_history()
        records.insert(0, record)
        records = records[:RUN_HISTORY_LIMIT]
        _write_json(RUN_HISTORY_FILE, records)
    except Exception as exc:
        print(f"  [error] could not write run history: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# SQLite database: dedup ledger + the real news database
# ---------------------------------------------------------------------------
def _db():
    # WAL lets the panel's --dump-news/--dump-lookup read while a run is
    # writing, and a longer busy timeout avoids SQLITE_BUSY mid-run.
    # In --no-write mode we connect to an in-memory DB so even the file
    # itself is never created/touched.
    conn = sqlite3.connect(":memory:", timeout=30) if NO_WRITE \
        else sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS last_fetched (
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (ticker, source)
        )"""
    )
    # The real news database: every item ever fetched is stored here (raw +
    # AI-enriched), so you can browse the last ~3 weeks of news even when it
    # wasn't pushed to Telegram.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            lang TEXT DEFAULT 'en',
            item_hash TEXT NOT NULL,
            title_raw TEXT,
            title_en TEXT,
            summary TEXT,
            snippet TEXT,
            category TEXT,
            importance INTEGER,
            sentiment TEXT,
            pushed INTEGER DEFAULT 0,
            reason TEXT,
            impact TEXT,
            url TEXT,
            published_at TEXT,
            first_seen TEXT NOT NULL,
            UNIQUE (ticker, source, item_hash)
        )"""
    )
    # Migration for DBs created before the `impact` column existed.
    try:
        conn.execute("ALTER TABLE news ADD COLUMN impact TEXT")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_first_seen ON news (first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker)")
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
    if NO_WRITE:
        return
    conn.execute(
        "INSERT OR REPLACE INTO last_fetched (ticker, source, fetched_at) VALUES (?,?,?)",
        (ticker, source, when),
    )
    conn.commit()


def item_hash(source, item_id, title=None):
    """
    A stable hash for an item so we can detect what's new vs already seen.

    Hashes on source + item_id (the URL / accession / link), NOT the title.
    """
    raw = f"{source}|{item_id}".strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_of(item):
    """
    Hash for an item DICT. Rescued orphan rows carry their original hash in
    '_hash' (the raw item id behind it is not reconstructible from the DB),
    everything else hashes from source + id as usual.
    """
    h = item.get("_hash")
    return h if h else item_hash(item["source"], item["id"], item.get("title", ""))


def is_new(conn, ticker, source, item_id, title):
    """Return True if this item has not been seen before."""
    h = item_hash(source, item_id, title)
    cur = conn.execute(
        "SELECT 1 FROM seen WHERE ticker=? AND source=? AND item_hash=?",
        (ticker, source, h),
    )
    return cur.fetchone() is None


def mark_seen(conn, ticker, source, item_id, title, url):
    if NO_WRITE:
        return
    h = item_hash(source, item_id, title)
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR IGNORE INTO seen (ticker, source, item_hash, title, url, first_seen) "
        "VALUES (?,?,?,?,?,?)",
        (ticker, source, h, title, url, now),
    )
    conn.commit()


def insert_news(conn, item):
    """Store a brand-new item in the news database (raw form, pre-AI)."""
    if NO_WRITE:
        return
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
    h = _hash_of(item)
    conn.execute(
        "INSERT OR IGNORE INTO news (ticker, source, lang, item_hash, title_raw, "
        "url, snippet, published_at, first_seen) VALUES (?,?,?,?,?,?,?,?,?)",
        (item["ticker"], item["source"], item.get("lang", "en"), h,
         item.get("title", ""), item.get("url", ""), item.get("snippet", ""),
         item.get("published_at", ""), now),
    )
    conn.commit()


def update_news_ai(conn, item):
    """Write the AI-enriched fields (translation, summary, score, ...) back."""
    if NO_WRITE:
        return
    h = _hash_of(item)
    conn.execute(
        "UPDATE news SET title_en=?, summary=?, category=?, importance=?, "
        "sentiment=?, reason=?, impact=? WHERE ticker=? AND source=? AND item_hash=?",
        (item.get("title_en", ""), item.get("summary", ""),
         item.get("category", "other"), item.get("importance"),
         item.get("sentiment", "neutral"), item.get("reason", ""),
         item.get("impact", ""),
         item["ticker"], item["source"], h),
    )
    conn.commit()


def mark_pushed(conn, item, pushed):
    """Set the pushed flag (1 = went to the Telegram digest)."""
    if NO_WRITE:
        return
    h = _hash_of(item)
    conn.execute(
        "UPDATE news SET pushed=? WHERE ticker=? AND source=? AND item_hash=?",
        (1 if pushed else 0, item["ticker"], item["source"], h),
    )
    conn.commit()


def list_news(conn, ticker=None, limit=300):
    """Return stored news (last ~3 weeks) as a list of dicts for browsing."""
    cols = ["ticker", "source", "lang", "title_en", "title_raw", "summary",
            "category", "importance", "sentiment", "pushed", "url",
            "published_at", "first_seen", "reason", "impact"]
    if ticker:
        rows = conn.execute(
            "SELECT ticker, source, lang, title_en, title_raw, summary, category, "
            "importance, sentiment, pushed, url, published_at, first_seen, reason, impact "
            "FROM news WHERE ticker=? ORDER BY first_seen DESC, id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker, source, lang, title_en, title_raw, summary, category, "
            "importance, sentiment, pushed, url, published_at, first_seen, reason, impact "
            "FROM news ORDER BY first_seen DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Rolling cleanup: always ~3 weeks of news, dedup hashes a bit longer
# ---------------------------------------------------------------------------
def prune_db(conn):
    """
    Rolling cleanup, run on every run:
      - DELETE news rows older than NEWS_RETENTION_DAYS (default 21) so the DB
        always holds about three weeks of news + useful data.
      - DELETE seen hashes older than SEEN_RETENTION_DAYS (default 21, same as
        news) so a recycled re-publication is treated as fresh again after the
        window (the `seen` ledger also powers the semantic-dedup history).
      - VACUUM when the file exceeds DB_SIZE_LIMIT_BYTES (deletes alone don't
        physically shrink a SQLite file).
    Returns (rows_deleted, vacuumed) for logging.
    """
    rows_deleted = 0
    vacuumed = False
    if NO_WRITE:
        return rows_deleted, vacuumed
    try:
        cutoff_news = (datetime.now(EASTERN) - timedelta(days=NEWS_RETENTION_DAYS)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        cutoff_seen = (datetime.now(EASTERN) - timedelta(days=SEEN_RETENTION_DAYS)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute("DELETE FROM news WHERE first_seen < ?", (cutoff_news,))
        rows_deleted += cur.rowcount
        cur = conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff_seen,))
        rows_deleted += cur.rowcount
        conn.commit()

        if os.path.exists(DB_FILE) and os.path.getsize(DB_FILE) > DB_SIZE_LIMIT_BYTES:
            conn.execute("VACUUM")
            vacuumed = True
    except Exception as exc:
        print(f"  [warn] DB prune failed: {exc}", file=sys.stderr)
    return rows_deleted, vacuumed


# ---------------------------------------------------------------------------
# Orphan rescue: recover items a crashed run stored but never pushed
# ---------------------------------------------------------------------------
def rescue_orphans(conn, config):
    """
    Recovery pass for the mark-seen-before-push gap: items are marked seen and
    stored in `news` during the fetch loop, but only AI-analyzed and pushed
    LATER in the run. A run that dies in between (crash, OOM on the small VM,
    AI outage at the wrong moment) leaves rows with importance IS NULL that
    are ALREADY in the seen ledger - no later run picks them up (is_new()
    says no) and they silently never reach a digest.

    This runs BEFORE the fetch loop, so everything it finds was stored by a
    PREVIOUS run. Re-queued items go through the normal pipeline: AI analysis,
    importance floor, AI veto, per-ticker caps - nothing is pushed blindly.
    Rows the previous run deliberately stored-only (importance set, pushed=0)
    are NOT touched. Bounded by max_items_per_run, newest first, limited to
    the retention window.
    """
    if NO_WRITE or conn is None:
        return []
    cutoff = (datetime.now(EASTERN)
              - timedelta(days=NEWS_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    limit = _cfg_int(config, "max_items_per_run", 40)
    try:
        rows = conn.execute(
            "SELECT ticker, source, item_hash, title_raw, url, snippet, lang, "
            "published_at, first_seen FROM news "
            "WHERE importance IS NULL AND pushed=0 AND first_seen >= ? "
            "ORDER BY first_seen DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    except Exception as exc:
        print(f"  [warn] orphan-rescue query failed: {exc}", file=sys.stderr)
        return []
    items = []
    for (ticker, source, h, title, url, snippet, lang, published_at,
         first_seen) in rows:
        items.append({
            "source": source,
            "ticker": ticker,
            # The original raw item id is gone (only its hash is stored), so
            # '_hash' carries the DB hash - update_news_ai / mark_pushed use
            # it to hit the right row. 'id' is just a placeholder.
            "id": h,
            "_hash": h,
            "title": title or "",
            "url": url or "",
            "date": published_at or "",
            "published_at": published_at or "",
            "first_seen": first_seen,
            "lang": lang or "en",
            "snippet": snippet or "",
            "rescued": True,
        })
    return items


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def is_chinese(text):
    return bool(text and CJK_RE.search(text))


# Characters that commonly continue a 2-char CJK term into an UNRELATED word
# (e.g. "元保" inside "元保险" or "美元保证金" - 保 continued by 险/证/金).
# A 2-char term followed by one of these is treated as a coincidental
# substring, not a real company mention.
CONTINUATION_BLOCK = set("险证金单券押汇据费额契担票据息利收付")


def _term_in_text(term, text):
    """
    Does 'term' appear in 'text' as a real mention (not a coincidental
    substring)? For 2-char CJK terms we reject matches where the character
    right after the term extends it into a different common word.
    """
    if not term or not text:
        return False
    idx = text.find(term)
    while idx >= 0:
        after = text[idx + len(term):idx + len(term) + 1]
        if not (len(term) == 2 and is_chinese(term) and after and after in CONTINUATION_BLOCK):
            return True
        idx = text.find(term, idx + 1)
    return False


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _parse_pub(s, naive_tz=EASTERN):
    """
    Parse a publish-date string to an aware datetime (Eastern), or None.
    Handles the formats the sources actually emit:
      - ISO 8601 with or without fractional seconds, 'Z' or offsets
        (Tavily: 2026-08-15T10:22:33.123Z)
      - 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' / 'YYYY/MM/DD ...'
      - RFC 822 / RFC 1123 (Google News RSS: 'Sat, 15 Aug 2026 05:00:00 GMT')
    Naive dates are assumed to be 'naive_tz' (Eastern by default; Chinese
    sources pass Asia/Shanghai - see _zh_date_to_et).
    """
    if not s:
        return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz)
        return dt.astimezone(EASTERN)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S",
                "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                "%a, %d %b %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=naive_tz)
            return dt.astimezone(EASTERN)
        except Exception:
            continue
    return None


def _normalize_pub(s, naive_tz=EASTERN):
    """ISO-ish string for the DB, or ''."""
    dt = _parse_pub(s, naive_tz=naive_tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _zh_date_to_et(s):
    """
    Normalize a Chinese-source date string (Beijing wall-clock: Eastmoney
    search dates, Eastmoney 7x24 showTime, Sina 7x24 create_time) to an
    Eastern-time 'YYYY-MM-DD HH:MM:SS' string ('' if unparseable). Doing this
    at the fetcher boundary means every downstream consumer - the delta
    filters, published_at storage, digest ordering - sees consistent ET.
    """
    return _normalize_pub(s, naive_tz=BEIJING)


# ---------------------------------------------------------------------------
# Company lookup + auto-discovery (the "alpha" config)
# ---------------------------------------------------------------------------
def build_zh_terms(meta):
    """
    Chinese search terms from a company profile: name_zh + aliases_zh +
    subsidiaries_zh (e.g. 乐信, 乐信集团, 分期乐, 桔子理财). Capped at
    MAX_ZH_TERMS, de-duplicated. Used for Google News zh / Eastmoney /
    Baidu (the Chinese sources where subsidiary news actually shows up).
    """
    terms = []
    if meta:
        for key in ("name_zh",):
            v = str(meta.get(key) or "").strip()
            if v and v not in terms:
                terms.append(v)
        for key in ("aliases_zh", "subsidiaries_zh"):
            for v in (meta.get(key) or []):
                v = str(v or "").strip()
                if v and v not in terms:
                    terms.append(v)
    return terms[:MAX_ZH_TERMS]


def build_en_terms(meta):
    """
    Non-Chinese search terms from a company profile: name_en +
    subsidiaries_other (e.g. Fenqile, Fenqile Indonesia). Used for Tavily
    and Google News EN, so subsidiary news is found even when it never
    mentions the ticker symbol or the Chinese name.
    """
    terms = []
    if meta:
        v = str(meta.get("name_en") or "").strip()
        if v and v not in terms:
            terms.append(v)
        for v in (meta.get("subsidiaries_other") or []):
            v = str(v or "").strip()
            if v and v not in terms:
                terms.append(v)
    return terms[:5]


def load_lookup():
    return _read_json(COMPANY_LOOKUP_FILE, {})


def save_lookup(data):
    if NO_WRITE:
        return
    _write_json(COMPANY_LOOKUP_FILE, data)


def seed_lookup_from_config(config, lookup):
    """
    Merge config_local.json ticker_meta into the lookup (config wins, fills
    gaps). This keeps the knowledge base alive even if the lookup file is
    fresh/deleted, and lets the panel's explicit overrides propagate.
    Returns (lookup, changed).
    """
    meta_map = config.get("ticker_meta", {}) or {}
    changed = False
    for ticker, meta in meta_map.items():
        entry = lookup.setdefault(ticker, {})
        for k, v in (meta or {}).items():
            # Only mark changed when a value actually differs, so we don't
            # rewrite the lookup file on every single run.
            if v not in (None, "", [], {}) and entry.get(k) != v:
                entry[k] = v
                changed = True
        if "last_updated" not in entry:
            entry["last_updated"] = datetime.now(EASTERN).strftime("%Y-%m-%d")
            changed = True
    return lookup, changed


def _parse_json_object(content):
    """Best-effort extraction of a JSON object from an AI response."""
    if not content:
        return None
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _merge_lookup_entry(existing, parsed):
    """Merge AI-extracted discovery data into an existing lookup entry."""
    merged = dict(existing or {})
    for k in ("name_zh", "name_en", "website", "news_url"):
        v = str((parsed or {}).get(k) or "").strip()
        if v:
            merged[k] = v
    for k in ("aliases_zh", "subsidiaries_zh", "subsidiaries_other", "keywords"):
        vals = [str(x).strip() for x in (parsed or {}).get(k) or [] if str(x).strip()]
        old = [str(x).strip() for x in (merged.get(k) or [])]
        merged[k] = list(dict.fromkeys(old + vals))[:12]
    sw = (parsed or {}).get("subsidiary_websites")
    if isinstance(sw, dict):
        old_sw = merged.get("subsidiary_websites") or {}
        old_sw.update({str(k): str(v) for k, v in sw.items() if k and v})
        merged["subsidiary_websites"] = old_sw
    return merged


def build_site_domains(meta):
    """
    Domains from the profile's websites (official site, news page, subsidiary
    sites) - used for `site:` Google News queries so news posted on the
    company's OWN websites is caught even if no news outlet covers it.
    """
    domains = []
    for key in ("website", "news_url"):
        v = str((meta or {}).get(key) or "").strip()
        if v:
            d = re.sub(r"^https?://(www\.)?", "", v).strip("/").split("/")[0]
            if d and d not in domains:
                domains.append(d)
    for v in ((meta or {}).get("subsidiary_websites") or {}).values():
        v = str(v or "").strip()
        if v:
            d = re.sub(r"^https?://(www\.)?", "", v).strip("/").split("/")[0]
            if d and d not in domains:
                domains.append(d)
    return domains[:3]


def discover_company(ticker, config, secrets, existing=None):
    """
    THE lookup step: for a ticker that is missing / stale / too sparse in
    company_lookup.json, search the web for its Chinese/local names and its
    subsidiaries (the real alpha - e.g. LX -> 分期乐, Fenqile, Indonesia
    companies), extract a structured profile with AI, and WRITE IT BACK to
    the lookup file so it is used from then on.

    Discovery sources, cheapest first:
      1. Tavily (general topic, 1 search) - best for reference lookups.
      2. Eastmoney search on the ticker symbol (free).
      3. Google News zh on "<TICKER> 股票" (free).
    Returns the (possibly minimal) entry.
    """
    existing = existing or {}
    name_hint = str(existing.get("name_zh") or "").strip()
    ai_key = secrets.get("ai_api_key", "")
    snippets = []

    # 1) EXA (neural) - the best discovery source: finds related companies /
    #    subsidiaries semantically (e.g. 深圳市分期乐网络科技 for LX, 陆金申华
    #    for LU) that keyword search never surfaces. Category "company" returns
    #    company-profile pages; small text snippets for the AI grounding.
    if secrets.get("exa_api_key"):
        q = f"{name_hint or ticker} 子公司 旗下品牌 相关企业 subsidiaries related companies"
        res = fetch_exa(q, secrets, config, since_dt=None, limit=6,
                        with_text=True, category="company")
        if res:
            snippets.extend(f"- {it['title']}: {it.get('snippet', '')[:200]}" for it in res)
            print(f"  [discovery] {ticker}: EXA returned {len(res)} company result(s) "
                  f"for profile lookup.")

    # 1b) Tavily general - fallback / enrichment (only if EXA gave us nothing).
    if not snippets and secrets.get("tavily_api_key"):
        queries = []
        if name_hint:
            queries.append(f"{name_hint} 公司 子公司 旗下品牌 subsidiaries brands")
        else:
            queries.append(f"{ticker} company profile subsidiaries brands stock")
            queries.append(f"{ticker} 上市公司 子公司 旗下品牌")
        for q in queries[:2]:
            res = fetch_tavily(q, secrets, config, since_dt=None, limit=6, topic="general")
            if res:
                snippets.extend(f"- {it['title']}: {it.get('snippet', '')[:200]}" for it in res)
                print(f"  [discovery] {ticker}: Tavily returned {len(res)} result(s) "
                      f"for profile lookup ({q[:50]}...).")
                if len(snippets) >= 10:
                    break

    # 2) Free fallbacks / enrichment (only if Tavily gave us nothing).
    if not snippets:
        em_terms = [ticker] + ([name_hint] if name_hint else [ticker + " 股票"])
        em = fetch_eastmoney_search(ticker, since_dt=None, limit=6, terms=em_terms)
        if em:
            snippets.extend(f"- {it['title']}" for it in em)
    if not snippets:
        gz = fetch_rss(google_news_url(f"{ticker} 股票", "zh"), ticker,
                       source="GoogleNewsZH", lang="zh")
        if gz:
            snippets.extend(f"- {it['title']}" for it in gz[:6])

    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    entry = {
        "name_zh": name_hint,
        "name_en": str(existing.get("name_en") or "").strip(),
        "aliases_zh": list(existing.get("aliases_zh", []) or []),
        "subsidiaries_zh": list(existing.get("subsidiaries_zh", []) or []),
        "subsidiaries_other": list(existing.get("subsidiaries_other", []) or []),
        "keywords": list(existing.get("keywords", []) or []),
        # Carry discovered websites forward! On the 30-day re-discovery the
        # AI's new result set often won't re-emit the company URL - without
        # this, a refresh would silently delete the websites and the
        # GoogleNewsSite (site:) source would go dark for that ticker.
        "website": str(existing.get("website") or "").strip(),
        "news_url": str(existing.get("news_url") or "").strip(),
        "subsidiary_websites": dict(existing.get("subsidiary_websites") or {}),
        "last_updated": today,
        "lookup_attempted": True,
    }

    if ai_key and snippets:
        base = config.get("ai_base_url") or DEFAULT_AI_BASE
        model = config.get("ai_model") or DEFAULT_AI_MODEL
        prompt = (
            f"You are a corporate research assistant. Below are search results "
            f"for the stock {ticker}"
            f"{(' (Chinese name: ' + name_hint + ')') if name_hint else ''}.\n"
            "Extract a structured profile of this company:\n"
            "  name_zh: official Chinese name (or '' if unknown)\n"
            "  name_en: official English name\n"
            "  aliases_zh: list of other Chinese names/abbreviations\n"
            "  subsidiaries_zh: list of Chinese subsidiary/brand names - "
            "include brands, apps, fintech platforms, BANKS, brokers, "
            "overseas/HK entities and any subsidiary mentioned (e.g. 分期乐 "
            "for LexinFintech, 平安普惠 for Lufax)\n"
            "  subsidiaries_other: list of non-Chinese subsidiaries/brands "
            "(e.g. Fenqile, Temu, LU Global)\n"
            "  website: the official corporate website URL (or '' if unknown)\n"
            "  news_url: the official news / press-release page URL (or '' if unknown)\n"
            "  subsidiary_websites: JSON object mapping each subsidiary/brand "
            "name to its website URL when visible in the results (or {})\n"
            "  keywords: 3-8 search keywords (Chinese and English names/brands) "
            "that will be used to find news about this company AND its "
            "subsidiaries\n"
            "ONLY include names and URLs you can support from the search results "
            "below. If something is unclear, omit it rather than guessing.\n"
            "Return ONLY a JSON object with exactly these keys.\n\n"
            "SEARCH RESULTS:\n" + "\n".join(snippets[:12])
        )
        content = _chat(base, model, ai_key, "You are a precise JSON-returning assistant.", prompt)
        parsed = _parse_json_object(content) if content else None
        if parsed:
            entry = _merge_lookup_entry(entry, parsed)
            print(f"  [discovery] {ticker}: extracted profile "
                  f"(zh={entry.get('name_zh') or '?'}, "
                  f"site={entry.get('website') or '?'}, "
                  f"subs_zh={entry.get('subsidiaries_zh')}, "
                  f"subs_other={entry.get('subsidiaries_other')})")
        else:
            print(f"  [discovery] {ticker}: AI extraction failed - keeping minimal profile.", file=sys.stderr)
    else:
        print(f"  [discovery] {ticker}: no AI key or no search results - "
              f"keeping minimal profile (will retry when stale).", file=sys.stderr)

    # If discovery produced nothing usable (AI refused to guess, Tavily down),
    # backdate last_updated so we retry within ~a week instead of waiting out
    # the full refresh window - otherwise a new ticker could sit with zero
    # Chinese-source coverage for 30 days.
    if not entry.get("name_zh") and not (entry.get("subsidiaries_zh") or entry.get("subsidiaries_other")):
        refresh_days = _cfg_int(config, "lookup_refresh_days", LOOKUP_REFRESH_DAYS)
        retry_on = datetime.now(EASTERN) - timedelta(days=max(1, refresh_days - 7))
        entry["last_updated"] = retry_on.strftime("%Y-%m-%d")
        print(f"  [discovery] {ticker}: nothing usable found - will retry lookup "
              f"on {retry_on.strftime('%Y-%m-%d')}.")

    # New-subsidiary-discovered alert: when discovery found names the lookup
    # did not have, tell the user on Telegram - this is the "wait, they own
    # Temu / a bank in Hong Kong?" moment, and it is exactly the alpha they
    # want to know about.
    existing_subs = set((existing.get("subsidiaries_zh") or [])
                        + (existing.get("subsidiaries_other") or []))
    new_subs = [s for s in (entry.get("subsidiaries_zh") or [])
                + (entry.get("subsidiaries_other") or []) if s not in existing_subs]
    new_name = bool(entry.get("name_zh")) and entry.get("name_zh") != name_hint
    if (new_subs or new_name) and not NO_WRITE:
        token = secrets.get("telegram_bot_token", "")
        chat_id = secrets.get("telegram_chat_id", "")
        if token and chat_id:
            msg = [f"🧩 New company info discovered for {ticker}"]
            if new_name:
                msg.append(f"Chinese name: {entry['name_zh']}"
                           + (f" ({entry.get('name_en')})" if entry.get("name_en") else ""))
            if new_subs:
                msg.append("New subsidiaries: " + ", ".join(new_subs))
            msg.append("News searches will now cover these. Fix in the panel (Step 3) if wrong.")
            send_telegram(token, chat_id, "\n".join(msg))
            print(f"  [discovery] {ticker}: sent Telegram alert "
                  f"({len(new_subs)} new subsidiary name(s)).")

    # Persist to the lookup file (create it if missing).
    lookup = load_lookup()
    lookup[ticker] = entry
    save_lookup(lookup)
    return entry


def ensure_company_meta(ticker, config, secrets, force=False):
    """
    The per-startup entry point: look the ticker up in company_lookup.json
    (seeded from config ticker_meta), run discovery when it is missing,
    stale, too sparse (no subsidiaries known), or force=True (--rediscover),
    then return the effective profile (discovered entry overlaid with
    explicit config overrides).
    """
    lookup = load_lookup()
    lookup, seed_changed = seed_lookup_from_config(config, lookup)
    if seed_changed:
        # Persist config-seeded entries so the file exists even for tickers
        # that don't need (re-)discovery (e.g. LX already has subsidiaries).
        save_lookup(lookup)

    entry = lookup.get(ticker)
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    refresh_days = _cfg_int(config, "lookup_refresh_days", LOOKUP_REFRESH_DAYS)

    needs_discovery = force or entry is None
    if entry is not None:
        attempted = entry.get("lookup_attempted")
        sparse = not (entry.get("subsidiaries_zh") or entry.get("subsidiaries_other"))
        stale = False
        lu = str(entry.get("last_updated") or "")
        if lu:
            try:
                # Compare date-only to avoid naive/aware datetime mismatches
                # (a TypeError here used to silently mark every entry stale).
                lu_dt = datetime.strptime(lu, "%Y-%m-%d").date()
                cutoff_date = (datetime.now(EASTERN) - timedelta(days=refresh_days)).date()
                stale = lu_dt < cutoff_date
            except Exception:
                stale = True
        # Discover when: stale, or sparse AND never attempted. A seeded entry
        # that already has subsidiaries (from config ticker_meta) is complete
        # enough - no need to burn a search + AI call re-discovering it. A
        # sparse entry that WAS attempted stays until it goes stale, so we
        # don't burn Tavily credits re-searching every single run.
        if not force and (stale or (sparse and not attempted)):
            needs_discovery = True

    if needs_discovery:
        print(f"  [lookup] {ticker}: {'FORCED ' if force else ''}"
              f"not in company lookup "
              f"{'(or stale/sparse)' if entry else ''} - searching and populating...")
        entry = discover_company(ticker, config, secrets, existing=entry or {})
    elif entry is not None:
        print(f"  [lookup] {ticker}: from lookup "
              f"({entry.get('name_zh') or '?'}"
              f"{' + ' + str(len(entry.get('subsidiaries_zh') or []) + len(entry.get('subsidiaries_other') or [])) + ' subsidiary term(s)' if entry.get('subsidiaries_zh') or entry.get('subsidiaries_other') else ''})")

    # Explicit config ticker_meta always overrides the discovered entry.
    cfg_meta = config.get("ticker_meta", {}).get(ticker, {}) or {}
    merged = dict(entry or {})
    for k, v in cfg_meta.items():
        if v not in (None, "", [], {}):
            merged[k] = v
    return merged


# ---------------------------------------------------------------------------
# SEC EDGAR (via edgartools)
# ---------------------------------------------------------------------------
# Form types that matter to an investor. Includes the company's own reports
# AND ownership filings. Note: US-listed Chinese ADRs are FOREIGN PRIVATE
# ISSUERS - they file 6-K (all material events) and 20-F (annual), NOT 8-K
# or 10-K, and they are exempt from Section 16, so Form 4 (insider trades)
# never appears for them; form "3" (new insider initial ownership) does.
SEC_FORMS = [
    "8-K", "8-K/A", "10-Q", "10-K", "6-K", "6-K/A", "20-F", "F-1", "424B3",
    "424B4", "DEF 14A", "3", "4", "144",
    "SCHEDULE 13D", "SC 13D", "SCHEDULE 13D/A", "SC 13D/A",
    "SCHEDULE 13G", "SC 13G", "SCHEDULE 13G/A", "SC 13G/A",
    "SC 13E-3", "SC 13E-4", "25", "13F-HR",
]


def _extract_sec_substance(form, text):
    """
    Best-effort extraction of WHAT a filing is about from its primary
    document text. Returns (title_suffix, snippet); ('', '') when nothing
    useful is found (caller keeps the plain "form filed date" title).

      - 6-K (the workhorse for Chinese ADRs): the "INFORMATION CONTAINED IN
        THIS REPORT ON FORM 6-K" paragraph (e.g. "...issued a press release
        announcing financial results...").
      - 8-K / 8-K/A (US companies): the Item codes (Item 1.01, 5.02, ...).
      - Form 4 / Form 3 (US companies): insider name + buy/sell + shares.
    """
    if not text:
        return "", ""
    flat = re.sub(r"\s+", " ", text)
    if form in ("6-K", "6-K/A"):
        m = re.search(r"INFORMATION CONTAINED IN THIS REPORT ON FORM 6-K\s*(.+)",
                      flat, re.IGNORECASE)
        para = (m.group(1).strip() if m else "")
        if len(para) < 20:
            return "", ""
        first_sent = re.split(r"(?<=[.!?])\s+", para)[0].strip()
        return first_sent[:130], para[:300]
    if form in ("8-K", "8-K/A"):
        found = []
        for it in re.findall(r"Item\s+(\d\.\d{2}(?:\([a-z]\))?)", flat, re.IGNORECASE):
            code = it.strip()
            if code not in found:
                found.append(code)
        if found:
            return "Item " + ", ".join(found[:6]), "Items: " + ", ".join(found[:8])
        return "", ""
    if form in ("4", "3"):
        m_name = re.search(r"rptOwnerName[^>]*>\s*([^<]+)", flat, re.IGNORECASE)
        name = m_name.group(1).strip() if m_name else ""
        if form == "4":
            code = re.search(r"transactionCode[^>]*>\s*([A-Z])", flat, re.IGNORECASE)
            code = code.group(1).upper() if code else ""
            shares = re.search(r"transactionShares[^>]*>.*?<value>\s*([\d,.]+)",
                               flat, re.IGNORECASE)
            sh = shares.group(1).strip() if shares else ""
            label = {"P": "BOUGHT", "S": "SOLD"}.get(code, code or "")
            parts = [p for p in (name, label, (sh + " sh" if sh else "")) if p]
            return ("Form 4: " + " ".join(parts)) if parts else "", ""
        return (f"Form 3: {name}") if name else "", ""
    return "", ""


def fetch_sec_filings(ticker, since_dt, conn=None):
    """
    SEC filings for this ticker filed since since_dt, using edgartools.

    'conn' (the DB) lets us skip the expensive primary-document fetch for
    filings already seen in previous runs - the delta date filter already
    limits the list, but after a long gap or a re-run we avoid re-downloading
    documents just to re-dedupe them.

    Returns a list of items, or None if the fetch FAILED (so the caller knows
    NOT to advance the delta timestamp and risk missing news).
    """
    if not SEC_AVAILABLE:
        print("  [warn] edgartools not installed - skipping SEC.")
        return None
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
            if acc and cik:
                url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                       f"{acc.replace('-', '')}/")
            else:
                url = ""
            title = f"{company_name} - {form} filed {filed}"
            snippet = ""
            # Enrichment: pull the primary document text for the forms where
            # the substance matters (6-K for ADRs; 8-K/Form 4/3 for US names)
            # and extract WHAT happened. Fully guarded - any failure keeps the
            # plain title (never break the SEC fetch). Skip the download for
            # filings already seen in a previous run (deduped anyway).
            already_seen = (conn is not None
                            and not is_new(conn, ticker, "SEC",
                                           acc or f"{form}-{filed}", title))
            if form in ("6-K", "6-K/A", "8-K", "8-K/A", "4", "3") and not already_seen:
                try:
                    text = f.text()
                    suffix, snippet = _extract_sec_substance(form, text)
                    if suffix:
                        title = f"{company_name} - {form}: {suffix}"
                except Exception as exc:
                    print(f"  [warn] SEC {ticker} {form} enrichment failed: {exc}",
                          file=sys.stderr)
            items.append({
                "source": "SEC",
                "ticker": ticker,
                "id": acc or f"{form}-{filed}",
                "title": title,
                "url": url,
                "date": filed,
                "lang": "en",
                "snippet": snippet,
                "form": form,
                "company": company_name,
            })
    except Exception as exc:
        print(f"  [error] SEC {ticker}: {exc}", file=sys.stderr)
        return None
    return items


SEC_VALIDATE_FILE = os.path.join(BASE_DIR, "sec_validate.json")


def sec_validate_due():
    """Run the (informational) SEC coverage check at most once a week -
    it costs one EDGAR request per ticker, which adds up at 15 tickers."""
    data = _read_json(SEC_VALIDATE_FILE, {})
    last = str(data.get("last") or "")
    if not last:
        return True
    try:
        return (datetime.strptime(last, "%Y-%m-%d").date()
                < (datetime.now(EASTERN) - timedelta(days=7)).date())
    except Exception:
        return True


def validate_sec_tickers(tickers):
    """Log which tickers resolve in SEC EDGAR (non-fatal)."""
    if not SEC_AVAILABLE:
        print("  [warn] edgartools not installed - skipping SEC coverage check.")
        return set()
    resolved = set()
    unresolved = []
    try:
        set_identity(SEC_IDENTITY)
        for ticker in tickers:
            try:
                company = Company(ticker)
                cik = str(getattr(company, "cik", "") or "").strip()
                name = str(getattr(company, "name", "") or ticker)
                if cik:
                    resolved.add(ticker)
                    print(f"  [sec] {ticker}: OK (CIK {cik}, {name})")
                else:
                    unresolved.append(ticker)
            except Exception as exc:
                unresolved.append(ticker)
                print(f"  [sec] {ticker}: could not resolve in SEC EDGAR ({exc})",
                      file=sys.stderr)
    except Exception as exc:
        print(f"  [warn] SEC coverage check failed: {exc}", file=sys.stderr)
        return resolved
    if unresolved:
        print(f"  [sec] No SEC coverage for: {', '.join(unresolved)} "
              f"(these tickers will only get news feeds).")
    return resolved


# ---------------------------------------------------------------------------
# RSS / Google News (via feedparser)
# ---------------------------------------------------------------------------
def fetch_rss(url, ticker, since_dt=None, source="RSS", lang="en"):
    """
    Fetch + parse an RSS/Atom feed. Uses requests (with a timeout!) instead of
    feedparser's own fetch, because feedparser silently returns an empty feed
    on network errors (which used to advance the delta and permanently skip
    that window) and has no timeout (a hung host stalled the whole cron run).

    Returns a list of items, or None on a hard failure (request error,
    non-200, or an unparseable body). A 200 response that is a legitimately
    empty feed returns [] (delta advances - that is really "no news").
    """
    items = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        for entry in parsed.entries[:30]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "") or url
            if not title:
                continue
            # Normalize the publish time from the parsed struct_time (UTC ->
            # Eastern, 'YYYY-MM-DD HH:MM:SS') so sorting/display work; keep
            # the raw string as a fallback.
            pub_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            pub = entry.get("published", "") or entry.get("updated", "")
            date = pub
            if pub_parsed:
                pub_dt = datetime(*pub_parsed[:6], tzinfo=ZoneInfo("UTC"))
                date = pub_dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
                if since_dt is not None and pub_dt < since_dt.astimezone(ZoneInfo("UTC")):
                    continue
            items.append({
                "source": source,
                "ticker": ticker,
                "id": link or title,
                "title": title,
                "url": link,
                "date": date,
                "feed": url,
                # Detect the language per item (a zh feed can carry EN titles
                # and vice versa); the caller's 'lang' is only a hint.
                "lang": "zh" if is_chinese(title) else "en",
                "snippet": "",
            })
        if not items and parsed.get("bozo"):
            # 200 OK but the body was not a parseable feed (HTML error page,
            # captcha, redirect page) -> treat as a failure, don't advance.
            print(f"  [warn] RSS {url}: unparseable feed body (bozo).")
            return None
        return items
    except Exception as exc:
        print(f"  [error] RSS {url}: {exc}", file=sys.stderr)
        return None


def google_news_url(query, lang="en"):
    """Google News RSS URL. For zh we force Chinese results (hl/gl/ceid)."""
    q = urllib.parse.quote_plus(query)
    if lang == "zh":
        return (f"https://news.google.com/rss/search?q={q}"
                f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    return f"https://news.google.com/rss/search?q={q}"


# ---------------------------------------------------------------------------
# Eastmoney (东方财富) - Chinese financial news search API (no key needed)
# ---------------------------------------------------------------------------
def fetch_eastmoney_search(query, since_dt=None, limit=10, terms=None):
    """
    Search Chinese financial news on Eastmoney by keyword (e.g. 分期乐).
    Public JSONP API; returns a list of items, or None on a hard failure.

    'terms' (the Chinese search terms for this ticker) is used as a precision
    filter: results whose title contains none of the terms are dropped, so
    broad keyword matches (e.g. "乐信" inside an unrelated article) never
    reach the AI or the DB.
    """
    if terms is None:
        terms = [query]
    param = {
        "uid": "",
        "keyword": query,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "time",          # newest first
                "pageIndex": 1,
                "pageSize": limit,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    try:
        resp = requests.get(
            EASTMONEY_SEARCH_API,
            params={"cb": "cb", "param": json.dumps(param, ensure_ascii=False)},
            headers=HEADERS, timeout=20,
        )
        resp.raise_for_status()
        text = resp.text.strip()
        m = re.match(r"^[^(]*\((.*)\)\s*$", text, re.DOTALL)
        try:
            if m:
                data = json.loads(m.group(1))
            else:
                # Some deployments return plain JSON without the JSONP wrapper.
                data = json.loads(text)
        except Exception as exc:
            # A 200 response we can't parse is a fetch failure, NOT "no news":
            # return None so the delta does not advance past this window.
            print(f"  [warn] Eastmoney search '{query}': unparseable body ({exc}).",
                  file=sys.stderr)
            return None
        arts = data.get("result", {}).get("cmsArticleWebOld", []) or []
        items = []
        for a in arts:
            title = strip_tags(a.get("title", ""))
            if not title:
                continue
            # Precision filter: the title must really mention one of the
            # ticker's Chinese names/subsidiaries (not a coincidental
            # substring like 元保 inside 元保险 / 美元保证金).
            if not any(_term_in_text(t, title) for t in terms):
                continue
            url = a.get("url", "") or ""
            content = strip_tags(a.get("content", ""))
            # Eastmoney dates are Beijing wall-clock - normalize to ET.
            date = _zh_date_to_et(a.get("date", ""))
            pub_dt = _parse_pub(date)
            if since_dt is not None and pub_dt and pub_dt < since_dt:
                continue
            items.append({
                "source": "Eastmoney",
                "ticker": "",  # filled by caller
                "id": url or title,
                "title": title,
                "url": url,
                "date": date,
                "lang": "zh" if is_chinese(title) else "en",
                "snippet": content[:300],
            })
        return items
    except Exception as exc:
        print(f"  [error] Eastmoney search '{query}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Baidu news (best-effort; degrades gracefully)
# ---------------------------------------------------------------------------
def fetch_baidu_news(query, since_dt=None, limit=10, terms=None):
    """
    Baidu news search page (tn=news). HTML is parsed with a regex on the
    news-title blocks. Best-effort source: an unparseable page returns []
    (we still advance the delta), a network failure returns None (we don't).
    Note: Baidu often redirects automated requests to a CAPTCHA page
    (wappass.baidu.com), so expect few or no items from server IPs.
    """
    headers = dict(HEADERS)
    headers["Accept-Language"] = "zh-CN,zh;q=0.9"
    try:
        resp = requests.get(
            BAIDU_NEWS_URL,
            params={"tn": "news", "word": query, "rtt": 1, "bsst": 1, "cl": 2},
            headers=headers, timeout=20,
        )
        resp.raise_for_status()
        items = []
        pattern = re.compile(
            r'<h3[^>]*class="[^"]*news-title[^"]*"[^>]*>.*?'
            r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
        for m in pattern.finditer(resp.text):
            url, raw_title = m.group(1), strip_tags(m.group(2))
            if not raw_title:
                continue
            if terms and not any(_term_in_text(t, raw_title) for t in terms):
                continue
            items.append({
                "source": "Baidu",
                "ticker": "",
                "id": url or raw_title,
                "title": raw_title,
                "url": url,
                "date": "",
                "lang": "zh" if is_chinese(raw_title) else "en",
                "snippet": "",
            })
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        print(f"  [error] Baidu news '{query}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Tavily - agent-grade news search (free plan: 1,000 credits/month)
# ---------------------------------------------------------------------------
def tavily_usage_today():
    """Usage tracker: daily count (resets at midnight ET) + monthly count
    (resets on the 1st). Both caps are enforced so the free 1,000/month
    plan can never be blown through."""
    data = _read_json(TAVILY_USAGE_FILE, {})
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    month = today[:7]
    if data.get("date") != today:
        data["date"] = today
        data["count"] = 0
    if data.get("month") != month:
        data["month"] = month
        data["month_count"] = 0
    return data


def fetch_tavily(query, secrets, config, since_dt=None, limit=8, topic="news",
                 terms=None):
    """
    Tavily search (default topic=news for fresh news; topic=general for
    company/lookup research). One "basic" search = 1 credit on the free plan.

    'terms' = the ticker's search terms (zh + en). When given, results whose
    title/snippet mention NONE of them are DROPPED - Tavily can return
    completely unrelated items (Heineken buybacks for LX, random tech news
    for QFIN), and without a precision filter they would be stored + analyzed
    (wasted tokens). Discovery calls pass no terms (it WANTS broad results).

    Budget (configurable):
      - daily cap  tavily_max_daily_searches   (default 15)
      - monthly cap tavily_max_monthly_searches (default 850)
    Both caps are checked BEFORE the call, so the free allowance is never
    blown. Returns a list of items, or None on a hard failure / when a cap is
    reached (None - NOT [] - so the caller does not advance the delta past a
    window we never actually searched).
    """
    key = secrets.get("tavily_api_key", "")
    if not key:
        return []
    daily_cap = _cfg_int(config, "tavily_max_daily_searches", TAVILY_MAX_DAILY_SEARCHES)
    monthly_cap = _cfg_int(config, "tavily_max_monthly_searches", TAVILY_MAX_MONTHLY_SEARCHES)
    usage = tavily_usage_today()
    if usage.get("count", 0) >= daily_cap:
        print(f"  [warn] Tavily daily cap ({daily_cap}) reached - skipping (delta not advanced).")
        return None
    if usage.get("month_count", 0) >= monthly_cap:
        print(f"  [warn] Tavily monthly cap ({monthly_cap}) reached - skipping (delta not advanced).")
        return None
    try:
        payload = {
            "api_key": key,
            "query": query,
            "topic": topic,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        if topic == "news":
            days = 1
            if since_dt:
                days = max(1, min(7, (datetime.now(EASTERN) - since_dt).days + 1))
            payload["days"] = days
        resp = requests.post(TAVILY_API, json=payload, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        # Count the credit ONLY after a successful call (and never in
        # --no-write mode, so safe testing doesn't consume the budget).
        if not NO_WRITE:
            usage["count"] += 1
            usage["month_count"] += 1
            _write_json(TAVILY_USAGE_FILE, usage)
        items = []
        for r in data.get("results", []) or []:
            title = (r.get("title") or "").strip()
            url = r.get("url", "") or ""
            if not title:
                continue
            content = (r.get("content") or "")[:300]
            # Precision filter (news path only): drop results that mention
            # none of the ticker's terms - kills Tavily's off-topic filler.
            if terms:
                hay = f"{title} {content}"
                if not any(_term_in_text(t, hay) for t in terms):
                    continue
            date = r.get("published_date", "") or ""
            pub_dt = _parse_pub(date)
            if topic == "news" and since_dt is not None and pub_dt and pub_dt < since_dt:
                continue
            items.append({
                "source": "Tavily",
                "ticker": "",
                "id": url or title,
                "title": title,
                "url": url,
                "date": date,
                "lang": "zh" if is_chinese(title) else "en",
                "snippet": content,
            })
        return items
    except Exception as exc:
        print(f"  [error] Tavily search '{query}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# China macro watch - the "I HAVE TO KNOW" tier (huge policy/market news)
# ---------------------------------------------------------------------------
# A FREE regex gate decides whether an item is macro-relevant - NO AI cost to
# filter. Only the few items that match ever reach the AI (one tiny batched
# call per run). This is how "huge Chinese news" gets delivered without
# burning tokens on everything else.
MACRO_KEYWORDS = [
    # Monetary policy
    r"降息", r"加息", r"降准", r"LPR", r"贷款市场报价利率", r"中期借贷便利|MLF",
    r"逆回购", r"存款准备金", r"货币政策", r"利率下调|下调利率|降低利率",
    # Fiscal / stimulus
    r"刺激(经济|消费|内需|市场)", r"万亿", r"特别国债", r"专项债",
    r"财政(刺激|政策)", r"扩大内需", r"消费券", r"国常会", r"政治局会议",
    r"中央经济工作会议", r"中央金融工作会议",
    # Fintech / consumer-loan regulation (the names that matter to the user)
    r"助贷", r"网络小额贷款|网络小贷|互联网小额贷款", r"消费金融(监管|新规|公司)?",
    r"小额贷款(新规|利率|监管)?", r"个人征信|征信(新规|监管)?",
    r"金融监管总局|银保监会|国家金融监督管理总局", r"互联网金融(监管|整治|新规)?",
    r"互联网贷款(新规|监管)?", r"贷款(新规|年化利率|利率上限)", r"利率上限",
    r"现金贷", r"金融科技(监管|新规)?", r"人工智能(金融|信贷|风控)|AI(金融|贷款|信贷)",
    # Markets / external risk
    r"中概股", r"中国金龙指数", r"熔断", r"千股跌停", r"退市新规",
    r"制裁", r"关税", r"出口管制", r"实体清单",
]
# English labels for the most important patterns (used as a tag in the digest
# and as the fallback when macro_translate is off - zero AI).
MACRO_TAGS = [
    ("降息", "RATE CUT"), ("加息", "RATE HIKE"), ("降准", "RRR CUT"),
    ("LPR", "LPR"), ("特别国债", "T-BOND ISSUE"), ("万亿", "HUGE STIMULUS"),
    ("刺激", "STIMULUS"), ("国常会", "STATE COUNCIL"), ("政治局", "POLITBURO"),
    ("助贷", "ASSISTED-LOAN REG"), ("消费金融", "CONSUMER-FINANCE REG"),
    ("网络小贷|网络小额贷款|互联网小额贷款", "ONLINE-LENDING REG"),
    ("催收", "DEBT-COLLECTION"), ("现金贷", "CASH-LOAN REG"),
    ("金融监管总局|银保监会|国家金融监督管理总局", "FIN REGULATOR"),
    ("中概股|中国金龙", "CHINA ADR"), ("关税", "TARIFF"), ("制裁", "SANCTIONS"),
    ("出口管制|实体清单", "EXPORT CONTROL"),
]
# One compact Google News query for longer-form macro coverage (the wires
# carry the flash items; this catches the articles).
MACRO_GNEWS_QUERY = "中国 央行 降息 降准 LPR 助贷 消费金融 中概股 刺激政策"


def macro_tag(text):
    """English label for a macro item (first matching pattern), or 'MACRO'."""
    for pat, label in MACRO_TAGS:
        if re.search(pat, text, re.IGNORECASE):
            return label
    return "MACRO"


def exa_usage_today():
    """EXA usage tracker: daily count (resets at midnight ET) + monthly count.
    Both caps are enforced so the free ~1,000/month plan is never blown."""
    data = _read_json(EXA_USAGE_FILE, {})
    today = datetime.now(EASTERN).strftime("%Y-%m-%d")
    month = today[:7]
    if data.get("date") != today:
        data["date"] = today
        data["count"] = 0
    if data.get("month") != month:
        data["month"] = month
        data["month_count"] = 0
    return data


def fetch_exa(query, secrets, config, since_dt=None, limit=6, with_text=False,
              category="news"):
    """
    EXA AI neural search. Finds pages ABOUT the concept, not just containing
    the keywords - catches differently-worded big news the regex never sees,
    and related entities during discovery (category="company" finds company
    profile pages, e.g. 深圳市分期乐网络科技 for LX).

    Budget (configurable): daily cap exa_max_daily_searches (default 32),
    monthly cap exa_max_monthly_searches (default 980). Returns a list of
    items, or None on failure / when capped (delta not advanced).
    'with_text' is used only for discovery (small maxCharacters; costs a
    little more, but discovery runs monthly).
    """
    key = secrets.get("exa_api_key", "")
    if not key:
        return []
    daily_cap = _cfg_int(config, "exa_max_daily_searches", EXA_MAX_DAILY_SEARCHES)
    monthly_cap = _cfg_int(config, "exa_max_monthly_searches", EXA_MAX_MONTHLY_SEARCHES)
    usage = exa_usage_today()
    if usage.get("count", 0) >= daily_cap:
        print(f"  [warn] EXA daily cap ({daily_cap}) reached - skipping (delta not advanced).")
        return None
    if usage.get("month_count", 0) >= monthly_cap:
        print(f"  [warn] EXA monthly cap ({monthly_cap}) reached - skipping (delta not advanced).")
        return None
    try:
        payload = {"query": query, "numResults": limit, "type": "neural",
                   "category": category}
        if with_text:
            payload["contents"] = {"text": {"maxCharacters": 300}}
        if category == "news" and since_dt is not None:
            payload["startPublishedDate"] = (
                since_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        resp = requests.post(EXA_API,
                             headers={"x-api-key": key, "Content-Type": "application/json"},
                             json=payload, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        # Count the credit ONLY after a successful call (and never in
        # --no-write mode).
        if not NO_WRITE:
            usage["count"] += 1
            usage["month_count"] += 1
            _write_json(EXA_USAGE_FILE, usage)
        items = []
        for r in data.get("results", []) or []:
            title = (r.get("title") or "").strip()
            url = r.get("url") or ""
            if not title:
                continue
            items.append({
                "source": "Exa",
                "ticker": "",
                "id": url or title,
                "title": title,
                "url": url,
                "date": r.get("publishedDate") or "",
                "lang": "zh" if is_chinese(title) else "en",
                "snippet": (r.get("text") or "")[:300],
            })
        return items
    except Exception as exc:
        print(f"  [error] EXA search '{query[:50]}': {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Chinese fast-news wires (the real-time "tape" - where alpha breaks first)
# ---------------------------------------------------------------------------
# 东财 7x24 快讯 (Eastmoney) and 新浪 7x24 (Sina): global breaking-news feeds
# in Chinese. Fetched ONCE per run and filtered per ticker by Chinese
# name/subsidiary terms inside the loop (no per-ticker queries, so just one
# request per wire per run). cls.cn now requires signed requests and
# 格隆汇's API is unstable - not worth the fragility.
EASTMONEY_724_API = "https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
SINA_724_API = "https://zhibo.sina.com.cn/api/zhibo/feed"
WIRE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}


def fetch_eastmoney_724(limit=50):
    """
    Eastmoney 7x24 breaking-news wire (kuaixun). Returns a list of raw items
    (ticker/source filled by the caller), or None on failure (so no delta is
    advanced for that wire).
    """
    try:
        resp = requests.get(
            EASTMONEY_724_API,
            params={"client": "web", "biz": "web_724", "fastColumn": "102",
                    "sortEnd": "", "pageSize": str(limit),
                    "req_trace": str(int(time.time() * 1000))},
            headers={**WIRE_HEADERS, "Referer": "https://kuaixun.eastmoney.com/"},
            timeout=20,
        )
        resp.raise_for_status()
        items = []
        for it in (resp.json().get("data") or {}).get("fastNewsList") or []:
            title = strip_tags(it.get("title") or "") or strip_tags(it.get("summary") or "")
            if not title:
                continue
            items.append({
                "source": "",
                "ticker": "",
                "id": str(it.get("code") or title),
                "title": title,
                "url": it.get("url") or "",
                # showTime is Beijing wall-clock - normalize to ET.
                "date": _zh_date_to_et(it.get("showTime") or ""),
                "lang": "zh",
                "snippet": strip_tags(it.get("summary") or "")[:300],
            })
        return items
    except Exception as exc:
        print(f"  [error] Eastmoney 7x24 wire: {exc}", file=sys.stderr)
        return None


def fetch_sina_724(limit=100):
    """
    Sina 7x24 fast-news wire (财经7x24). Returns a list of raw items, or None
    on failure.
    """
    try:
        resp = requests.get(
            SINA_724_API,
            params={"page": "1", "page_size": str(limit), "zhibo_id": "152",
                    "tag_id": "0", "dire": "f", "dpc": "1"},
            headers={**WIRE_HEADERS, "Referer": "https://finance.sina.com.cn/7x24/"},
            timeout=20,
        )
        resp.raise_for_status()
        items = []
        for it in (((resp.json().get("result") or {}).get("data") or {})
                   .get("feed") or {}).get("list") or []:
            title = strip_tags(it.get("rich_text") or it.get("text") or "")
            if not title:
                continue
            items.append({
                "source": "",
                "ticker": "",
                "id": str(it.get("id") or title),
                "title": title[:200],
                "url": "",
                # create_time is Beijing wall-clock - normalize to ET.
                "date": _zh_date_to_et(it.get("create_time") or ""),
                "lang": "zh",
                "snippet": title[:300],
            })
        return items
    except Exception as exc:
        print(f"  [error] Sina 7x24 wire: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------
AI_RETRIES = 2
AI_RETRY_DELAY_SEC = 3


def _is_retryable(resp):
    """Return True if an HTTP response is a transient failure worth retrying."""
    if resp is None:
        return True
    return resp.status_code in (429,) or resp.status_code >= 500


def _chat(base, model, key, system, user, timeout=60):
    """One chat completion call with retry for transient failures."""
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    last_exc = None
    attempts = 0
    for attempt in range(AI_RETRIES + 1):
        resp = None
        attempts = attempt + 1
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if not _is_retryable(resp):
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            last_exc = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, requests.exceptions.HTTPError) and resp is not None \
                    and resp.status_code in (400, 401, 403, 404, 422):
                break
        if attempt < AI_RETRIES:
            print(f"  [warn] AI call attempt {attempt + 1} failed ({last_exc}); "
                  f"retrying in {AI_RETRY_DELAY_SEC}s...", file=sys.stderr)
            time.sleep(AI_RETRY_DELAY_SEC)
    print(f"  [error] AI call failed after {attempts} attempt(s): {last_exc}",
          file=sys.stderr)
    return None


def _parse_json_array(content):
    """Best-effort extraction of a JSON array from an AI response."""
    if not content:
        return None
    m = re.search(r"\[.*\]", content, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AI analysis: one batched call per ticker (translate + summarize + score)
# ---------------------------------------------------------------------------
def ai_analyze(items, config, secrets, meta_map, conn=None, run_start=None):
    """
    For each ticker, ONE batched AI call that:
      - translates titles to English,
      - summarizes,
      - categorizes (filing/earnings/press_release/analyst/regulatory/
        ownership/delisting/fraud/other),
      - scores importance 1-10,
      - tags sentiment,
      - suggests push true/false,
      - flags same-batch duplicates (duplicate_of),
      - flags known events (known_event: same story as something already seen
        in the last NEWS_RETENTION_DAYS - this REPLACES the old separate
        semantic-dedup AI call, halving AI calls per ticker per run).

    Chinese-language news about the company or its subsidiaries is explicitly
    weighed heavily (that is the edge the user wants). One call per ticker
    keeps token usage low. Falls back to sensible defaults on any error.
    Returns the same list of items, enriched in place.
    """
    if not items:
        return items
    base = config.get("ai_base_url") or DEFAULT_AI_BASE
    model = config.get("ai_model") or DEFAULT_AI_MODEL
    key = secrets.get("ai_api_key", "")
    if not key:
        print("  [warn] No AI API key set - storing items without AI analysis.")
        for it in items:
            it["title_en"] = it.get("title", "")
            it["summary"] = ""
            it["category"] = "other"
            it["importance"] = 5
            it["sentiment"] = "neutral"
            it["push"] = True
            it["reason"] = ""
            it["impact"] = ""
            it["is_dup"] = False
            it["is_known"] = False
        return items

    by_ticker = {}
    for it in items:
        by_ticker.setdefault(it.get("ticker", ""), []).append(it)

    enriched = []
    for ticker, ticker_items in by_ticker.items():
        meta = meta_map.get(ticker, {}) or {}
        # Normalize - the lookup file is AI-grown and could hold odd shapes
        # (a string where a list belongs); never let that crash the run.
        name_zh = str(meta.get("name_zh") or ticker)
        name_en = str(meta.get("name_en") or "")
        subs = ("、".join(str(x) for x in (meta.get("subsidiaries_zh") or [])
                          if str(x).strip())) or "its subsidiaries"
        site = str(meta.get("website") or "")
        lines = []
        for i, it in enumerate(ticker_items, 1):
            entry = {
                "number": i,
                "source": it.get("source", ""),
                "lang": it.get("lang", "en"),
                "title": it.get("title", ""),
                "url": it.get("url", ""),
            }
            if it.get("snippet"):
                entry["snippet"] = it["snippet"][:200]
            lines.append(entry)
        # Folded semantic dedup: show what has already been seen for this
        # ticker so the model can flag recycled/same-event stories
        # (known_event) - this replaces the old separate dedup AI call.
        # The batch's own titles are excluded: items are marked seen at fetch
        # time, so a rescued orphan (or any current item) must not be flagged
        # known_event against itself.
        history = (get_recent_seen_titles(
                       conn, ticker, before=run_start,
                       exclude={it.get("title", "") for it in ticker_items})
                   if conn else [])
        hist_lines = [f"- {t}" for t in history]
        if ticker == "MACRO":
            # China macro: framed around impact on the user's fintech names.
            prompt = (
                "You are a China macro analyst. Below are major China policy / "
                "market news items found today (already gated as macro-relevant).\n"
                "For EACH item return one JSON object with keys:\n"
                "  number, title_en (concise English translation), summary (one "
                "English sentence), category (one of monetary, fiscal, "
                "regulatory, fintech_reg, market, geopolitical, other), "
                "importance (integer 1-10: 8-10 = will move Chinese stocks or "
                "directly affects consumer-lending fintechs like QFIN/LX/LU; "
                "6-7 = significant market news; <6 = minor), sentiment "
                "(positive/negative/neutral), push (true if the user must know "
                "about it NOW), duplicate_of (number of an earlier item that is "
                "the same event, else null), known_event (true if same as an "
                "ALREADY SEEN headline), reason (one short English sentence why "
                "it matters), impact (one short English sentence: what this "
                "means for US-listed Chinese fintech/consumer-lending companies "
                "like 奇富科技 QFIN, 乐信 LX, 陆金所 LU - e.g. 'cheaper funding "
                "for 分期乐's lending' or 'tighter assisted-loan rules pressure "
                "origination volume'; empty string if not applicable).\n"
                "Return ONLY a JSON array of these objects, same order as the items.\n\n"
                "ALREADY SEEN (last few weeks):\n"
                + ("\n".join(hist_lines) if hist_lines else "(none)")
                + "\n\nITEMS:\n" + json.dumps(lines, ensure_ascii=False)
            )
        else:
            prompt = (
            f"You are an investor's news analyst for the stock {ticker} "
            f"({name_zh}{(' / ' + name_en) if name_en else ''}"
            f"{(' | official site: ' + site) if site else ''}).\n"
            f"Chinese-language news about this company or its subsidiaries "
            f"(e.g. {subs}) is especially valuable - weigh it heavily; it often "
            f"contains information English media misses.\n"
            "Below are NEW items found today. Each has a number, source, "
            "language, title, and a short snippet.\n"
            "For EACH item return one JSON object with keys:\n"
            "  number (the item's number), title_en (concise English "
            "translation; keep as-is if already English), summary (one English "
            "sentence), category (one of filing, earnings, press_release, "
            "analyst, regulatory, ownership, delisting, fraud, other), "
            "importance (integer 1-10: 8-10 = "
            "must-know for an investor such as earnings, regulatory action, "
            "M&A, major product or subsidiary news; 6-7 = significant; <6 = "
            "routine), sentiment (positive/negative/neutral), push (true only "
            "if the user should be alerted right now - material and not spam), "
            "duplicate_of (the 1-based number of an EARLIER item in this list "
            "that describes the SAME event, e.g. the same article syndicated "
            "under two URLs; null if this item is not a duplicate of an "
            "earlier one), "
            "known_event (true if this item describes the SAME underlying "
            "event as one of the ALREADY SEEN headlines below - possibly "
            "translated, possibly re-published under a new URL; false if it "
            "is genuinely new), "
            "reason (one short English sentence why it matters), "
            "impact (one short English sentence: what this means for the "
            "company's business or stock - e.g. 'this could pressure next "
            "quarter's loan volume'; empty string '' if routine or not "
            "applicable).\n"
            "Return ONLY a JSON array of these objects, same order as the items.\n\n"
            "ALREADY SEEN (last few weeks):\n"
            + ("\n".join(hist_lines) if hist_lines else "(none)")
            + "\n\nITEMS:\n" + json.dumps(lines, ensure_ascii=False)
        )
        print(f"  AI analysis {ticker}: {len(ticker_items)} item(s), one batched call...")
        content = _chat(base, model, key,
                        "You are a precise JSON-returning assistant.", prompt)
        by_num = {}
        parsed = _parse_json_array(content) if content else None
        if parsed:
            for obj in parsed:
                try:
                    by_num[int(obj.get("number"))] = obj
                except Exception:
                    continue
        # Intra-batch dedup: if the AI marked item N as a duplicate of an
        # EARLIER item in the same batch (same event, different URL/source),
        # we still translate/store it, but flag is_dup so it never gets
        # pushed. Chains resolve naturally: only items whose duplicate_of is
        # already a kept (non-dup) item are dropped.
        kept_nums = set()
        for i, it in enumerate(ticker_items, 1):
            obj = by_num.get(i, {})
            dup_of = obj.get("duplicate_of")
            try:
                dup_num = int(dup_of) if dup_of not in (None, "", "null") else None
            except Exception:
                dup_num = None
            it["is_dup"] = dup_num is not None and dup_num in kept_nums
            if not it["is_dup"]:
                kept_nums.add(i)
            it["title_en"] = str(obj.get("title_en") or it.get("title") or "").strip()
            it["summary"] = str(obj.get("summary") or "").strip()
            it["category"] = str(obj.get("category") or "other").strip()
            try:
                it["importance"] = max(1, min(10, int(obj.get("importance") or 5)))
            except Exception:
                it["importance"] = 5
            it["sentiment"] = str(obj.get("sentiment") or "neutral").strip()
            it["push"] = bool(obj.get("push", True))
            it["reason"] = str(obj.get("reason") or "").strip()
            it["impact"] = str(obj.get("impact") or "").strip()
            it["is_known"] = bool(obj.get("known_event", False))
            enriched.append(it)
        n_dups = sum(1 for it in ticker_items if it.get("is_dup"))
        n_known = sum(1 for it in ticker_items if it.get("is_known"))
        if n_dups or n_known:
            print(f"  AI dedup {ticker}: {n_dups} same-batch duplicate(s) + "
                  f"{n_known} known/recycled event(s) marked (stored, not pushed).")
    return enriched


# ---------------------------------------------------------------------------
# Seen-history for the folded AI dedup
# ---------------------------------------------------------------------------
SEMANTIC_DEDUP_HISTORY = 20


def get_recent_seen_titles(conn, ticker, limit=SEMANTIC_DEDUP_HISTORY, before=None,
                           exclude=None):
    """
    Return recent already-seen titles for a ticker (from the `seen` ledger),
    newest first. This history is folded into the per-ticker AI-analysis
    prompt so the model can flag recycled / same-event stories (known_event)
    without a separate AI call.

    'before' excludes the current run's items (marked 'seen' during the fetch
    loop) so two distinct new events in the same batch are never deduped
    against each other before either is pushed.

    'exclude' drops specific titles from the history - used by ai_analyze to
    remove the CURRENT BATCH's own titles, which are already in the ledger
    (items are marked seen at fetch time). Without this, a rescued orphan
    could be flagged known_event against ITS OWN earlier title and never
    pushed.
    """
    if before:
        cur = conn.execute(
            "SELECT title FROM seen WHERE ticker=? AND first_seen < ? "
            "ORDER BY first_seen DESC LIMIT ?",
            (ticker, before, limit),
        )
    else:
        cur = conn.execute(
            "SELECT title FROM seen WHERE ticker=? "
            "ORDER BY first_seen DESC LIMIT ?",
            (ticker, limit),
        )
    if exclude:
        ex = set(exclude)
        return [r[0] for r in cur.fetchall() if r[0] and r[0] not in ex]
    return [r[0] for r in cur.fetchall() if r[0]]


def is_macro(text, extra=None):
    """Free regex gate: does this item look like BIG China macro news?
    'extra' = user-supplied additional keywords from config (macro_keywords)."""
    if not text:
        return False
    pats = MACRO_KEYWORDS + [str(p) for p in (extra or []) if str(p).strip()]
    return any(re.search(p, text) for p in pats)


def collect_macro_items(conn, config, secrets, wire_cache, src_on,
                        initial_hours, run_start, now_utc_str):
    """
    The 'China macro' tier: huge policy/market news the user MUST know about
    (rate cuts, stimulus, assisted-loan regulation, ...). Gated FREE by
    regex (no AI cost to filter). Sources: the two 7x24 wires (already
    fetched once per run) + one Google News macro query. New items are
    stored under the pseudo-ticker "MACRO" and returned.
    """
    if not config.get("macro_enabled", True):
        return []
    extra = [str(k) for k in (config.get("macro_keywords") or []) if str(k).strip()]
    raw_items = []
    for wire_src in ("Eastmoney724", "Sina724"):
        raw = wire_cache.get(wire_src)
        if raw is None:
            print(f"  [warn] macro: {wire_src} wire unavailable (delta not advanced).")
            continue
        if raw:
            set_last_fetched(conn, "MACRO", wire_src, now_utc_str)
        for it in raw:
            if is_macro(f"{it.get('title', '')} {it.get('snippet', '')}", extra):
                item = dict(it)
                item["ticker"] = "MACRO"
                item["source"] = wire_src
                raw_items.append(item)
    if src_on("google_news_macro"):
        last = get_last_fetched(conn, "MACRO", "GoogleNewsMacro")
        since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                    if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
        res = fetch_rss(google_news_url(MACRO_GNEWS_QUERY, "zh"), "MACRO", since_dt,
                        source="GoogleNewsMacro", lang="zh")
        if res is None:
            print("  [warn] macro: Google News macro query failed - NOT advancing delta.")
        else:
            for it in res:
                if is_macro(f"{it.get('title', '')} {it.get('snippet', '')}", extra):
                    raw_items.append(it)
            set_last_fetched(conn, "MACRO", "GoogleNewsMacro", now_utc_str)

    # EXA neural macro search: finds BIG China news semantically - no regex
    # needed, catches differently-worded items (e.g. "助贷的生死时刻").
    if secrets.get("exa_api_key") and src_on("exa_macro"):
        last = get_last_fetched(conn, "MACRO", "ExaMacro")
        since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                    if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
        res = fetch_exa(MACRO_EXA_QUERY, secrets, config, since_dt=since_dt,
                        limit=8, category="news")
        if res is None:
            print("  [warn] macro: EXA macro search failed - NOT advancing delta.")
        else:
            for it in res:
                item = dict(it)
                item["ticker"] = "MACRO"
                item["source"] = "ExaMacro"
                raw_items.append(item)
            set_last_fetched(conn, "MACRO", "ExaMacro", now_utc_str)
    seen_keys = set()
    new_items = []
    for it in raw_items:
        key = (it["source"], it["id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        it["published_at"] = _normalize_pub(it.get("date", ""))
        it["first_seen"] = run_start
        if is_new(conn, "MACRO", it["source"], it["id"], it["title"]):
            new_items.append(it)
            mark_seen(conn, "MACRO", it["source"], it["id"], it["title"], it.get("url", ""))
            insert_news(conn, it)
    return new_items


def analyze_macro(macro_items, config, secrets, conn, run_start):
    """
    ONE tiny batched AI call for the macro items (translate + score + impact
    on the user's fintech names) - typically 0-3 items, so a few hundred
    tokens per run at most. When macro_translate is off (or no AI key), falls
    back to pushing the raw Chinese headline + an English tag: ZERO AI cost.
    """
    if not macro_items:
        return []
    key = secrets.get("ai_api_key", "")
    if config.get("macro_translate", True) and key:
        meta = {"MACRO": {"name_zh": "中国宏观", "name_en": "China Macro",
                          "subsidiaries_zh": [], "subsidiaries_other": []}}
        analyzed = ai_analyze(macro_items, config, secrets, meta,
                              conn=conn, run_start=run_start)
        for it in analyzed:
            update_news_ai(conn, it)
        ranked = sorted(
            [it for it in analyzed
             if not it.get("is_dup") and not it.get("is_known") and it.get("push", True)],
            key=lambda it: it.get("importance") or 0, reverse=True)
        return ranked[:_cfg_int(config, "macro_max_per_run", 3)]
    # Zero-AI fallback: raw Chinese + English tag, always pushed (capped).
    for it in macro_items:
        it["title_en"] = it.get("title", "")
        it["impact"] = ""
        it["importance"] = 8
        it["push"] = True
    return macro_items[:_cfg_int(config, "macro_max_per_run", 3)]


def format_macro(items):
    """The 'China Macro' digest section - big policy/market news, shown at the
    top of the digest, tagged with an English label."""
    if not items:
        return None
    lines = ["📢 CHINA MACRO — big policy/market news", ""]
    for it in items:
        title = it.get("title_en") or it.get("title", "")
        tag = macro_tag(f"{it.get('title', '')} {it.get('snippet', '')}")
        lines.append(f"• [{tag}] {title}")
        if it.get("impact"):
            lines.append(f"    → {it['impact']}")
        if it.get("url"):
            lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def build_snapshot(conn, hours=24, limit=10):
    """
    'Manual run' digest: the most important stored items from the last
    'hours' (regardless of push state), so a manual run ALWAYS delivers
    something - the current picture - instead of 'nothing new to push'
    (which is what a normal run says when the dedup ledger has seen it all).
    Returns the formatted message, or None if there is nothing notable.
    """
    cutoff = (datetime.now(EASTERN) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT ticker, COALESCE(NULLIF(title_en,''), title_raw), importance, "
        "category, impact, url FROM news WHERE first_seen >= ? "
        "AND importance IS NOT NULL ORDER BY importance DESC, first_seen DESC LIMIT ?",
        (cutoff, limit)).fetchall()
    if not rows:
        return None
    lines = ["📊 Manual snapshot — most important items (last 24h)", ""]
    for ticker, title, importance, category, impact, url in rows:
        header = f"• [{ticker}] {title}"
        if importance:
            header += f" ⭐{importance}"
        if category:
            header += f" ({category})"
        lines.append(header)
        if impact:
            lines.append(f"    → {impact}")
        if url:
            lines.append(f"    {url}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
# Telegram rejects messages over 4096 chars; Google News redirect URLs are
# ~600-800 chars each, so a full 10-item digest can hit ~9,000 chars. We
# split into chunks safely below the limit.
TELEGRAM_MAX_CHARS = 4000


def _split_message(message):
    """Split a long digest into <= TELEGRAM_MAX_CHARS chunks at blank lines
    (hard-splitting a single oversized block, e.g. a huge URL, on whitespace)."""
    if len(message) <= TELEGRAM_MAX_CHARS:
        return [message]
    chunks = []
    current = ""
    for block in message.split("\n\n"):
        piece = block if not current else "\n\n" + block
        if len(current) + len(piece) <= TELEGRAM_MAX_CHARS:
            current += piece
        else:
            if current:
                chunks.append(current)
            while len(block) > TELEGRAM_MAX_CHARS:
                cut = block.rfind(" ", 0, TELEGRAM_MAX_CHARS)
                cut = cut if cut > 0 else TELEGRAM_MAX_CHARS
                chunks.append(block[:cut])
                block = block[cut:].lstrip()
            current = block
    if current:
        chunks.append(current)
    return chunks


def send_telegram(token, chat_id, message):
    if not token or not chat_id:
        return False
    url = TELEGRAM_API.format(token=token)
    chunks = _split_message(message)
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=15)
            resp.raise_for_status()
        except Exception as exc:
            ok = False
            print(f"  [error] Telegram send failed: {exc}", file=sys.stderr)
    return ok


def format_digest(filtered, ticker_count, stored_count=0):
    if not filtered:
        return None
    lines = [f"📰 Portfolio News Digest ({ticker_count} ticker(s))", ""]
    for item in filtered:
        ticker = item.get("ticker", "")
        title = item.get("title_en") or item.get("title", "")
        url = item.get("url", "")
        reason = item.get("reason", "")
        category = item.get("category", "")
        importance = item.get("importance")
        header = f"• [{ticker}] {title}"
        if category:
            header += f"  ({category})"
        if importance:
            header += f" ⭐{importance}"
        lines.append(header)
        if reason:
            lines.append(f"    {reason}")
        if item.get("impact"):
            lines.append(f"    → {item['impact']}")
        if url:
            lines.append(f"    {url}")
        lines.append("")
    if stored_count > 0:
        lines.append(f"…and {stored_count} more item(s) stored — see panel Step 5.")
    return "\n".join(lines)


def select_push_items(enriched, config):
    """
    Decide which enriched items go to the Telegram digest.
      - drop same-batch duplicates (is_dup) - stored, not pushed
      - regulatory force-push: headlines matching penalty/regulatory keywords
        get boosted to >= 8 so they are never buried by generic scoring
      - importance floor (push_min_importance, default 4) + AI push veto
      - rank by importance (Chinese-language items tie-break higher)
      - per-ticker cap (push_max_per_ticker, default 3): one ticker can't eat
        every slot while another name has news, but the cap relaxes when it's
        the only name with candidates (solo big-news day still delivers).
    Returns the pushed list (subset of enriched, in push order).
    """
    push_mode = config.get("push_mode", "all")  # "all" | "score"
    floor = _cfg_int(config, "push_min_importance", 4)
    min_score = _cfg_int(config, "push_min_score", 7)
    max_digest = _cfg_int(config, "max_digest_items", 10)
    max_per_ticker = _cfg_int(config, "push_max_per_ticker", 3)

    unique = [it for it in enriched
              if not it.get("is_dup") and not it.get("is_known")]

    # Regulatory force-push: subsidiary penalties / regulatory action is the
    # core alpha - a code-level override so it is never buried by 1-10 scoring.
    reg_pattern = re.compile(
        r"(处罚|罚款|立案|约谈|调查|退市|监管|违规|delist|fraud|investigat|penalt|enforcement|regulat)",
        re.IGNORECASE)
    for it in unique:
        hay = f"{it.get('title', '')} {it.get('title_en', '')}"
        if reg_pattern.search(hay):
            it["importance"] = max(it.get("importance") or 0, 8)
            if it.get("category") in (None, "", "other"):
                it["category"] = "regulatory"

    # Importance floor + AI veto: push=false is honored as a veto, and nothing
    # below the floor is pushed (kills ⭐1-3 noise).
    if push_mode == "score":
        candidates = [it for it in unique
                      if it.get("push", True)
                      and (it.get("importance") or 0) >= max(floor, min_score)]
    else:
        candidates = [it for it in unique
                      if it.get("push", True)
                      and (it.get("importance") or 0) >= floor]

    candidates.sort(
        key=lambda it: ((it.get("importance") or 0), 1 if it.get("lang") == "zh" else 0),
        reverse=True)

    pushed = []
    counts = {}
    for it in candidates:
        if len(pushed) >= max_digest:
            break
        t = it.get("ticker", "")
        if counts.get(t, 0) >= max_per_ticker:
            # Only bite the cap if some OTHER ticker can still fill this slot.
            if any(it2.get("ticker") != t for it2 in candidates if it2 not in pushed):
                continue
        pushed.append(it)
        counts[t] = counts.get(t, 0) + 1
    return pushed


# ---------------------------------------------------------------------------
# Schedule guard
# ---------------------------------------------------------------------------
# Three runs a day, pinned to US market time. The third run (23:00 ET =
# 12:00 Beijing noon) catches the Chinese MORNING news burst - the alpha
# breaks 9:00-12:00 Beijing time, long before US media picks it up, and the
# 16:45 ET run misses it entirely.
SCHEDULE_RUN_TIMES = (dtime(9, 15), dtime(16, 45), dtime(23, 0))
SCHEDULE_TOLERANCE_MIN = 5


def _schedule_guard(now_et):
    """Exit without doing any work unless now is close to a scheduled run time."""
    now_t = now_et.time()
    for target in SCHEDULE_RUN_TIMES:
        delta = abs(
            (datetime.combine(now_et.date(), now_t)
             - datetime.combine(now_et.date(), target)).total_seconds()
        )
        if delta <= SCHEDULE_TOLERANCE_MIN * 60:
            return
    print(f"[{now_et.strftime('%Y-%m-%d %H:%M %Z')}] "
          f"Outside scheduled times (9:15 / 16:45 / 23:00 ET) - skipping.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Parse --no-write FIRST so every mode below (including --rediscover)
    # respects it: no DB/lookup/history writes, no Telegram alerts, no
    # Tavily credit usage.
    global NO_WRITE
    NO_WRITE = "--no-write" in sys.argv

    # ---- CLI modes first (never blocked by the schedule guard) ----
    if "--purge-junk" in sys.argv:
        # One-time cleanup of stored trash (never-pushed items scored at or
        # below the junk bar). Run this after a filter fix to clean history.
        threshold = 2
        for a in sys.argv:
            if a.startswith("--purge-junk"):
                parts = a.split("=", 1)
                if len(parts) == 2 and parts[1].strip().isdigit():
                    threshold = int(parts[1].strip())
        if NO_WRITE:
            print("  [purge] --no-write set - nothing deleted.")
            sys.exit(0)
        conn = _db()
        cur = conn.execute(
            "DELETE FROM news WHERE pushed=0 AND importance IS NOT NULL "
            "AND importance <= ?", (threshold,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        print(f"  [purge] deleted {deleted} stored junk item(s) "
              f"(pushed=0, importance <= {threshold}).")
        sys.exit(0)

    if "--rediscover" in sys.argv:
        ticker_arg = None
        for a in sys.argv:
            if a.startswith("--rediscover"):
                parts = a.split("=", 1)
                if len(parts) == 2 and parts[1].strip():
                    ticker_arg = parts[1].strip().upper()
        config = load_config()
        secrets = load_secrets()
        tickers = [t.strip().upper() for t in config.get("tickers", []) if t.strip()]
        if ticker_arg:
            tickers = [t for t in tickers if t == ticker_arg]
        print(f"  [rediscover] forcing company discovery for {len(tickers)} ticker(s)...")
        for t in tickers:
            ensure_company_meta(t, config, secrets, force=True)
        print("  [rediscover] done.")
        sys.exit(0)

    if "--dump-lookup" in sys.argv:
        print(json.dumps(load_lookup(), ensure_ascii=False, indent=1))
        sys.exit(0)

    if "--dump-usage" in sys.argv:
        print(json.dumps({"tavily": tavily_usage_today(), "exa": exa_usage_today()},
                         ensure_ascii=False))
        sys.exit(0)

    if "--dump-news" in sys.argv:
        ticker_arg = None
        for a in sys.argv:
            if a.startswith("--dump-news"):
                parts = a.split("=", 1)
                if len(parts) == 2 and parts[1].strip():
                    ticker_arg = parts[1].strip().upper()
        conn = _db()
        print(json.dumps(list_news(conn, ticker=ticker_arg), ensure_ascii=False))
        conn.close()
        sys.exit(0)

    dry_run = "--dry-run" in sys.argv
    snapshot = "--snapshot" in sys.argv  # manual run: always deliver (current picture)
    start_time = datetime.now(EASTERN)
    run_start = start_time.strftime("%Y-%m-%d %H:%M:%S")

    if "--force" not in sys.argv:
        _schedule_guard(start_time)

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

    if sec_validate_due():
        validate_sec_tickers(tickers)
        if not NO_WRITE:
            _write_json(SEC_VALIDATE_FILE, {"last": start_time.strftime("%Y-%m-%d")})
    else:
        print("  [sec] coverage check skipped (ran within the last 7 days).")

    secrets = load_secrets()
    token = secrets.get("telegram_bot_token", "")
    chat_id = secrets.get("telegram_chat_id", "")

    # Global retention settings (rolling cleanup).
    global NEWS_RETENTION_DAYS, SEEN_RETENTION_DAYS
    NEWS_RETENTION_DAYS = max(1, _cfg_int(config, "news_retention_days", NEWS_RETENTION_DAYS))
    SEEN_RETENTION_DAYS = max(NEWS_RETENTION_DAYS, _cfg_int(config, "seen_retention_days", SEEN_RETENTION_DAYS))

    conn = _db()
    pruned, vacuumed = prune_db(conn)
    if pruned:
        print(f"  [db] rolling cleanup removed {pruned} old row(s) "
              f"(news > {NEWS_RETENTION_DAYS}d, hashes > {SEEN_RETENTION_DAYS}d).")
    if vacuumed:
        print("  [db] VACUUM ran (DB file shrank).")

    all_new = []

    # Recovery pass FIRST (before any fetching): re-queue rows that a previous
    # run stored but never analyzed/pushed (crash between the fetch loop and
    # the AI/push stage - the mark-seen-before-push gap). They flow through
    # the normal AI analysis, floor, veto and caps below like fresh items.
    rescued = rescue_orphans(conn, config)
    if rescued:
        all_new.extend(rescued)
        record["rescued_items"] = len(rescued)
        print(f"  [recovery] re-queued {len(rescued)} unanalyzed item(s) from "
              f"a previous interrupted run.")

    record["tickers_checked"] = len(tickers)

    initial_hours = _cfg_int(config, "initial_lookback_hours", 24)
    # ET wall-clock string used for the per-source last_fetched deltas.
    now_utc_str = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")
    sources_cfg = config.get("sources", {})
    # The effective per-ticker profiles (company_lookup.json + config
    # overrides); also passed to the AI so it knows the Chinese names.
    effective_meta = {}

    def src_on(key):
        return sources_cfg.get(key, True)

    print(f"[{start_time.strftime('%Y-%m-%d %H:%M %Z')}] Checking {len(tickers)} "
          f"ticker(s) for new news (sources: SEC, GoogleNews, GoogleNewsZH, "
          f"GoogleNewsSite, Eastmoney, Eastmoney724, Sina724, Baidu, Tavily, RSS)...")

    # Global Chinese fast-news wires - ONE fetch per wire per run, shared by
    # all tickers (filtered per ticker by its Chinese terms inside the loop).
    wire_cache = {}
    if src_on("eastmoney_724"):
        wire_cache["Eastmoney724"] = fetch_eastmoney_724()
    if src_on("sina_724"):
        wire_cache["Sina724"] = fetch_sina_724()
    # Extra user keywords for the macro gate (config macro_keywords).
    macro_extra = [str(k) for k in (config.get("macro_keywords") or []) if str(k).strip()]

    for ticker in tickers:
        # The lookup step: pull the company profile (Chinese name, aliases,
        # subsidiaries like 分期乐/Fenqile) - discovering + populating the
        # lookup file if this ticker is new or stale. Isolated so one
        # ticker's lookup failure (network, AI, disk) never kills the run.
        try:
            meta = ensure_company_meta(ticker, config, secrets)
        except Exception as exc:
            print(f"  [error] company lookup for {ticker} failed: {exc}",
                  file=sys.stderr)
            continue
        effective_meta[ticker] = meta
        zh_terms = build_zh_terms(meta)
        en_terms = build_en_terms(meta)
        if zh_terms:
            print(f"  {ticker}: Chinese search terms -> {' / '.join(zh_terms)}")
        if en_terms:
            print(f"  {ticker}: EN subsidiary terms -> {' / '.join(en_terms)}")
        if not zh_terms and (src_on("google_news_zh") or src_on("eastmoney")
                             or src_on("baidu") or src_on("tavily")):
            print(f"  {ticker}: no Chinese name found - Chinese sources skipped. "
                  f"(add name_zh/aliases_zh/subsidiaries_zh to config_local.json)")

        # A helper that runs one "source fetch" for a ticker and folds the
        # results into all_new / the DB. Returns None if the fetch failed.
        def process_source(source, fetch_fn):
            last = get_last_fetched(conn, ticker, source)
            if last:
                since_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
            else:
                since_dt = datetime.now(EASTERN) - timedelta(hours=initial_hours)
            items = fetch_fn(since_dt)
            if items is None:
                print(f"  [warn] {ticker} {source} fetch failed - NOT advancing "
                      f"delta (will retry from {since_dt.strftime('%Y-%m-%d %H:%M')}).")
                return
            for item in items:
                item["ticker"] = ticker
                if is_new(conn, ticker, item["source"], item["id"], item["title"]):
                    item["published_at"] = _normalize_pub(item.get("date", ""))
                    item["first_seen"] = run_start
                    all_new.append(item)
                    mark_seen(conn, ticker, item["source"], item["id"],
                              item["title"], item.get("url", ""))
                    insert_news(conn, item)
            set_last_fetched(conn, ticker, source, now_utc_str)

        def process_wire(source, raw_items, terms):
            """Filter a global fast-news wire's items by this ticker's Chinese
            terms + delta, then fold matches into all_new / the DB (same
            semantics as process_source - None wire => delta not advanced)."""
            if not terms:
                return
            last = get_last_fetched(conn, ticker, source)
            since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                        if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
            found = 0
            for raw in raw_items:
                hay = f"{raw.get('title', '')} {raw.get('snippet', '')}"
                # Macro-matched items belong to the 📢 CHINA MACRO section -
                # skip them here so they are not pushed twice.
                if is_macro(hay, macro_extra):
                    continue
                if not any(_term_in_text(t, hay) for t in terms):
                    continue
                pub_dt = _parse_pub(raw.get("date", ""))
                if pub_dt and pub_dt < since_dt:
                    continue
                item = dict(raw)
                item["ticker"] = ticker
                item["source"] = source
                if is_new(conn, ticker, source, item["id"], item["title"]):
                    item["published_at"] = _normalize_pub(item.get("date", ""))
                    item["first_seen"] = run_start
                    all_new.append(item)
                    mark_seen(conn, ticker, source, item["id"], item["title"],
                              item.get("url", ""))
                    insert_news(conn, item)
                    found += 1
            set_last_fetched(conn, ticker, source, now_utc_str)
            if found:
                print(f"  {ticker}: {found} new {source} wire item(s).")

        # 1) SEC filings (English, ADR regulatory coverage).
        if src_on("sec"):
            process_source("SEC", lambda sd: fetch_sec_filings(ticker, sd, conn=conn))

        # 2) Chinese sources (the alpha) - require Chinese search terms.
        if zh_terms:
            # Google News, Chinese edition.
            if src_on("google_news_zh"):
                zh_query = " OR ".join(zh_terms)
                process_source(
                    "GoogleNewsZH",
                    lambda sd: fetch_rss(google_news_url(zh_query, "zh"), ticker, sd,
                                         source="GoogleNewsZH", lang="zh"))

            # Eastmoney search - one query per term (capped), precision-filtered.
            if src_on("eastmoney"):
                def _em(sd, terms=zh_terms[:EASTMONEY_MAX_QUERIES]):
                    out = []
                    failed = False
                    for term in terms:
                        res = fetch_eastmoney_search(term, sd, terms=zh_terms)
                        if res is None:
                            failed = True
                            continue
                        out.extend(res)
                    return None if failed else out
                process_source("Eastmoney", _em)

            # Baidu news (best-effort, one query using the main name).
            if src_on("baidu"):
                process_source("Baidu", lambda sd: fetch_baidu_news(zh_terms[0], sd, terms=zh_terms))

            # Tavily news search - the scarce resource. Skipped when the free
            # sources (GoogleNewsZH/Eastmoney/Baidu) already covered this
            # ticker this run, plus daily/monthly caps enforced inside.
            if src_on("tavily"):
                free_count = sum(1 for it in all_new if it.get("ticker") == ticker)
                min_free = _cfg_int(config, "tavily_min_free_items", TAVILY_MIN_FREE_ITEMS)
                if free_count >= min_free:
                    print(f"  {ticker}: free sources already found {free_count} "
                          f"item(s) this run - skipping Tavily (saving credits).")
                else:
                    tav_query = " OR ".join(zh_terms[:2] + en_terms[:2])
                    process_source(
                        "Tavily",
                        lambda sd: fetch_tavily(tav_query, secrets, config, sd,
                                                terms=zh_terms + en_terms))

        # 2.5) Google News restricted to the company's OWN websites (site:)
        #      - catches official announcements / press releases that no news
        #      outlet picks up. Free, no Tavily credits. Only runs when the
        #      lookup has discovered website domains.
        site_domains = build_site_domains(meta)
        if site_domains and src_on("google_news_site"):
            site_query = " OR ".join(f"site:{d}" for d in site_domains)
            print(f"  {ticker}: official-site search -> {site_query}")
            process_source(
                "GoogleNewsSite",
                lambda sd: fetch_rss(google_news_url(site_query, "zh"), ticker, sd,
                                     source="GoogleNewsSite", lang="zh"))

        # 3) Google News English - includes the company's EN names/brands so
        #    subsidiary news is found even when the ticker symbol isn't in it.
        if src_on("google_news_en"):
            en_query = " OR ".join([f"{ticker} stock"] + en_terms[:3])
            process_source(
                "GoogleNews",
                lambda sd: fetch_rss(google_news_url(en_query, "en"), ticker, sd,
                                     source="GoogleNews", lang="en"))

        # 3.5) Chinese fast-news wires (the real-time tape): one global fetch
        #      per wire per run, filtered here by this ticker's Chinese
        #      names/subsidiaries. This is where the alpha breaks first.
        for wire_src, wire_key in (("Eastmoney724", "eastmoney_724"),
                                   ("Sina724", "sina_724")):
            if not src_on(wire_key) or wire_src not in wire_cache:
                continue
            raw = wire_cache.get(wire_src)
            if raw is None:
                print(f"  [warn] {ticker} {wire_src} wire fetch failed - "
                      f"NOT advancing delta.")
                continue
            process_wire(wire_src, raw, zh_terms)

        # 3.6) EXA neural search - semantic recall: finds big/impact news
        #      about the company that keyword sources miss ("the Shenzhen-
        #      based insurer" instead of "Huize"). Skipped when the free
        #      sources already covered the ticker this run (budget).
        if src_on("exa") and secrets.get("exa_api_key"):
            free_count = sum(1 for it in all_new if it.get("ticker") == ticker)
            exa_min_free = _cfg_int(config, "exa_min_free_items", EXA_MIN_FREE_ITEMS)
            if free_count >= exa_min_free:
                print(f"  {ticker}: free sources already found {free_count} item(s) "
                      f"this run - skipping EXA (saving credits).")
            else:
                exa_query = (f"{' '.join(zh_terms[:2])} {' '.join(en_terms[:2])} "
                             f"重大 监管 政策 影响 风险")
                process_source(
                    "Exa",
                    lambda sd: fetch_exa(exa_query, secrets, config, sd,
                                         limit=5, category="news"))

        # 4) Company RSS feeds (configured per ticker) - source "RSS".
        feeds = config.get("rss_feeds", {}).get(ticker, [])
        if feeds:
            last = get_last_fetched(conn, ticker, "RSS")
            since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                        if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
            rss_ok = True
            for feed_url in feeds:
                feed_items = fetch_rss(feed_url, ticker, since_dt, source="RSS", lang="en")
                if feed_items is None:
                    rss_ok = False
                    print(f"  [warn] {ticker} RSS {feed_url} failed - NOT advancing delta.")
                    continue
                for item in feed_items:
                    item["ticker"] = ticker
                    if is_new(conn, ticker, item["source"], item["id"], item["title"]):
                        item["published_at"] = _normalize_pub(item.get("date", ""))
                        item["first_seen"] = run_start
                        all_new.append(item)
                        mark_seen(conn, ticker, item["source"], item["id"],
                                  item["title"], item.get("url", ""))
                        insert_news(conn, item)
            if rss_ok:
                set_last_fetched(conn, ticker, "RSS", now_utc_str)

    record["new_items"] = len(all_new)
    print(f"  {len(all_new)} new item(s) found (all stored in the news DB).")

    # ---- China macro watch: the "I HAVE TO KNOW" tier ----
    # Huge policy/market news (rate cuts, stimulus, assisted-loan regulation).
    # Gated FREE by regex; only matched items reach the AI (one tiny batched
    # call). Always pushed in their own digest section, never floor-capped.
    macro_items = collect_macro_items(conn, config, secrets, wire_cache, src_on,
                                      initial_hours, run_start, now_utc_str)
    macro_pushed = analyze_macro(macro_items, config, secrets, conn, run_start)
    for it in macro_items:
        mark_pushed(conn, it, it in macro_pushed)
    if macro_items:
        print(f"  {len(macro_items)} China macro item(s) -> {len(macro_pushed)} "
              f"pushed (the 'must know' tier).")
    macro_digest = format_macro(macro_pushed)

    # Keep only the freshest items for the AI pass when there's a flood.
    # Real publish dates first; undated items (rare Tavily/Baidu hits) fall
    # back to first_seen so they are NOT treated as the oldest and trimmed.
    def _sort_key(it):
        for key in ("published_at", "date"):
            dt = _parse_pub(it.get(key) or "")
            if dt:
                return dt
        fs = it.get("first_seen")
        if fs:
            try:
                return datetime.strptime(fs, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
            except Exception:
                pass
        # Aware, like every other branch - a naive datetime.min here would
        # raise TypeError when sorting mixed-aware datetimes.
        return datetime.min.replace(tzinfo=EASTERN)
    all_new.sort(key=_sort_key, reverse=True)

    max_to_filter = _cfg_int(config, "max_items_per_run", 40)
    if len(all_new) > max_to_filter:
        print(f"  Trimming to the {max_to_filter} most recent for AI analysis.")
        all_new = all_new[:max_to_filter]

    if not all_new and not macro_pushed:
        print("  No new items - nothing to do.")
        if macro_digest:
            if dry_run:
                print("\n" + "=" * 60)
                print("DRY RUN - CHINA MACRO:")
                print("=" * 60)
                print(macro_digest)
                print("=" * 60)
            elif token and chat_id:
                if send_telegram(token, chat_id, macro_digest):
                    print(f"  Macro digest sent ({len(macro_pushed)} item(s)).")
                else:
                    record["alerts_failed"] = record.get("alerts_failed", []) + ["macro"]
        elif snapshot and token and chat_id and not dry_run:
            # Manual run with nothing new: deliver the current picture instead.
            snap_msg = build_snapshot(conn)
            if snap_msg and send_telegram(token, chat_id, snap_msg):
                print("  Manual snapshot sent (nothing new since last run - current picture).")
            elif snap_msg:
                record["alerts_failed"] = record.get("alerts_failed", []) + ["snapshot"]
            else:
                print("  Manual snapshot: nothing notable in the last 24h - nothing sent.")
        elif snapshot and dry_run:
            snap_msg = build_snapshot(conn)
            if snap_msg:
                print("\n" + "=" * 60)
                print("DRY RUN - manual snapshot would be sent:")
                print("=" * 60)
                print(snap_msg)
                print("=" * 60)
            else:
                print("  (snapshot: nothing notable in the last 24h)")
        conn.close()
        record["duration_sec"] = round((datetime.now(EASTERN) - start_time).total_seconds(), 2)
        append_run_record(record)
        return

    pushed = []
    if all_new:
        # ---- AI analysis: translate + summarize + score + dedup in ONE call
        # per ticker (the seen-ledger history is folded into the prompt, so no
        # separate semantic-dedup AI call is needed - half the AI calls). ----
        enriched = ai_analyze(all_new, config, secrets, effective_meta,
                              conn=conn, run_start=run_start)
        # Push selection FIRST: the regulatory force-push mutates importance /
        # category in place, so it must run before update_news_ai persists the
        # boosted values (otherwise the DB would show the pre-boost score).
        pushed = select_push_items(enriched, config)
        for it in enriched:
            update_news_ai(conn, it)
        for it in enriched:
            mark_pushed(conn, it, it in pushed)
        record["sent_items"] = len(pushed)
        floor = _cfg_int(config, "push_min_importance", 4)
        max_digest = _cfg_int(config, "max_digest_items", 10)
        max_per_ticker = _cfg_int(config, "push_max_per_ticker", 3)
        print(f"  Pushing {len(pushed)} item(s) to Telegram "
              f"(importance >= {floor}, cap {max_digest}, max {max_per_ticker}/ticker). "
              f"{len(enriched) - len(pushed)} item(s) stored for browsing.")

    digest = format_digest(pushed, len(tickers),
                           stored_count=len(all_new) - len(pushed)) if pushed else None
    if digest or macro_digest:
        if dry_run:
            if macro_digest:
                print("\n" + "=" * 60)
                print("DRY RUN - CHINA MACRO:")
                print("=" * 60)
                print(macro_digest)
                print("=" * 60)
            if digest:
                print("\n" + "=" * 60)
                print("DRY RUN - digest would be sent to Telegram:")
                print("=" * 60)
                print(digest)
                print("=" * 60)
        elif token and chat_id:
            sent_any = False
            if macro_digest and send_telegram(token, chat_id, macro_digest):
                sent_any = True
            elif macro_digest:
                record["alerts_failed"] = record.get("alerts_failed", []) + ["macro"]
            if digest and send_telegram(token, chat_id, digest):
                sent_any = True
            elif digest:
                record["alerts_failed"] = record.get("alerts_failed", []) + ["digest"]
            if sent_any:
                print(f"  Digest sent (macro {len(macro_pushed)} + "
                      f"regular {len(pushed)} item(s)).")
        else:
            print("  Digest ready but Telegram not configured.")
    elif snapshot and token and chat_id and not dry_run:
        # Manual run with nothing push-worthy: deliver the current picture.
        snap_msg = build_snapshot(conn)
        if snap_msg and send_telegram(token, chat_id, snap_msg):
            print("  Manual snapshot sent (nothing push-worthy - current picture).")
        elif snap_msg:
            record["alerts_failed"] = record.get("alerts_failed", []) + ["snapshot"]
        else:
            print("  Manual snapshot: nothing notable in the last 24h - nothing sent.")
    elif snapshot and dry_run:
        snap_msg = build_snapshot(conn)
        if snap_msg:
            print("\n" + "=" * 60)
            print("DRY RUN - manual snapshot would be sent:")
            print("=" * 60)
            print(snap_msg)
            print("=" * 60)
        else:
            print("  (snapshot: nothing notable in the last 24h)")
    else:
        print("  No items to push - nothing sent.")

    conn.close()
    record["duration_sec"] = round((datetime.now(EASTERN) - start_time).total_seconds(), 2)
    append_run_record(record)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never die silently: log the traceback AND record the failure in the
        # run history so the panel shows what happened.
        import traceback
        traceback.print_exc()
        try:
            from datetime import datetime as _dt
            append_run_record({
                "timestamp": _dt.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "status": "error",
                "error": f"unhandled: {type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
        sys.exit(1)
