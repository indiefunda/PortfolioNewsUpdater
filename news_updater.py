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

import base64
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
# Exclusive-run marker (flock); see acquire_run_lock().
RUN_LOCK_FILE = "news_updater.lock"

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
# Per-ticker EXA neural search. OFF by default: measured over 616 stored Exa
# rows, 610 never mentioned the company (the neural query's sector tail returned
# the whole industry), and 5-8 of every 8 results were discarded as undated, so a
# paid credit per ticker per run bought almost nothing. The EXA allowance is far
# better spent on the macro tier. Turn on with config "exa_per_ticker": true.
EXA_PER_TICKER_DEFAULT = False
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

# Hard freshness backstop: any item whose PUBLISH time is older than this
# many hours is dropped at ingestion, regardless of what the source returned
# (search APIs - EXA neural search especially - sometimes ignore their own
# date filters and serve months-old articles as "brand-new"). 96h covers even
# a long weekend between runs; 0 disables. Configurable: max_news_age_hours.
MAX_NEWS_AGE_HOURS = 96

# ---------------------------------------------------------------------------
# News-quality gates (the "is this actually about MY company, and is it NEW?"
# layer). These exist because relevance and repetition - not the scoring -
# are what makes a digest feel like noise.
# ---------------------------------------------------------------------------
# Any item whose publish time is older than this is stored but NEVER pushed,
# no matter what the AI scored it. This is the code-level guard against
# recycled coverage of an event the user already has (e.g. earnings results
# re-reported days later by another outlet). Regulatory force-pushes are
# exempt (a penalty is news whenever it surfaces). Configurable:
# push_max_age_hours; 0 disables.
PUSH_MAX_AGE_HOURS = 72
# A stored item that has never been analysed (trimmed out of a busy run) is
# re-queued by rescue_orphans. Cap that recovery batch generously so the tail
# always drains instead of starving behind the newest items.
ORPHAN_RESCUE_LIMIT = 120
# Consecutive failures before a rescued orphan is left alone (never loops).
ORPHAN_MAX_RESCUES = 3
# Optional "sector watch" tier: semantically-relevant SECTOR news that does
# not mention the company itself (e.g. Chinese insurance-sector regulation for
# HUIZ/YB). Off by default - the user's complaint was exactly this class of
# item - but when enabled it is labelled and capped separately instead of
# being mixed into the per-ticker digest. Configurable: sector_watch.
SECTOR_WATCH_DEFAULT = False
SECTOR_WATCH_MAX_PER_RUN = 2
# Global-markets tier: genuinely systemic US/global items (Fed decisions, CPI,
# payrolls) that move Chinese ADRs. Kept SEPARATE from 📢 CHINA MACRO - which
# stays reserved for China policy - and capped hard. Off by default because a
# daily "Nasdaq Golden Dragon index closed down X%" is not actionable news.
# Configurable: global_markets.
GLOBAL_MARKETS_DEFAULT = False
GLOBAL_MARKETS_MAX_PER_RUN = 2
# How many macro/global candidates may be STORED per run. The wires produce
# dozens of macro-ish items per run but only the top few are ever pushed, so
# storing them all just filled the browsable DB with rows that never got an AI
# score (they showed as "—" in the panel). Everything not stored is simply not
# seen again, so the DB stays a record of notable macro news.
MACRO_STORE_MAX_PER_RUN = 12
# Near-duplicate detection: two items for the same ticker whose titles share
# this much of their token sets are the SAME story from different outlets.
STORY_JACCARD_MIN = 0.6
STORY_CONTAINMENT_MIN = 0.82
# How many of a ticker's already-pushed titles the mechanical repeat gate
# compares a new candidate against.
PUSH_REPEAT_HISTORY = 160
PUSH_REPEAT_JACCARD = 0.62
# Event-level guard window: a corporate event (an earnings release for a given
# fiscal period, an EGM, a dividend) is pushed ONCE, and every later article
# about that same event is suppressed for this many days. 0 disables the event
# guard (the per-story repeat gate still applies). Configurable:
# event_repeat_window_days.
EVENT_REPEAT_WINDOW_DAYS = 7
# Local-time anchor for a date-only publish date (see _is_date_only).
NOON_HOUR = 12


def _is_date_only(s):
    """True for 'YYYY-MM-DD' / 'YYYY/MM/DD' (no time component) strings.

    Search APIs (EXA, Tavily) often return a date with no time. Parsing that
    as midnight invents an exact time that the source never gave - which then
    drives the freshness gates and the 📅 date in the digest. We anchor
    date-only values at noon of that local day instead: still honest to the
    day, and never 'yesterday' because of a timezone shift.
    """
    s = str(s or "").strip()
    return bool(re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", s))


def _parse_pub(s, naive_tz=EASTERN):
    """
    Parse a publish-date string to an aware datetime (Eastern), or None.
    Handles the formats the sources actually emit:
      - ISO 8601 with or without fractional seconds, 'Z' or offsets
        (Tavily: 2026-08-15T10:22:33.123Z)
      - 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD' / 'YYYY/MM/DD ...'
      - RFC 822 / RFC 1123 (Google News RSS: 'Sat, 15 Aug 2026 05:00:00 GMT')
    Naive dates are assumed to be 'naive_tz' (Eastern by default; Chinese
    sources pass Asia/Shanghai - see _zh_date_to_et). A DATE-ONLY value is
    anchored at 12:00 local rather than midnight (see _is_date_only).
    """
    if not s:
        return None
    s = str(s).strip()
    date_only = _is_date_only(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz)
            if date_only:
                dt = dt.replace(hour=NOON_HOUR, minute=0, second=0, microsecond=0)
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
                if date_only:
                    dt = dt.replace(hour=NOON_HOUR, minute=0, second=0)
            return dt.astimezone(EASTERN)
        except Exception:
            continue
    return None

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
    # Migration: how many times an unanalyzed row was re-queued by
    # rescue_orphans (stops a permanently unanalyzable row from looping).
    try:
        conn.execute("ALTER TABLE news ADD COLUMN rescues INTEGER DEFAULT 0")
    except Exception:
        pass
    # The repeat gate's memory: one row per story actually PUSHED per ticker,
    # so a re-report of the same event (new URL, different outlet, days later)
    # cannot be pushed twice even when the AI does not spot it. This is the
    # mechanical backstop for "I already read this".
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pushed_stories (
            ticker TEXT NOT NULL,
            story_key TEXT NOT NULL,
            title TEXT,
            pushed_at TEXT NOT NULL,
            event_key TEXT,
            PRIMARY KEY (ticker, story_key)
        )"""
    )
    # Migration: the event-level key (ticker + category + fiscal period), so
    # one earnings release can only ever be pushed once.
    try:
        conn.execute("ALTER TABLE pushed_stories ADD COLUMN event_key TEXT")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pushed_event "
                 "ON pushed_stories (ticker, event_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_first_seen ON news (first_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_ticker ON news (ticker)")
    return conn


# Event-level guard: a corporate EVENT (an earnings release for a given fiscal
# period, a shareholder vote, a convertible-note extension) is news ONCE. Every
# later article about it - a different outlet, a translated headline, an
# analyst write-up - is coverage, not news, and is what made the digest feel
# like it was repeating itself for days.
#
# The period is taken from the title when stated (explicitly or as a period-end
# date), never guessed from the clock, so last quarter's report cannot block
# this quarter's.
_CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4"}
_ORD_Q = {"first": "1", "second": "2", "third": "3", "fourth": "4"}


def _norm_cn_num(s):
    """'二' -> '2' so Chinese and English phrasings of a quarter agree."""
    return _CN_NUM.get(str(s), str(s))


_EVENT_PERIOD_PATTERNS = [
    # Q2 2026 / 2Q26 / 2026 Q2 / 2026年第二季度 / 第二季度
    (r"\b(?:Q|q)([1-4])\s*(?:FY)?\s*(20\d{2})\b",
     lambda m: f"{m.group(2)}Q{m.group(1)}"),
    (r"\b(20\d{2})\s*(?:FY)?\s*(?:Q|q)([1-4])\b",
     lambda m: f"{m.group(1)}Q{m.group(2)}"),
    (r"(20\d{2})\s*年\s*第\s*([一二三四1-4])\s*季度",
     lambda m: f"{m.group(1)}Q{_norm_cn_num(m.group(2))}"),
    (r"第\s*([一二三四1-4])\s*季度",
     lambda m: f"Q{_norm_cn_num(m.group(1))}"),
    # English ordinals: 'Second Quarter 2026' / 'Second-Quarter 2026'
    (r"\b(second|third|fourth|first)[\s-]*quarter\b.{0,24}?(20\d{2})",
     lambda m: f"{m.group(2)}Q{_ORD_Q[m.group(1).lower()]}"),
    (r"\b(20\d{2})\b.{0,24}?\b(second|third|fourth|first)[\s-]*quarter\b",
     lambda m: f"{m.group(1)}Q{_ORD_Q[m.group(2).lower()]}"),
    # H1 2026 / 上半年 / 2026年上半年
    (r"\bH([12])\s*(20\d{2})\b", lambda m: f"{m.group(2)}H{m.group(1)}"),
    (r"(20\d{2})\s*年\s*上半年", lambda m: f"{m.group(1)}H1"),
    (r"上半年", lambda m: "H1"),
    # FY2026 / 全年 / 年度
    (r"\bFY\s*(20\d{2})\b", lambda m: f"{m.group(1)}FY"),
    (r"(20\d{2})\s*年\s*全年度?", lambda m: f"{m.group(1)}FY"),
    (r"全年度|全年业绩|年度业绩", lambda m: "FY"),
    # A period END date is an explicit, unambiguous fiscal marker
    # (2026-06-30 == Q2 2026 for a calendar-year filer). Both the numeric and
    # the month-name form are handled - "for the quarter ended June 30, 2026"
    # is the single most common earnings-release phrasing there is.
    (r"20(\d{2})[-/年]\s*0?6[-/月]\s*30", lambda m: f"20{m.group(1)}Q2"),
    (r"20(\d{2})[-/年]\s*0?3[-/月]\s*31", lambda m: f"20{m.group(1)}Q1"),
    (r"20(\d{2})[-/年]\s*0?9[-/月]\s*30", lambda m: f"20{m.group(1)}Q3"),
    (r"20(\d{2})[-/年]\s*12[-/月]\s*31", lambda m: f"20{m.group(1)}Q4"),
    (r"\b(june|jun)\s*30,?\s*(20\d{2})",
     lambda m: f"{m.group(2)}Q2"),
    (r"\b(march|mar)\s*31,?\s*(20\d{2})",
     lambda m: f"{m.group(2)}Q1"),
    (r"\b(september|sept|sep)\s*30,?\s*(20\d{2})",
     lambda m: f"{m.group(2)}Q3"),
    (r"\b(december|dec)\s*31,?\s*(20\d{2})",
     lambda m: f"{m.group(2)}Q4"),
]
_ORD_Q = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
# ORDER MATTERS: the first matching pattern names the event, so a specific
# event must come before a general one ("...EGM to Extend Convertible Notes"
# is a convertible-note event, not a generic EGM).
_EVENT_NAME_PATTERNS = [
    (r"earnings call|earnings results|financial results|quarter(?:ly)? ended|"
     r"results for the (?:quarter|year|period)|full[- ]year results|"
     r"季度业绩|业绩电话会议|业绩发布|财报|年度业绩|业绩公告|业绩说明会|"
     r"中期业绩|业绩报告", "earnings"),
    (r"converts?ible (?:notes?|bond)|可转换(?:债券|票据)", "connote"),
    (r"share (?:issuance|placement)|配股|增发|供股", "shareissue"),
    (r"dividend|分红|派息", "dividend"),
    (r"share repurchase|buyback|回购", "buyback"),
    (r"extraordinary general meeting|EGM|临时股东大会|股东特别大会", "egm"),
]


def _event_period(title):
    """('2026Q2' | 'H1' | 'FY' | None) - the fiscal period stated in a title."""
    # Chinese sources mix full-width （）／， with ASCII (a Chinese headline can
    # carry "（Lufax Holding Ltd. 2026 Q2 Results）"). Fold the full-width forms
    # to ASCII first, or the period patterns silently miss.
    title = (title or "").translate(str.maketrans("（）［］，／：", "()[],/:"))
    for pat, fmt in _EVENT_PERIOD_PATTERNS:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            return fmt(m)
    return None


def event_key(item):
    """
    A stable key for the corporate EVENT behind an item, or None.

    Combines the category (earnings / egm / dividend ...) with the fiscal
    period when the title states one. None means "this item is not a recurring
    corporate event", so the event guard steps aside and only the per-story
    repeat gate applies.
    """
    hay = " ".join(str(item.get(k) or "") for k in ("title", "title_en"))
    for pat, name in _EVENT_NAME_PATTERNS:
        if re.search(pat, hay, re.IGNORECASE):
            period = _event_period(hay)
            return f"{name}:{period}" if period else name
    return None


def canonical_url(item):
    """
    The real article URL behind a Google News RSS link, when recoverable.

    Google News wraps links as news.google.com/rss/articles/CBMi<opaque>. The
    LEGACY format embedded the target URL in that base64 protobuf, and this
    decodes it. The CURRENT format is an opaque id (verified: 0 of 40 sampled
    payloads contained an http(s) URL), whose target is only obtainable by
    calling news.google.com - so there we fall back to ''.
    """
    url = str(item.get("url") or "").strip()
    if "news.google.com" not in url or "/articles/" not in url:
        return ""
    try:
        seg = url.split("/articles/", 1)[1].split("?", 1)[0].split("/", 1)[0]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg)
    except Exception:
        return ""
    found = re.findall(rb"https?://[^\s\"'<>\x00-\x1f]{6,600}", raw)
    if not found:
        return ""
    return max(found, key=len).decode("utf-8", "replace").rstrip("\\\x01\x02\x03 ")


def story_key(item):
    """
    Stable key for the repeat gate.

    Preference order:
      1. the canonical URL when the real target is recoverable (identical page);
      2. domain + normalised title when the feed told us the origin outlet -
         this is what lets a Google News link and the outlet's own article
         resolve to the same key even though their URLs share nothing;
      3. the raw URL;
      4. a hash of the normalised title, so a re-published story under a new URL
         still matches by headline.
    """
    canon = canonical_url(item)
    if canon:
        return "c:" + hashlib.sha256(canon.lower().encode("utf-8")).hexdigest()[:32]
    title = _clean_title(item.get("title", ""))
    domain = _domain_of(item.get("origin") or "")
    if domain and title:
        return "d:" + hashlib.sha256(
            (domain + "|" + title).encode("utf-8")).hexdigest()[:32]
    url = str(item.get("url") or "").strip()
    if url:
        return "u:" + hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:32]
    return "t:" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:32]


def _domain_of(url):
    """Lower-cased host of a URL ('' when there is none)."""
    m = re.match(r"https?://([^/]+)", str(url or "").strip(), re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).lower().removeprefix("www.")


def record_pushed_stories(conn, items, now=None):
    """Remember the stories just pushed (the repeat AND event gates' memory)."""
    if NO_WRITE or conn is None or not items:
        return
    stamp = (now or datetime.now(EASTERN)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        for it in items:
            # Store the ORIGINAL headline: the repeat gate compares tokens
            # against it next run, and the AI's English translation of a
            # Chinese headline carries far fewer comparable tokens.
            conn.execute(
                "INSERT OR REPLACE INTO pushed_stories "
                "(ticker, story_key, title, pushed_at, event_key) VALUES (?,?,?,?,?)",
                (it.get("ticker", ""), story_key(it),
                 (it.get("title") or it.get("title_en") or "")[:300], stamp,
                 event_key(it)))
        conn.commit()
    except Exception as exc:
        print(f"  [warn] could not record pushed stories: {exc}", file=sys.stderr)


def _story_already_pushed(conn, item, now, event_window_days=None):
    """
    Has this story - or the corporate EVENT behind it - already gone out for
    this ticker?

    Three checks, cheapest first:
      1. the exact story key (same URL, or same normalised headline);
      2. the EVENT key: one earnings release / EGM / dividend for a given
         fiscal period is pushed ONCE, however many outlets later write it up
         under their own URL and wording;
      3. token overlap against recent pushed headlines (near-identical
         re-writes in the same language).
    """
    ticker = item.get("ticker", "")
    try:
        row = conn.execute("SELECT 1 FROM pushed_stories WHERE ticker=? AND story_key=?",
                           (ticker, story_key(item))).fetchone()
        if row:
            return True
        # (2) Event-level guard.
        if event_window_days is None:
            event_window_days = EVENT_REPEAT_WINDOW_DAYS
        ev = event_key(item)
        if ev and event_window_days > 0:
            ev_cutoff = (now - timedelta(days=event_window_days)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            row = conn.execute(
                "SELECT 1 FROM pushed_stories WHERE ticker=? AND event_key=? "
                "AND pushed_at >= ?", (ticker, ev, ev_cutoff)).fetchone()
            if row:
                item["_event_repeat"] = ev
                return True
        # (3) Headline-similarity fallback.
        cutoff = (now - timedelta(days=NEWS_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        rows = conn.execute(
            "SELECT title FROM pushed_stories WHERE ticker=? AND pushed_at >= ? "
            "ORDER BY pushed_at DESC LIMIT ?",
            (ticker, cutoff, PUSH_REPEAT_HISTORY)).fetchall()
    except Exception:
        return False
    tok_new = item.get("_story_tokens") or story_tokens(item.get("title", ""))
    if not tok_new:
        return False
    for (title,) in rows:
        known = story_tokens(title or "")
        if known and _jaccard(tok_new, known) >= PUSH_REPEAT_JACCARD:
            return True
    return False


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
    """Return stored news (last ~3 weeks) as a list of dicts for browsing.

    Includes `item_hash` so the panel can delete an individual row (the hash is
    what identifies a row together with its ticker and source).
    """
    cols = ["ticker", "source", "lang", "title_en", "title_raw", "summary",
            "category", "importance", "sentiment", "pushed", "url",
            "published_at", "first_seen", "reason", "impact", "item_hash"]
    if ticker:
        rows = conn.execute(
            "SELECT ticker, source, lang, title_en, title_raw, summary, category, "
            "importance, sentiment, pushed, url, published_at, first_seen, reason, "
            "impact, item_hash FROM news WHERE ticker=? "
            "ORDER BY first_seen DESC, id DESC LIMIT ?",
            (ticker, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ticker, source, lang, title_en, title_raw, summary, category, "
            "importance, sentiment, pushed, url, published_at, first_seen, reason, "
            "impact, item_hash FROM news ORDER BY first_seen DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def delete_news_item(conn, ticker, source="", item_hash="", title="", url="",
                     keep_ledger=False):
    """
    Delete ONE stored news row, plus (by default) the repeat-gate memory it
    created.

    Removing the ledger entry matters: if the row was pushed, leaving it in
    `pushed_stories` would keep suppressing that story. Deleting the user's
    copy is an explicit "I don't want this", so it also clears the memory that
    would block the same story from being pushed again. Pass keep_ledger=True
    to remove the row from the browser view while keeping the dedup memory.

    Identified by (ticker, source, item_hash) when available (what the panel
    sends), else by an exact title/URL match so a hand-typed call still works.
    Returns a dict describing what was removed.
    """
    result = {"deleted": 0, "ledger_removed": 0}
    where, params = "", []
    if item_hash:
        where = "ticker=? AND source=? AND item_hash=?"
        params = [ticker, source, item_hash]
    elif url:
        where, params = "ticker=? AND url=?", [ticker, url]
    elif title:
        where, params = "ticker=? AND (title_raw=? OR title_en=?)", [ticker, title, title]
    else:
        return result
    rows = conn.execute(f"SELECT url, title_raw, title_en FROM news WHERE {where}",
                        params).fetchall()
    cur = conn.execute(f"DELETE FROM news WHERE {where}", params)
    result["deleted"] = cur.rowcount
    if not keep_ledger:
        # Clear the repeat-gate / EVENT-gate memory for the deleted stories.
        #
        # Clearing only the per-story key is not enough: the event guard matches
        # on `event_key` ("earnings:2026Q2"), so deleting one article about an
        # earnings release left the event suppressed and every other article
        # about it stayed blocked - the user deletes the item and the story
        # simply never comes back. Deleting the row is an explicit "I don't want
        # this", so the event memory for that exact event goes with it, letting
        # a genuine re-report reach them again.
        #
        # One key per identity the row could have been recorded under (URL,
        # normalised headline, event), plus an event-key fallback on the stored
        # title so seeded/legacy rows are covered too.
        try:
            for (u, tr, te) in rows:
                keys = set()
                if u:
                    keys.add(story_key({"ticker": ticker, "url": u, "title": ""}))
                if tr:
                    keys.add(story_key({"ticker": ticker, "url": "", "title": tr}))
                if te:
                    keys.add(story_key({"ticker": ticker, "url": "", "title": te}))
                for sk in keys:
                    c2 = conn.execute(
                        "DELETE FROM pushed_stories WHERE ticker=? AND story_key=?",
                        (ticker, sk))
                    result["ledger_removed"] += c2.rowcount
                # Event-level memory (any row for this event, however stored).
                ev = event_key({"title": tr or te or "", "title_en": te or ""})
                if ev:
                    c3 = conn.execute(
                        "DELETE FROM pushed_stories WHERE ticker=? AND event_key=?",
                        (ticker, ev))
                    result["ledger_removed"] += c3.rowcount
                    result["event_cleared"] = ev
        except Exception as exc:
            print(f"  [warn] could not clear push ledger: {exc}", file=sys.stderr)
    conn.commit()
    return result


def acquire_run_lock():
    """
    Take an exclusive lock so two updater processes cannot run at once.

    cron fires twice a day, but the panel's "Run now" can start a second run
    while the first is still going. Both would write news.db (WAL + a 30s busy
    timeout mostly hides it) and, worse, BOTH could send a Telegram digest -
    a duplicate message with no way to tell which run produced it.

    Returns (state, handle):
      ('acquired', file)  - this process owns the run; keep the handle open
      ('busy', None)      - another run holds it; the caller should exit quietly
      ('unavailable', None) - no locking here (e.g. no fcntl, or --no-write);
                            carry on, the old behaviour
    """
    if NO_WRITE:
        return "unavailable", None
    try:
        import fcntl  # POSIX only; the updater runs on the Linux VM
    except Exception:
        return "unavailable", None
    try:
        fh = open(os.path.join(BASE_DIR, RUN_LOCK_FILE), "w")
    except Exception:
        return "unavailable", None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return "busy", None
    except Exception:
        fh.close()
        return "unavailable", None
    try:
        fh.write(str(os.getpid()))
        fh.flush()
    except Exception:
        pass
    return "acquired", fh


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
      - DELETE pushed_stories entries older than EVENT_REPEAT_WINDOW_DAYS * 3.
        That table is the repeat/event gate's memory and was NEVER pruned: rows
        accumulate forever (~55 pushes / 10 days = ~2,000 rows/year), because a
        new URL for the same event inserts a new row. Keeping a few multiples of
        the event window preserves the gate's behaviour exactly (an event only
        suppresses within event_repeat_window_days) while bounding growth.
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
        cutoff_pushed = (datetime.now(EASTERN)
                         - timedelta(days=max(1, EVENT_REPEAT_WINDOW_DAYS) * 3)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute("DELETE FROM news WHERE first_seen < ?", (cutoff_news,))
        rows_deleted += cur.rowcount
        cur = conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff_seen,))
        rows_deleted += cur.rowcount
        cur = conn.execute("DELETE FROM pushed_stories WHERE pushed_at < ?",
                           (cutoff_pushed,))
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
    says no) and they silently never reach a digest. Items that a busy run
    deferred at the trim step land in the same state.

    This runs BEFORE the fetch loop, so everything it finds was stored by a
    PREVIOUS run. Re-queued items go through the normal pipeline: AI analysis,
    importance floor, AI veto, per-ticker caps - nothing is pushed blindly.
    Rows the previous run deliberately stored-only (importance set, pushed=0)
    are NOT touched.

    Two hard-won details:
      - the batch is bounded generously (ORPHAN_RESCUE_LIMIT, not
        max_items_per_run) so a backlog always drains instead of starving
        behind the newest items;
      - every attempt increments `rescues` and rows that keep failing are left
        alone after ORPHAN_MAX_RESCUES, so a permanently unanalyzable row can
        never loop forever.
    """
    if NO_WRITE or conn is None:
        return []
    cutoff = (datetime.now(EASTERN)
              - timedelta(days=NEWS_RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    limit = ORPHAN_RESCUE_LIMIT
    try:
        rows = conn.execute(
            "SELECT ticker, source, item_hash, title_raw, url, snippet, lang, "
            "published_at, first_seen FROM news "
            "WHERE importance IS NULL AND pushed=0 AND first_seen >= ? "
            "AND COALESCE(rescues, 0) < ? "
            "ORDER BY first_seen DESC LIMIT ?",
            (cutoff, ORPHAN_MAX_RESCUES, limit),
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
    if items and not NO_WRITE:
        # Count the attempt now: if this run dies again before analysis, the
        # next run still sees progress instead of retrying the same row forever.
        try:
            for it in items:
                conn.execute("UPDATE news SET rescues = COALESCE(rescues, 0) + 1 "
                             "WHERE ticker=? AND source=? AND item_hash=?",
                             (it["ticker"], it["source"], it["_hash"]))
            conn.commit()
        except Exception as exc:
            print(f"  [warn] orphan-rescue bookkeeping failed: {exc}", file=sys.stderr)
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


def _is_stale_item(item, max_age_hours=MAX_NEWS_AGE_HOURS):
    """
    True if the item's publish time PARSES and is older than max_age_hours.
    This is the hard freshness backstop: search engines/APIs (EXA neural
    search especially) sometimes ignore their own date filters and serve
    months-old stories as "new"; whatever a source claims, nothing older
    than this window may enter the pipeline. Undated items return False
    here (nothing to check) - the per-source delta filters handle those
    where a usable date exists.
    """
    try:
        hours = float(max_age_hours)
    except Exception:
        hours = float(MAX_NEWS_AGE_HOURS)
    if hours <= 0:
        return False  # backstop disabled via config (max_news_age_hours: 0)
    dt = _parse_pub(item.get("date", "") or item.get("published_at", ""))
    if dt is None:
        return False
    return dt < datetime.now(EASTERN) - timedelta(hours=hours)


def _item_age_hours(item):
    """Age in hours from the item's publish time to now, or None if undated."""
    dt = _parse_pub(item.get("published_at") or item.get("date") or "")
    if dt is None:
        return None
    return (datetime.now(EASTERN) - dt).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Relevance + duplicate detection (the "why is this in my digest?" layer)
# ---------------------------------------------------------------------------
# Titles carry a lot of machine noise that must not count as "content" when
# comparing two headlines: Google News appends " - Publisher", most items end
# with the outlet name after a dash/pipe, and HUIZ's own name keeps appearing
# inside story titles.
_TITLE_NOISE_RE = re.compile(
    r"(?i)\b(h1|q[1-4]|fy\s?\d{2}|20\d{2})\b|谷歌新闻|新浪财经|网易订阅|"
    r"东方财富|腾讯新闻|搜狐|百度|今日头条|- ?[^-]{2,28}$|\| ?[^|]{2,28}$")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "as", "is", "are", "its", "it", "after", "before", "from",
    "news", "stock", "stocks", "shares", "says", "said", "new", "will",
    "co", "ltd", "inc", "corp", "company", "companies", "q2", "q1", "q3",
    "q4", "results", "report", "reports", "announces", "announced",
}


def _clean_title(title):
    """Lowercased title with publisher/noise suffixes stripped."""
    t = strip_tags(str(title or "")).lower()
    t = re.sub(r"\s+", " ", t)
    t = _TITLE_NOISE_RE.sub(" ", t)
    return t.strip(" -|·—–:：,，。.")


def story_tokens(title):
    """
    Comparable token set for a headline: latin words + CJK bigrams.

    CJK is split into overlapping bigrams because there is no word
    segmentation here - it makes 保险行业景气度 and 保险行业景气 comparable while
    keeping unrelated sentences apart. Returns a set (empty when the title is
    too short to compare safely).
    """
    t = _clean_title(title)
    if not t:
        return set()
    tokens = set()
    for w in re.findall(r"[a-z0-9][a-z0-9.%]*", t):
        if len(w) >= 3 and w not in _STOPWORDS:
            tokens.add(w)
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return tokens


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def _containment(a, b):
    """How much of the SMALLER token set appears in the larger one."""
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def _same_story(a, b):
    """
    Are two token sets the same story (a re-report, a syndicated copy, a
    different outlet's headline for one event)? Containment catches the
    "same headline, one with extra words" case that Jaccard alone misses.
    Very short token sets are only trusted when one fully contains the other,
    so unrelated three-word headlines cannot collapse.
    """
    if not a or not b:
        return False
    if min(len(a), len(b)) < 3:
        return _containment(a, b) >= 0.999
    if _containment(a, b) >= STORY_CONTAINMENT_MIN:
        return True
    return _jaccard(a, b) >= STORY_JACCARD_MIN


# Which source wins when several outlets carry the same story. The order
# encodes SIGNAL, not volume: a Chinese-language report, a filing or the
# company's own site beats a syndication aggregator.
STORY_SOURCE_RANK = {
    "SEC": 0, "RSS": 1, "GoogleNewsSite": 2, "GoogleNewsZH": 3,
    "Eastmoney": 4, "Eastmoney724": 5, "Sina724": 6, "Tavily": 7,
    "GoogleNews": 8, "Exa": 9, "Baidu": 10,
}


def _story_rank(item):
    """Lower is better: source signal, then Chinese, then dated, then snippet."""
    return (
        STORY_SOURCE_RANK.get(item.get("source", ""), 50),
        0 if item.get("lang") == "zh" else 1,
        0 if (item.get("published_at") or item.get("date")) else 1,
        0 if item.get("snippet") else 1,
    )


def dedupe_same_story(items):
    """
    Collapse the SAME story arriving from several sources into one primary
    item (the per-source hash cannot see across sources, and the AI folding
    runs too late to stop the digest filling with four copies of one earnings
    release). Keeps the best-sourced copy per story, marks the rest
    `_superseded` - they are still analysed and stored for browsing, but they
    can never occupy a digest seat. Returns (kept, collapsed_count).
    """
    clusters = []  # list of {"tokens": set, "members": [items]}
    for it in items:
        tok = story_tokens(it.get("title", ""))
        it["_story_tokens"] = tok
        placed = False
        if tok:
            for cl in clusters:
                # Only compare within a ticker: the same sector headline under
                # two tickers is a relevance problem, not a duplicate one.
                if cl["ticker"] != it.get("ticker"):
                    continue
                if _same_story(tok, cl["tokens"]):
                    cl["members"].append(it)
                    placed = True
                    break
        if not placed:
            clusters.append({"tokens": tok, "ticker": it.get("ticker"),
                             "members": [it]})
    kept, collapsed = [], 0
    for cl in clusters:
        primary = min(cl["members"], key=_story_rank)
        kept.append(primary)
        for it in cl["members"]:
            if it is not primary:
                it["_superseded"] = True
                collapsed += 1
    return kept, collapsed


def _company_name_tokens(names):
    """
    Normalised, abbreviated forms of the company's OWN names used for the
    relevance check. Alphabet-only suffixes ('Inc', 'Holdings', 'Ltd') are
    dropped so 'Yuanbao Inc.' matches a title that says 'Yuanbao'; short
    forms are only used when they are at least 4 characters, so a bare
    'LU'/'YB' ticker cannot make every article look relevant.
    """
    out = set()
    for nm in names:
        nm = re.sub(r"\s+", " ", str(nm or "").strip())
        if not nm:
            continue
        out.add(nm.lower())
        stripped = re.sub(r"(?i)\b(inc|corp|corporation|ltd|limited|holdings?|"
                          r"group|company|co|technolog(y|ies)|plc|sa|nv)\b\.?", "", nm)
        stripped = re.sub(r"\s+", " ", stripped).strip(" .,-")
        if len(stripped) >= 4:
            out.add(stripped.lower())
    return out


# Latin words that look like a company name inside a longer phrase but are not
# a company mention on their own.
_GENERIC_NAME_WORDS = {
    "china", "chinese", "automotive", "insurance", "holdings", "holding",
    "technology", "technologies", "financial", "finance", "systems", "group",
    "global", "digital", "online", "national", "international", "bank",
    "capital", "consumer", "credit", "loan", "loans", "technology",
}


def mentions_company(hay, names):
    """
    Does this text actually mention the company (by its own name/aliases)?

    This is the gate that keeps sector news with no connection to the stock
    out of the per-ticker digest - the 'El Niño is redrawing the insurance
    industry's risk map' class of item. Terms are matched with _term_in_text,
    which rejects coincidental CJK substrings, and short Latin names are only
    accepted when they appear as a whole word in a meaningful phrase.
    """
    if not hay:
        return False
    for name in _company_name_tokens(names):
        if is_chinese(name):
            if _term_in_text(name, hay):
                return True
            continue
        if len(name) < 4:
            continue
        if name in _GENERIC_NAME_WORDS:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", hay):
            return True
    return False


# Sector vocabularies for the optional sector_watch tier. Deliberately
# NARROW multi-character phrases: a bare 保险 or 汽车 would match half of
# everything and re-create the noise this tier exists to contain.
SECTOR_VOCAB = (
    (("保险", "insur"), ["保险业", "险企", "保险法", "保险中介", "偿付能力",
                         "insurance industry", "insurers", "insurer",
                         "insurance regulator"]),
    (("汽车", "automotive", "auto"), ["汽车行业", "车企", "整车", "零部件",
                                       "automakers", "auto parts",
                                       "automotive industry"]),
    (("助贷", "信贷", "消费金融", "lending", "fintech"),
     ["助贷", "消费金融", "互联网贷款", "小额贷款", "贷款余额",
      "loan facilitation", "consumer lending", "assisted loans"]),
    (("房地产", "property", "real estate"), ["房地产", "房企", "地产"]),
)


def sector_tokens_for(meta):
    """Sector phrases that apply to this company (see SECTOR_VOCAB)."""
    names = [str(meta.get("name_zh") or ""), str(meta.get("name_en") or "")]
    names += [str(x) for x in (meta.get("aliases_zh") or [])]
    names += [str(x) for x in (meta.get("subsidiaries_zh") or [])]
    names += [str(x) for x in (meta.get("subsidiaries_other") or [])]
    names += [str(x) for x in (meta.get("keywords") or [])]
    hay = " ".join(names).lower()
    out = []
    for triggers, tokens in SECTOR_VOCAB:
        if any(t.lower() in hay for t in triggers):
            for tok in tokens:
                if tok not in out:
                    out.append(tok)
    return out[:6]


def resolve_relevance(meta):
    """
    Build the relevance context for one ticker:
      names    - the company's own names/aliases, the only ones that count as
                 a real mention (subsidiaries are NOT company names)
      domains  - its discovered websites
      strict   - True when a source query contains ONLY company-specific names
      strong   - True when the source is a company-lookup source at all
      sector_tokens - narrow sector phrases for the sector_watch tier
    """
    names = [str(meta.get("name_zh") or ""), str(meta.get("name_en") or "")]
    names += [str(x) for x in (meta.get("aliases_zh") or [])]
    names = [n.strip() for n in names if n and n.strip()]
    return {
        "names": names,
        # Subsidiary/brand names (分期乐, Fenqile, 奇富借条, 360数科...). News
        # about a subsidiary IS news about the stock, so these count as a
        # genuine mention too - they just cannot be the ONLY thing a query is
        # built from.
        "subsidiary_names": [
            str(x).strip() for x in ((meta.get("subsidiaries_zh") or [])
                                     + (meta.get("subsidiaries_other") or []))
            if x and str(x).strip()],
        "domains": build_site_domains(meta),
        "ticker": str(meta.get("ticker") or ""),
        "sector_tokens": sector_tokens_for(meta),
        "strict": False,
        "strong": bool(names),
    }


def passes_relevance(item, meta, resolved):
    """
    How relevant is this item to the company? Returns:

      'company'   - the text mentions the company's own names/aliases, its
                    ticker as a whole word, or one of its domains. Always kept.
      'sector'    - a SECTOR story (the company's business area, no mention of
                    the company). Kept only when the caller enables the
                    separately-capped sector_watch tier, or when the query
                    itself was company-specific; otherwise dropped. This is
                    the 'El Niño is redrawing the insurance industry's risk
                    map' class of item.
      'unrelated' - nothing to do with the company or its sector. Dropped, and
                    never sent to the AI.
    """
    hay = " ".join(str(item.get(k) or "") for k in ("title", "snippet")).lower()
    if mentions_company(hay, resolved["names"]):
        return "company"
    # A subsidiary/brand mention (分期乐, Fenqile, 奇富借条) is a real company
    # mention: the news is about the stock's own business, not its sector.
    if mentions_company(hay, resolved.get("subsidiary_names") or []):
        return "company"
    ticker = str(item.get("ticker") or "").lower()
    if ticker and re.search(r"(?<![a-z0-9])" + re.escape(ticker) + r"(?![a-z0-9])", hay):
        return "company"
    for domain in resolved["domains"]:
        if domain and domain in hay:
            return "company"
    # Not about the company by name. Sector vocabularies are deliberately
    # narrow: broad words like 金融/监管 alone would match almost everything.
    if resolved["sector_tokens"] and any(t in hay for t in resolved["sector_tokens"]):
        return "sector"
    if resolved["strict"] and resolved["strong"]:
        # The query itself was company-specific, so a result from it is at
        # least on the company's sector. Flag it as sector, never as company.
        return "sector"
    return "unrelated"


def ingest_items(conn, all_new, source, items, ticker, since_dt, max_age_hours,
                 relevance_resolved=None, sector_watch=False, sector_items=None,
                 run_start=None, report=None):
    """
    Fold one source's raw results into the DB / all_new for a ticker.

    Every source goes through the SAME gate order:
      1. delta filter (pub_dt < since_dt -> too old for this source)
      2. story_tokens computed once, reused by relevance, dedup and the
         repeat gate downstream
      3. hard freshness backstop (MAX_NEWS_AGE_HOURS)
      4. RELEVANCE: does it mention the company, or is it merely its sector?
      5. exact per-source dedup (the source+url hash in `seen`)
    Returns the number of items accepted into `all_new`. `report` collects drop
    counts for the run log so it is visible WHY items disappear.
    """
    accepted = 0
    rep = report if report is not None else {}

    def bump(key):
        rep[key] = rep.get(key, 0) + 1

    for item in items:
        item["ticker"] = ticker
        pub_dt = _parse_pub(item.get("date", ""))
        if since_dt is not None and pub_dt and pub_dt < since_dt:
            bump("older_than_delta")
            continue
        if not item.get("_story_tokens"):
            item["_story_tokens"] = story_tokens(item.get("title", ""))
        if _is_stale_item(item, max_age_hours):
            bump("stale")
            continue
        if relevance_resolved is not None:
            verdict = passes_relevance(item, None, relevance_resolved)
            if verdict == "unrelated":
                bump("unrelated")
                continue
            if verdict == "sector":
                if not sector_watch:
                    bump("sector_off")
                    continue
                item["_sector"] = True
        item["published_at"] = _normalize_pub(item.get("date", ""))
        if run_start:
            item["first_seen"] = run_start
        if not is_new(conn, ticker, item["source"], item["id"], item["title"]):
            bump("already_seen")
            continue
        mark_seen(conn, ticker, item["source"], item["id"], item["title"],
                  item.get("url", ""))
        insert_news(conn, item)
        if sector_items is not None and item.get("_sector"):
            sector_items.append(item)
            bump("sector_kept")
        else:
            all_new.append(item)
            accepted += 1
    return accepted


# ---------------------------------------------------------------------------
# Company lookup + auto-discovery (the "alpha" config)
# ---------------------------------------------------------------------------
def term_quality(term):
    """
    How useful is this term for FINDING news? Lower is better.

    The single biggest recall problem was the budget: MAX_ZH_TERMS is 8 and
    discovery returns full legal entity names ("湖北恒隆汽车系统集团有限公司",
    "上海陆家嘴国际金融资产交易市场股份有限公司"), which pushed the short brand
    names that news actually uses ("分期乐", "平安普惠") out of the slots. A
    legal-registry name is a precise filter but a poor search term.

    Ranking: the company's own short name first, then brands/subsidiaries by
    length, then long legal names, then keywords (which are often market-data
    symbols like "YB.US" rather than search terms).
    """
    t = str(term or "").strip()
    if not t:
        return 99
    cjk = is_chinese(t)
    if cjk:
        n = len(t)
        if n <= 4:
            return 0          # brand-length: 乐信, 分期乐, 桔子理财, 元保
        if n <= 6:
            return 1          # short entity: 乐信集团, 元保科技
        if n <= 10:
            return 3
        return 5              # full legal name: 深圳分期乐网络科技有限公司
    # Latin terms: a brand name beats a long corporate name, and a bare
    # ticker/symbol ("YB.US", "LX") is not a search term at all. Latin terms
    # belong to build_en_terms(), not here.
    if re.fullmatch(r"[A-Za-z0-9.\-]{1,8}", t):
        return 9
    words = t.split()
    if len(words) >= 2:
        return 7              # "Huize Holding" - an EN term, not a ZH one
    return 8


def build_zh_terms(meta):
    """
    Chinese search terms from a company profile, ranked by usefulness.

    Sources: name_zh, aliases_zh, subsidiaries_zh, subsidiaries_other AND
    `keywords` - the lookup has always stored discovered keywords ("元保数科",
    "元保香港", "云犀科技", "平安陆金所") and this function simply ignored them, so
    names discovery had found were never searched.

    Ranking matters because the budget is only MAX_ZH_TERMS (8). Discovery
    returns full legal entity names ("湖北恒隆汽车系统集团有限公司"), and putting
    those first pushed the short brand names that headlines actually use
    ("分期乐", "平安普惠") out of the slots. Short brand-length names win.

    Latin terms are excluded here (they are handled by build_en_terms) - an
    English word in a Chinese query just dilutes it.
    """
    if not meta:
        return []
    candidates = []   # (rank, insertion order, term)

    def add(value, rank_hint=None):
        v = str(value or "").strip()
        if not v or not is_chinese(v):      # ZH list only
            return
        candidates.append((term_quality(v) if rank_hint is None else rank_hint,
                           len(candidates), v))

    add(meta.get("name_zh"), rank_hint=0)       # the company's own name always
    for key in ("aliases_zh", "subsidiaries_zh", "subsidiaries_other", "keywords"):
        for v in (meta.get(key) or []):
            add(v)
    # Stable sort by rank, keeping insertion order inside a rank, then dedupe.
    ordered, seen = [], set()
    for _, _, term in sorted(candidates, key=lambda c: (c[0], c[1])):
        if term.lower() in seen:
            continue
        seen.add(term.lower())
        ordered.append(term)
    return ordered[:MAX_ZH_TERMS]


def build_en_terms(meta):
    """
    Non-Chinese search terms from a company profile: name_en +
    subsidiaries_other (e.g. Fenqile, Fenqile Indonesia) and Latin keywords.
    Used for Tavily and Google News EN, so subsidiary news is found even when
    it never mentions the ticker symbol or the Chinese name.
    """
    terms = []
    if meta:
        v = str(meta.get("name_en") or "").strip()
        if v and v not in terms:
            terms.append(v)
        for key in ("subsidiaries_other", "keywords"):
            for v in (meta.get(key) or []):
                v = str(v or "").strip()
                if not v or v in terms or is_chinese(v):
                    continue
                # Skip bare tickers/market symbols ("YB.US", "LX") - common
                # English words return noise, the ticker is already searched by
                # the Google News EN query itself.
                if re.fullmatch(r"[A-Z0-9.\-]{1,8}", v):
                    continue
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

    # Explicit config ticker_meta overrides the discovered entry.
    #
    # Scalar fields (name_zh, name_en, website...) are replaced outright, which
    # is what "override" should mean. LIST fields are UNIONED instead, because
    # replacing them silently discards everything discovery had found: a config
    # entry with `subsidiaries_zh: []` would otherwise throw away the 8 CAAS
    # entities the lookup had discovered. Nothing is lost, the user's own names
    # are simply added.
    cfg_meta = config.get("ticker_meta", {}).get(ticker, {}) or {}
    merged = dict(entry or {})
    list_keys = ("aliases_zh", "subsidiaries_zh", "subsidiaries_other", "keywords")
    for k, v in cfg_meta.items():
        if v in (None, "", [], {}):
            continue
        if k in list_keys and isinstance(v, list):
            old = list(merged.get(k) or [])
            merged[k] = list(dict.fromkeys([str(x).strip() for x in v if str(x).strip()]
                                           + [str(x).strip() for x in old if str(x).strip()]))
        else:
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
            # Google News wraps every article in an opaque
            # news.google.com/rss/articles/CBMi... link, but the feed names the
            # ORIGIN outlet in <source url="https://www.reuters.com">. Keeping
            # that domain gives the story layer a real signal for cross-source
            # duplicate detection (the Google link is only an opaque id).
            origin = ""
            src = entry.get("source")
            if isinstance(src, dict):
                origin = str(src.get("href") or "").strip()
            elif src:
                origin = str(getattr(src, "href", "") or "").strip()
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
                # Origin domain (from <source url>), or ''.
                "origin": origin,
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
    # China-policy anchors. These are what let a China headline read as macro:
    # the gate needs a strong anchor or TWO distinct hits (see is_macro), so a
    # US Fed headline that merely contains 加息 no longer qualifies.
    r"央行", r"利率体系", r"六大行|国有大行", r"公开市场操作",
    r"财政部", r"证监会", r"国家发改委|发改委", r"中国人民银行",
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
        # DELTA ENFORCEMENT: startPublishedDate above is only a HINT - EXA's
        # neural search regularly ignores it and serves months-old pages
        # (recycled stories that would then be AI-scored and PUSHED as "new").
        # Verify the returned publishedDate ourselves, like Tavily/the wires:
        #   - dated result older than since_dt -> dropped;
        #   - news-category result with NO parsable date -> also dropped
        #     (its freshness cannot be proven). Discovery/company searches
        #     (category="company") are exempt - profile pages aren't news.
        enforce_delta = category == "news" and since_dt is not None
        items = []
        dropped = 0
        for r in data.get("results", []) or []:
            title = (r.get("title") or "").strip()
            url = r.get("url") or ""
            if not title:
                continue
            if enforce_delta:
                pub_dt = _parse_pub(r.get("publishedDate") or "")
                if pub_dt is None or pub_dt < since_dt:
                    dropped += 1
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
        if dropped:
            print(f"  [exa] dropped {dropped} stale/undated result(s) "
                  f"(delta window starts {since_dt.strftime('%Y-%m-%d %H:%M')}).")
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
def ai_analyze(items, config, secrets, meta_map, conn=None, run_start=None,
               prompt_mode=None, prompt_extra=""):
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
                # Original publish date (ET, '' if undated) - lets the model
                # recognize RECYCLED old stories and veto them (known_event).
                "published": it.get("published_at") or _normalize_pub(it.get("date", "")),
            }
            if it.get("snippet"):
                entry["snippet"] = it["snippet"][:200]
            lines.append(entry)
        # Folded semantic dedup: show what has already been seen for this
        # ticker so the model can flag recycled/same-event stories
        # (known_event) - this replaces the old separate dedup AI call.
        #
        # The two blocks are labelled differently on purpose. "ALREADY SEEN
        # BEFORE THIS RUN" is the ledger up to run_start (so an item is never
        # compared against itself), while the ITEMS block is the current batch,
        # which the prompt tells the model to compare against ITSELF - that is
        # how cross-source copies of one event (different outlet, different
        # URL) get collapsed instead of filling the digest with duplicates.
        history = (get_recent_pushed_titles(conn, ticker, limit=SEMANTIC_DEDUP_HISTORY)
                   if conn else [])
        hist_lines = [f"- {t}" for t in history]
        mode = prompt_mode or ("macro" if ticker == "MACRO"
                               else "global" if ticker == "GLOBAL"
                               else "sector" if ticker.endswith("~SECTOR")
                               else "ticker")
        if mode in ("macro", "global"):
            # Macro tiers: framed around impact on the user's fintech names.
            analyst = ("You are a China macro analyst. Below are major China "
                       "policy / market news items found today (already gated "
                       "as macro-relevant).\n"
                       if mode == "macro" else
                       "You are a global-markets analyst. Below are US/global "
                       "market news items that could move Chinese ADRs.\n")
            impact_scope = (
                "what this means for US-listed Chinese fintech/consumer-lending "
                "companies like 奇富科技 QFIN, 乐信 LX, 陆金所 LU - e.g. 'cheaper "
                "funding for 分期乐's lending' or 'tighter assisted-loan rules "
                "pressure origination volume'; empty string if not applicable"
                if mode == "macro" else
                "what this means for US-listed Chinese ADRs, especially "
                "consumer-lending fintechs (奇富科技 QFIN, 乐信 LX, 陆金所 LU); "
                "empty string if not applicable")
            prompt = (
                analyst
                + prompt_extra
                + "For EACH item return one JSON object with keys:\n"
                "  number, title_en (concise English translation), summary (one "
                "English sentence), category (one of monetary, fiscal, "
                "regulatory, fintech_reg, market, geopolitical, other), "
                "importance (integer 1-10), sentiment "
                "(positive/negative/neutral), push (true if the user must know "
                "about it NOW), duplicate_of (number of an earlier item that is "
                "the same event, else null), known_event (true if same as an "
                "ALREADY SEEN headline), reason (one short English sentence why "
                "it matters), impact (one short English sentence: "
                + impact_scope + ").\n"
                "Return ONLY a JSON array of these objects, same order as the items.\n\n"
                "ALREADY SEEN BEFORE THIS RUN:\n"
                + ("\n".join(hist_lines) if hist_lines else "(none)")
                + "\n\nITEMS (all from THIS run - compare THESE against each other):\n"
                + json.dumps(lines, ensure_ascii=False)
            )
        elif mode == "sector":
            # Sector tier: industry news that never mentions the company. The
            # prompt is explicit that this is CONTEXT, not a company event.
            prompt = (
            f"The items below are INDUSTRY/sector news relevant to the business "
            f"area of the stock {ticker}"
            f"{(' (' + name_zh + ')') if name_zh and name_zh != ticker else ''}. "
            f"They do NOT mention the company itself - they are context.\n"
            + prompt_extra +
            "For EACH item return one JSON object with keys:\n"
            "  number, title_en (concise English translation), summary (one "
            "English sentence), category (one of regulatory, market, "
            "press_release, other), importance (integer 1-10), sentiment "
            "(positive/negative/neutral), push (true only if this sector "
            "development is material for the company's business), "
            "duplicate_of (number of an earlier item that is the same event, "
            "else null), known_event (true if same as an ALREADY SEEN headline), "
            f"reason (one short English sentence why the sector context matters "
            f"for {ticker}), impact (one short English sentence: what this "
            f"means for {ticker}'s business or stock; empty string if not "
            "applicable).\n"
            "Return ONLY a JSON array of these objects, same order as the items.\n\n"
            "ALREADY SEEN BEFORE THIS RUN:\n"
            + ("\n".join(hist_lines) if hist_lines else "(none)")
            + "\n\nITEMS (all from THIS run - compare THESE against each other):\n"
            + json.dumps(lines, ensure_ascii=False)
            )
        else:
            prompt = (
            f"You are an investor's news analyst for the stock {ticker} "
            f"({name_zh}{(' / ' + name_en) if name_en else ''}"
            f"{(' | official site: ' + site) if site else ''}).\n"
            f"Chinese-language news about this company or its subsidiaries "
            f"(e.g. {subs}) is especially valuable - weigh it heavily; it often "
            f"contains information English media misses.\n"
            + prompt_extra
            + "Below are NEW items found today. Each has a number, source, "
            "language, title, publish date, and a short snippet.\n"
            "FRESHNESS RULE: if an item's 'published' date is days or more in "
            "the past relative to today, it is a recycled OLD story, not "
            "breaking news - set known_event=true and push=false for it "
            "unless something genuinely new happened.\n"
            "SAME STORY RULE: the items below come from MANY outlets, so "
            "several of them are usually the SAME story reported by different "
            "sources (e.g. one earnings release covered by four outlets). "
            "Exactly ONE item per story may be pushed. Pick the single best "
            "one (prefer the Chinese-language original or the most specific "
            "report) and mark every other copy duplicate_of that item's "
            "number. Two items are the same story when they describe the same "
            "event - even if the wording, the outlet and the URL differ. An "
            "item that is merely about the same INDUSTRY or the same broad "
            "topic is NOT a duplicate; it is a separate story.\n"
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
            "ALREADY SEEN BEFORE THIS RUN (do NOT use these to compare the "
            "items below against each other):\n"
            + ("\n".join(hist_lines) if hist_lines else "(none)")
            + "\n\nITEMS (all from THIS run - compare THESE against each other):\n"
            + json.dumps(lines, ensure_ascii=False)
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


def get_recent_pushed_titles(conn, ticker, limit=SEMANTIC_DEDUP_HISTORY):
    """
    The "ALREADY REPORTED TO THE USER" history for one ticker, newest first.

    This reads `pushed_stories` - the ledger of what actually went out - rather
    than the `seen` ledger, which holds EVERY item ever fetched. Feeding the
    model the fetch log meant ~91% of its "already seen" list was junk that was
    never sent: price-ticker pages ("HUIZ|Huize Holding Ltd|Price:1.480|
    Chg%:-0.010"), product FAQ pages, and duplicate spam. The model was being
    told those were news the user had already received, which is one route by
    which genuinely new items got marked known_event and suppressed.

    "Already seen" should mean "already delivered", so it now does.
    """
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT title, pushed_at FROM pushed_stories WHERE ticker=? "
            "ORDER BY pushed_at DESC LIMIT ?", (ticker, limit)).fetchall()
    except Exception:
        return []
    return [f"{r[0]}  [sent {str(r[1])[:10]}]" for r in rows if r[0]]


def get_recent_seen_titles(conn, ticker, limit=SEMANTIC_DEDUP_HISTORY, before=None,
                           exclude=None):
    """
    Return recent already-seen titles for a ticker (from the `seen` ledger),
    newest first. This history is folded into the per-ticker AI-analysis
    prompt so the model can flag recycled / same-event stories (known_event)
    without a separate AI call.

    'before' excludes the current run's items (marked 'seen' during the fetch
    loop) so the "already reported BEFORE this run" block is unambiguous.

    'exclude' drops specific titles from the history - used by ai_analyze to
    remove the CURRENT BATCH's own titles from the "already reported" block.
    Cross-source copies of one event arrive as separate numbered items, so the
    prompt lists the current batch explicitly instead - that is what lets the
    model recognise item 5 as the same story as item 1 without ever comparing
    an item against itself.
    """
    if before:
        cur = conn.execute(
            "SELECT title, first_seen FROM seen WHERE ticker=? AND first_seen < ? "
            "ORDER BY first_seen DESC LIMIT ?",
            (ticker, before, limit),
        )
    else:
        cur = conn.execute(
            "SELECT title, first_seen FROM seen WHERE ticker=? "
            "ORDER BY first_seen DESC LIMIT ?",
            (ticker, limit),
        )
    rows = cur.fetchall()
    if exclude:
        ex = set(exclude)
        return [f"{r[0]}  [first seen {str(r[1])[:10]}]"
                for r in rows if r[0] and r[0] not in ex]
    return [f"{r[0]}  [first seen {str(r[1])[:10]}]" for r in rows if r[0]]


# Global-markets patterns: genuinely systemic US/global news that moves
# Chinese ADRs. Kept SEPARATE from the China-macro tier (below) so a Fed
# headline no longer arrives labelled as "China macro".
GLOBAL_MARKET_PATTERNS = [
    r"美联储", r"鲍威尔", r"沃勒", r"FOMC", r"非农", r"美国CPI|CPI数据",
    r"ADP数据|ADP就业", r"联邦基金利率", r"点阵图", r"美国通胀|美国PPI",
    r"美国国债收益率|美债收益率", r"华尔街|标普500|纳斯达克指数|道琼斯",
    r"美股", r"美元指数", r"人民币汇率|离岸人民币",
    # Non-China central banks / economies: a foreign rate decision is global
    # markets news, NOT China macro, even though the headline says 加息.
    r"欧洲央行|欧央行|ECB", r"日本央行|日银", r"英国央行|英格兰银行",
    r"韩国央行|澳洲联储|加拿大央行|瑞士央行",
]
# The subset of those patterns that unambiguously identifies a FOREIGN subject:
# a foreign central bank or a US-economy datapoint. An item matching one of
# these never counts as China macro unless it ALSO carries a strong China
# anchor (see is_macro). Generic market words (美股/美债/华尔街) are NOT here:
# they are useful for labelling the global tier but too weak to overrule a
# China headline.
FOREIGN_CENTRAL_BANK_PATTERNS = [
    r"美联储", r"鲍威尔", r"沃勒", r"FOMC", r"欧央行", r"欧洲央行", r"ECB",
    r"日本央行", r"日银", r"英国央行", r"英格兰银行", r"韩国央行",
    r"澳洲联储", r"加拿大央行", r"瑞士央行", r"美国非农", r"美国CPI",
]
# US / foreign sovereign and market subjects: an item about these that carries
# no China anchor is GLOBAL MARKETS news, not China macro. This is what keeps
# "美国财政部8周期国库券中标利率" and "华尔街警告美国AI债务扩张" out of the
# China section.
FOREIGN_SUBJECT_PATTERNS = FOREIGN_CENTRAL_BANK_PATTERNS + [
    r"美国财政部", r"美国国债", r"美债", r"华尔街", r"标普500",
    r"纳斯达克", r"道琼斯", r"美股",
]
# Strong, specific CHINA-policy anchors. One of these is enough to make a
# headline count as China macro on its own.
#
# NOTE: generic rate-move vocabulary (降息/加息/降准/逆回购/非农/关税) is
# deliberately NOT here. "ECB hikes 25bp" also matches 加息 and 逆回购-like
# wording, so treating those as anchors put a European Central Bank decision in
# the CHINA MACRO section. Generic monetary words only count as one half of the
# two-distinct-hits test; these anchors are the China-specific policies.
STRONG_MACRO_PATTERNS = [
    r"LPR", r"贷款市场报价利率", r"MLF", r"存款准备金",
    r"特别国债", r"专项债", r"国常会", r"政治局",
    r"中央经济工作会议", r"中央金融工作会议",
    r"助贷", r"网络小贷", r"网络小额贷款", r"互联网小额贷款",
    r"消费金融", r"金融监管总局", r"银保监会", r"国家金融监督管理总局",
    r"互联网金融", r"互联网贷款", r"小额贷款", r"现金贷",
    r"中概股", r"中国金龙", r"退市新规", r"实体清单", r"出口管制",
    r"财政部", r"证监会", r"国家发改委",
]


# China-side monetary anchors. A story carrying one of these is a China story
# even when it also mentions the Fed/ECB ("中国央行下调LPR 应对美联储加息").
#
# NOTE: a bare 央行 is deliberately NOT an anchor - it is a substring of
# 欧洲央行 / 英国央行 / 日本央行, so including it made ECB and BoE headlines look
# like China stories. Foreign bank names (美联储/欧洲央行/...) are likewise never
# anchors.
CHINA_ANCHOR_PATTERNS = [
    r"中国央行", r"中国人民银行", r"人民银行", r"降息", r"降准", r"LPR",
    r"贷款市场报价利率", r"MLF", r"逆回购", r"存款准备金",
    # A China index is a China story even though its name contains 纳斯达克.
    r"中国金龙", r"中概股",
]


def is_foreign_central_bank(text):
    """True when the text is about a NON-China central bank / economy."""
    return has_foreign_subject(text)


def has_foreign_subject(text):
    """True when the text's subject is foreign (a non-China central bank, the
    US Treasury, US equities...). See FOREIGN_SUBJECT_PATTERNS."""
    if not text:
        return False
    return any(re.search(p, text) for p in FOREIGN_SUBJECT_PATTERNS)


def _foreign_subject_only(text):
    """
    The item's subject is foreign AND it carries no China anchor.

    This is the single rule that keeps the two macro tiers mutually exclusive:
    "ECB hikes 25bp" and "美国财政部回购国债" are global, while
    "中国央行下调LPR 应对美联储加息" is China macro (it has a China anchor).
    """
    if not text or not has_foreign_subject(text):
        return False
    return not any(re.search(p, text) for p in CHINA_ANCHOR_PATTERNS)


def macro_score(text, extra=None):
    """(number of distinct China-macro patterns matched, pattern list)."""
    if not text:
        return 0, []
    pats = MACRO_KEYWORDS + [str(p) for p in (extra or []) if str(p).strip()]
    hits = [p for p in pats if re.search(p, text)]
    return len(hits), hits


def is_macro(text, extra=None):
    """
    Free regex gate: does this item look like BIG China macro news?

    A single generic hit is not enough. '加息' or '关税' alone used to qualify
    US Fed and tariff headlines as China macro, which is how the CHINA MACRO
    section ended up carrying US non-farm payrolls, Fed commentary and even an
    ECB rate decision. So:
      - a foreign central bank / US economy reference alone never qualifies;
      - one hit only counts when it is a strong, specific China-policy pattern;
      - two distinct hits always count (a China story mentioning the Fed too
        still qualifies).
    """
    if not text:
        return False
    # A PURE foreign central bank / US-economy reference is never China macro,
    # however many generic 加息/逆回购-style words the headline also carries
    # ("ECB hikes 25bp, Lagarde flags sticky inflation"). A China anchor wins:
    # "中国央行下调LPR 应对美联储加息" is a China story.
    if _foreign_subject_only(text):
        return False
    hits, matched = macro_score(text, extra)
    if hits >= 2:
        return True
    if any(re.search(p, text) for p in STRONG_MACRO_PATTERNS):
        return True
    for p in matched:
        if any(re.search(p, str(x)) for x in (extra or [])):
            return True
    return False


def is_global_markets(text):
    """Free regex gate for the global-markets tier (see GLOBAL_MARKET_PATTERNS)."""
    if not text:
        return False
    return any(re.search(p, text) for p in GLOBAL_MARKET_PATTERNS)


def news_tier(text, extra=None):
    """
    Route one headline to its tier: 'macro', 'global' or 'ticker'.

    Order matters and is deliberately simple:
      1. a foreign central bank / US-economy reference WITHOUT a China anchor
         -> 'global' (an ECB or Fed headline that also contains 加息);
      2. a strong China-policy anchor -> 'macro';
      3. a China monetary anchor (央行/降息/LPR/逆回购...) -> 'macro';
      4. otherwise anything matching the global vocabulary -> 'global'.

    'extra' are the user's own macro_keywords (config), so a custom watchword
    routes exactly like the built-in ones.
    """
    if not text:
        return "ticker"
    if _foreign_subject_only(text):
        return "global"
    if any(re.search(p, text) for p in STRONG_MACRO_PATTERNS):
        return "macro"
    if is_macro(text, extra):
        return "macro"
    if is_global_markets(text):
        return "global"
    return "ticker"


def tag_global_markets(items):
    """Label each item's news tier ('macro' / 'global' / 'ticker')."""
    for it in items:
        it["_tier"] = news_tier(f"{it.get('title', '')} {it.get('snippet', '')}")
    return items


def format_global_markets(items):
    """
    The 🌍 GLOBAL MARKETS section: systemic US/global items that move Chinese
    ADRs (Fed decisions, payrolls, CPI). Capped hard and OFF by default - a
    daily index-close recap is not actionable, but a Fed pivot is.
    """
    if not items:
        return None
    lines = ["🌍 GLOBAL MARKETS — systemic US/global items", ""]
    for it in items:
        title = it.get("title_en") or it.get("title", "")
        pub = (it.get("published_at") or _normalize_pub(it.get("date", "")) or "")
        lines.append(f"• {title}" + (f" 📅{pub[:10]}" if pub else ""))
        if it.get("impact"):
            lines.append(f"    → {it['impact']}")
        if it.get("url"):
            lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def format_sector_watch(items):
    """
    The 🏭 SECTOR section - optional (config sector_watch). Sector news that
    does NOT mention the company is only useful when it is explicitly labelled
    as sector context, so it is never mixed into the stock's own items.
    """
    if not items:
        return None
    lines = ["🏭 SECTOR CONTEXT — industry news, not company-specific", ""]
    for it in items:
        title = it.get("title_en") or it.get("title", "")
        ticker = it.get("ticker", "")
        pub = (it.get("published_at") or _normalize_pub(it.get("date", "")) or "")
        lines.append(f"• [{ticker}] {title}" + (f" 📅{pub[:10]}" if pub else ""))
        if it.get("summary"):
            lines.append(f"    {it['summary']}")
        if it.get("impact"):
            lines.append(f"    → {it['impact']}")
        if it.get("url"):
            lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def select_global_markets(items, config):
    """Top-N global-markets items by importance (default cap 2)."""
    cap = max(1, _cfg_int(config, "global_markets_max_per_run",
                          GLOBAL_MARKETS_MAX_PER_RUN))
    ranked = sorted([it for it in items if not it.get("is_dup")
                     and not it.get("is_known") and it.get("push", True)],
                    key=lambda it: it.get("importance") or 0, reverse=True)
    return ranked[:cap]


def collect_macro_items(conn, config, secrets, wire_cache, src_on,
                        initial_hours, run_start, now_utc_str,
                        max_age_hours=MAX_NEWS_AGE_HOURS):
    """
    Collect the "must know" tiers from the wires + one Google News macro query.

    This gathers BOTH the China-macro items and the global-markets items, and
    the caller splits them with news_tier(). It only ever kept is_macro() items
    before, which made the 🌍 GLOBAL MARKETS tier unreachable: a Fed, CPI or
    payrolls headline is global but NOT China macro, so it was discarded here -
    before the tier split that would have routed it to "GLOBAL". Six of seven
    real global headlines could never be collected.

    Everything is stored under the "MACRO" pseudo-ticker for the delta bookkeeping
    (one last_fetched record per source); the caller re-labels the global ones.
    """
    if not config.get("macro_enabled", True):
        return []
    extra = [str(k) for k in (config.get("macro_keywords") or []) if str(k).strip()]

    def wanted(text):
        """Would the China-macro OR the global-markets tier want this item?
        (The caller splits the result with news_tier().) Being generous here is
        cheap: the gate is a free regex and the per-run caps still apply."""
        return news_tier(text) != "ticker"

    raw_items = []
    for wire_src in ("Eastmoney724", "Sina724"):
        raw = wire_cache.get(wire_src)
        if raw is None:
            print(f"  [warn] macro: {wire_src} wire unavailable (delta not advanced).")
            continue
        if raw:
            set_last_fetched(conn, "MACRO", wire_src, now_utc_str)
        for it in raw:
            if wanted(f"{it.get('title', '')} {it.get('snippet', '')}"):
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
                if wanted(f"{it.get('title', '')} {it.get('snippet', '')}"):
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
    candidates = []
    for it in raw_items:
        # Same hard freshness backstop as the per-ticker path: a macro wire/
        # search hit with an ancient publish time is a recycled story.
        if _is_stale_item(it, max_age_hours):
            continue
        hay = f"{it.get('title', '')} {it.get('snippet', '')}"
        # Drop routine market chatter BEFORE storing it. The wire carries every
        # tick ("US futures extend gains", "spot silver rises 1.34%", "euro
        # extends decline", "SK Hynix up 2% premarket", single-stock moves).
        # Storing those filled the browsable database with rows that can never
        # be pushed - and, because only the top few macro items get scored, most
        # of them also showed up with no importance at all.
        if market_noise(hay):
            continue
        key = (it["source"], it["id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        it["published_at"] = _normalize_pub(it.get("date", ""))
        it["first_seen"] = run_start
        if is_new(conn, "MACRO", it["source"], it["id"], it["title"]):
            candidates.append(it)

    # Store at most macro_store_max_per_run per run (default 12), not every
    # candidate the wires produced. These are the rows that get an AI score;
    # the rest never enter the database, so it stays a browsable record of
    # notable macro news instead of a dump of the tape.
    store_cap = max(1, _cfg_int(config, "macro_store_max_per_run",
                                MACRO_STORE_MAX_PER_RUN))
    if len(candidates) > store_cap:
        print(f"  [macro] {len(candidates)} candidate(s) passed the quality gate; "
              f"storing the newest {store_cap}.")
        candidates = candidates[:store_cap]
    for it in candidates:
        mark_seen(conn, "MACRO", it["source"], it["id"], it["title"], it.get("url", ""))
        insert_news(conn, it)
    return candidates


# ---------------------------------------------------------------------------
# Routine-market filter for the macro/global tiers
# ---------------------------------------------------------------------------
# The 7x24 wires carry the whole tape, not just news. Filtering only by
# "is this macro-ish" let the database fill with tick-by-tick market chatter:
# "US stock futures extend gains", "spot silver rises 1.34%", "euro extends
# decline against dollar, last at $1.1576", "SK Hynix up over 2% in premarket",
# "VIX falls 1.49 points", single-stock quotes. Those can never be actionable
# and only the top few macro items get an AI score, so the rest were stored with
# no importance at all - pure clutter.
ROUTINE_MARKET_PATTERNS = [
    r"盘前|盘后|premarket|pre-market|after-?hours",
    r"(涨幅|跌幅|升幅|降幅)?(扩大|收窄)(至|到)",
    r"(涨|跌|升|降)[\d.]+\s*(%|％|个百分点|个基点|点)",
    r"上涨|下跌|走高|走低|回落|反弹|跳水",
    r"报[\d.,]+\s*(美元|元|点|%|％)",
    r"最新报|现报|报收|收于|收盘价",
    r"extend(s|ed)?\s+(gains|losses|decline|rally)",
    r"(falls?|rises?|drops?|jumps?|gains?|slides?|climbs?|sinks?|retreats?|extends?|ticks?|eases?|holds?|wavers?|closes?)\b[^.;]{0,24}?\b[\d.]+\s*(%|point|bp|basis point)",
    r"(falls?|rises?|drops?|up|down)\s+(over|more than|nearly)\s+[\d.]+\s*%",
    r"创(日内|历史|阶段)?新(高|低)",
    r"一度|盘中|盘中触及",
    r"美元指数|dollar index",
    r"(欧洲|欧元|英镑|日元|美元).{0,12}(跌|涨|走低|走高|贬值|升值)",
    r"\b(euro|yen|sterling|yuan|dollar)\b[^.;]{0,40}\b(extends?|falls?|rises?|slips?|drops?|weakens?|strengthens?|gains?)\b",
    # Treasury yields / auctions / buybacks: market mechanics, not policy.
    r"(10|2|30|two|ten|thirty)[\s-]*year[^.;]{0,12}(treasury|note|bond)?[^.;]{0,12}(yield|收益率)",
    r"(国债|美债).{0,8}(收益率|回购|招标|中标|发行)",
    r"国库券|Treasury (bill|auction|buyback|note|bond)",
    r"国际金价|现货(黄金|白银|原油)|WTI|布伦特|Brent|COMEX",
    r"金价|油价|银价",
    r"VIX|恐慌指数|fear index",
    r"空头头寸|short interest",
    # Rate-odds chatter: the market's betting line, not a policy action.
    r"加息预期|降息预期|rate-?hike (odds|bets|expectations|probability)|"
    r"Fed (rate )?(odds|bets)|expects? .{0,12}(rate hike|rate cut|加息|降息)|"
    r"traders? .{0,24}(odds|bets|expect)|概率|押注|预计.{0,6}(加息|降息)",
    r"隔夜逆回购|reverse repo|RRP",
    r"外汇指数|currency index|emerging[- ]market",
    # A market reaction to a data release is still just a market reaction
    # ("After inflation data, euro extends decline..."). The release itself
    # ("US August CPI rises 3.4%") is kept by the noteworthy list.
    r"(after|following|despite|amid|on)\s+.{0,24}?\b(inflation|CPI|jobs|payroll|GDP)\b",
    # Chinese market statistics: a bare "上涨0.23%"-style move. A data RELEASE
    # states a comparison ("同比上涨3.2%", "高于预期") and is kept below.
    r"(上涨|下跌|上升|下降|回落|走高|走低|增长|下滑)\s*[\d.]+\s*(%|％|个百分点|点)",
    r"(数据|通胀数据)(公布|发布)?(后|之后)",
]
# A market item that ALSO carries one of these is real macro news and is kept.
# Deliberately NARROW: an actual policy ACT or a data RELEASE, never the
# market's reaction to one, and never the market's odds on one.
NOTEWORTHY_MARKET_PATTERNS = [
    r"加息\s*\d|降息\s*\d|上调.{0,6}利率|下调.{0,6}利率",
    r"(hikes?|cuts?|raises?|lowers?)\s+(rates?|the (benchmark|policy) rate)",
    r"利率决议|议息会议|点阵图|政策声明",
    # A data release: the noun followed by a reported change (never a bare
    # mention of "inflation data", which is usually a market reaction).
    r"(CPI|通胀|inflation|PPI|非农|payrolls?|失业率|GDP|出口|进口)"
    r"\s*(同比|环比)?\s*(rise|rose|rises|fall|fell|falls|jump|jumps|climb|climbs|"
    r"slow|slows|accelerat|ease|eases|beat|beats|miss|misses|print)",
    # Chinese data release: needs a comparison, which market stats lack.
    r"(同比|环比|超预期|高于预期|低于预期|不及预期|超出预期)",
    r"(CPI|GDP|非农|通胀)\s*(数据)?\s*(公布|发布|出炉)",
    r"衰退|recession", r"刺激|stimulus", r"关税|tariff", r"制裁|sanction",
    r"降准|LPR|MLF|存款准备金|货币政策|宽松|紧缩",
    r"助贷|消费金融|小额贷款|互联网贷款|贷款新规",
    r"证监会|金融监管总局|银保监会|国务院|发改委|政治局",
    r"中央金融|中央经济|国常会|中国金龙|中概股",
]
ROUTINE_MARKET_RE = re.compile("|".join(ROUTINE_MARKET_PATTERNS), re.IGNORECASE)
NOTEWORTHY_MARKET_RE = re.compile("|".join(NOTEWORTHY_MARKET_PATTERNS),
                                  re.IGNORECASE)


def market_noise(text):
    """
    True when an item is routine market chatter that should never be stored.

    It is noise when it looks like price/market reporting AND carries no
    policy-grade subject. "Fed's Powell says further hikes possible" and
    "US August CPI rises 3.4%" survive (they have a subject that matters);
    "Nasdaq 100 futures extend gains to 1%" and "SK Hynix up 2% premarket"
    do not.
    """
    if not text:
        return False
    if not ROUTINE_MARKET_RE.search(text):
        return False
    return not NOTEWORTHY_MARKET_RE.search(text)


def purge_macro_noise(conn, dry_run=False):
    """
    Delete stored macro/global rows that the new quality gate would never keep,
    plus unanalyzed macro rows that were never going to be pushed.

    The macro tier used to store EVERY candidate the wires produced and only
    score the top few, so the browsable database filled with tape chatter
    ("US futures extend gains", "SK Hynix up 2% premarket") that showed up with
    no importance at all. This removes that backlog. Pushed rows are never
    touched - they are the record of what was actually delivered.

    Returns (deleted, scanned).
    """
    rows = conn.execute(
        "SELECT id, title_raw, title_en, snippet, pushed, importance FROM news "
        "WHERE ticker IN ('MACRO', 'GLOBAL')").fetchall()
    doomed = []
    for row in rows:
        # Positional access: this connection has no row factory.
        row_id, title_raw, title_en, snippet, pushed, importance = row[:6]
        if pushed:
            continue                       # never delete what was delivered
        hay = " ".join(str(x or "") for x in (title_raw, title_en, snippet))
        if market_noise(hay):
            doomed.append(row_id)
        elif importance is None:
            # Stored but never scored, and not delivered: the wire's leftovers.
            doomed.append(row_id)
    if dry_run:
        return len(doomed), len(rows)
    for chunk_start in range(0, len(doomed), 200):
        chunk = doomed[chunk_start:chunk_start + 200]
        conn.execute("DELETE FROM news WHERE id IN (%s)"
                     % ",".join("?" * len(chunk)), chunk)
    conn.commit()
    return len(doomed), len(rows)


def translate_titles(texts, config, secrets, chunk=25):
    """
    Translate a list of Chinese headlines to English in as few AI calls as
    possible: one JSON round-trip per `chunk` headlines (measured: 12 headlines
    in one ~17s call, so the cost is negligible).

    Returns a list of the same length, with the ORIGINAL string kept for any
    entry the model did not answer - never '' , so a failed translation can
    never blank out a title.
    """
    texts = list(texts or [])
    if not texts:
        return []
    key = secrets.get("ai_api_key", "")
    if not key:
        return texts
    base = config.get("ai_base_url") or DEFAULT_AI_BASE
    model = config.get("ai_model") or DEFAULT_AI_MODEL
    out = list(texts)
    for start in range(0, len(texts), chunk):
        batch = texts[start:start + chunk]
        items = [{"n": i, "title": t} for i, t in enumerate(batch, 1)]
        prompt = (
            "Translate each Chinese headline below into concise, natural "
            "English. Keep company names, tickers, numbers and percentages "
            "as they are. Do not add commentary. If a headline is already "
            "English, repeat it unchanged.\n"
            'Return ONLY a JSON array of objects with keys "n" and "en", one '
            "per headline, in the same order.\n\n"
            "HEADLINES:\n" + json.dumps(items, ensure_ascii=False)
        )
        content = _chat(base, model, key,
                        "You are a precise JSON-returning translator.", prompt)
        parsed = _parse_json_array(content) if content else None
        if not parsed:
            print(f"  [warn] translation batch {start//chunk + 1} failed; "
                  f"keeping the original titles.", file=sys.stderr)
            continue
        by_n = {}
        for obj in parsed:
            try:
                by_n[int(obj.get("n"))] = str(obj.get("en") or "").strip()
            except Exception:
                continue
        for i in range(len(batch)):
            got = by_n.get(i + 1, "")
            if got:
                out[start + i] = got
    return out


def translate_stored_news(conn, config, secrets, limit=200, pushed_only=False,
                          ticker=None, dry_run=False):
    """
    Backfill English titles for stored rows that never got one.

    Rows end up untranslated when they are trimmed out of a busy run before the
    AI stage (they used to be stranded permanently: the old trim left them
    marked 'seen', so no later run would re-fetch them). This gives those rows
    their English title so the panel is readable.

    Returns (translated, considered).
    """
    where = ["title_raw IS NOT NULL", "title_raw != ''",
             "(title_en IS NULL OR title_en = '' OR title_en = title_raw)"]
    params = []
    if pushed_only:
        where.append("pushed = 1")
    if ticker:
        where.append("ticker = ?")
        params.append(ticker.upper())
    rows = conn.execute(
        "SELECT id, title_raw FROM news WHERE " + " AND ".join(where) +
        " ORDER BY pushed DESC, id DESC LIMIT ?", params + [limit]).fetchall()
    if not rows:
        return 0, 0
    if dry_run:
        return 0, len(rows)
    titles = [r[1] for r in rows]
    translated = translate_titles(titles, config, secrets)
    n = 0
    for (row_id, raw), en in zip(rows, translated):
        if en and en != raw:
            conn.execute("UPDATE news SET title_en=? WHERE id=?", (en, row_id))
            n += 1
    conn.commit()
    return n, len(rows)


def analyze_macro(macro_items, config, secrets, conn, run_start, pseudo="MACRO"):
    """
    ONE tiny batched AI call for the macro items (translate + score + impact
    on the user's fintech names) - typically 0-3 items, so a few hundred
    tokens per run at most. When macro_translate is off (or no AI key), falls
    back to pushing the raw Chinese headline + an English tag: ZERO AI cost.
    """
    if not macro_items:
        return []
    key = secrets.get("ai_api_key", "")
    # The global-markets tier gets its own ⭐ scale: a systemic Fed/macro item
    # (8-10) versus an ordinary US session recap (2-4), so the cap keeps the
    # right ones.
    if pseudo == "GLOBAL":
        extra = (
            "IMPORTANCE SCALE (this is the GLOBAL MARKETS tier): 8-10 = a "
            "systemic event that reprices global risk and Chinese ADRs "
            "(a Fed rate decision/pivot, an inflation or payrolls surprise, a "
            "major tariff or sanctions action); 5-7 = notable macro data or "
            "index moves; 2-4 = a routine daily session recap (index closed "
            "down 0.3%, premarket futures fell). Score routine recaps LOW and "
            "set push=false for them - the user does not need a daily index "
            "recap.\n")
    else:
        extra = ""
    if config.get("macro_translate", True) and key:
        meta = {pseudo: {"name_zh": "中国宏观", "name_en": "China Macro",
                         "subsidiaries_zh": [], "subsidiaries_other": []}}
        if pseudo == "GLOBAL":
            meta = {pseudo: {"name_zh": "全球宏观", "name_en": "Global Markets",
                             "subsidiaries_zh": [], "subsidiaries_other": []}}
        analyzed = ai_analyze(macro_items, config, secrets, meta,
                              conn=conn, run_start=run_start, prompt_extra=extra)
        for it in analyzed:
            update_news_ai(conn, it)
        cap = _cfg_int(config, "macro_max_per_run", 3) if pseudo == "MACRO" \
            else _cfg_int(config, "global_markets_max_per_run", GLOBAL_MARKETS_MAX_PER_RUN)
        ranked = sorted(
            [it for it in analyzed
             if not it.get("is_dup") and not it.get("is_known") and it.get("push", True)],
            key=lambda it: it.get("importance") or 0, reverse=True)
        # The global tier additionally requires a real score: a recap the model
        # scored 3 must not occupy a seat.
        if pseudo == "GLOBAL":
            ranked = [it for it in ranked if (it.get("importance") or 0) >= 5]
        return ranked[:cap]
    # Zero-AI fallback: raw Chinese + English tag, always pushed (capped).
    for it in macro_items:
        it["title_en"] = it.get("title", "")
        it["impact"] = ""
        it["importance"] = 8 if pseudo == "MACRO" else 5
        it["push"] = True
    cap = _cfg_int(config, "macro_max_per_run", 3) if pseudo == "MACRO" \
        else _cfg_int(config, "global_markets_max_per_run", GLOBAL_MARKETS_MAX_PER_RUN)
    return macro_items[:cap]


def format_macro(items):
    """The 'China Macro' digest section - big China policy/market news, shown
    at the top of the digest, tagged with an English label."""
    if not items:
        return None
    lines = ["📢 CHINA MACRO — big policy/market news", ""]
    for it in items:
        title = it.get("title_en") or it.get("title", "")
        tag = macro_tag(f"{it.get('title', '')} {it.get('snippet', '')}")
        pub = (it.get("published_at")
               or _normalize_pub(it.get("date", "")) or "")
        lines.append(f"• [{tag}] {title}" + (f" 📅{pub[:10]}" if pub else ""))
        if it.get("impact"):
            lines.append(f"    → {it['impact']}")
        if it.get("url"):
            lines.append(f"    {it['url']}")
        lines.append("")
    return "\n".join(lines)


def collect_sector_watch(sector_report, conn, config, secrets, run_start,
                         meta_map=None):
    """
    Optional 🏭 SECTOR tier (config sector_watch, default OFF).

    Items that are about the company's INDUSTRY but never mention the company
    are stored as `_sector` during ingestion. When the tier is enabled they get
    one small batched AI call per ticker and the best few (capped globally) are
    shown in a separate, clearly-labelled section - so sector context is
    available without polluting the per-stock items.

    'meta_map' is the effective per-ticker profile (company_lookup + config
    overrides). It used to be passed as an EMPTY dict, so the prompt fell back
    to the bare ticker as the company name and had no site/subsidiaries - the
    sector items were scored with less context than the ticker items.
    """
    if not config.get("sector_watch", SECTOR_WATCH_DEFAULT):
        return []
    meta_map = meta_map or {}
    pool = []
    for ticker, items in (sector_report or {}).items():
        if not items:
            continue
        reviewed = ai_analyze(items, config, secrets,
                              {ticker: meta_map.get(ticker, {})}, conn=conn,
                              run_start=run_start, prompt_mode="sector",
                              prompt_extra=("IMPORTANCE SCALE (this is the "
                                            "SECTOR tier): 8-10 = a sector-wide "
                                            "change that directly alters this "
                                            "company's market (new regulation, "
                                            "a big industry shock); 5-7 = "
                                            "meaningful industry development; "
                                            "2-4 = generic industry commentary. "
                                            "Score generic commentary LOW and "
                                            "set push=false for it.\n"))
        for it in reviewed:
            update_news_ai(conn, it)
            if it.get("push", True) and (it.get("importance") or 0) >= 5 \
                    and not it.get("is_dup") and not it.get("is_known"):
                it["_sector"] = True
                pool.append(it)
    pool.sort(key=lambda it: it.get("importance") or 0, reverse=True)
    cap = max(1, _cfg_int(config, "sector_watch_max_per_run",
                          SECTOR_WATCH_MAX_PER_RUN))
    return pool[:cap]


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


def format_digest(filtered, ticker_count, stored_count=0, run_label=None):
    if not filtered:
        return None
    # Name the run so it is obvious WHICH digest this is: the 09:15 ET read
    # (pre-open) or the 17:00 ET read (one hour after the close).
    suffix = f" · {run_label}" if run_label and run_label != "manual" else ""
    lines = [f"📰 Portfolio News Digest ({ticker_count} ticker(s)){suffix}", ""]
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
        # Show the item's own publish date so anything stale is visible at a
        # glance (defense-in-depth against recycled old stories).
        pub = (item.get("published_at")
               or _normalize_pub(item.get("date", "")) or "")
        if pub:
            header += f" 📅{pub[:10]}"
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


def select_push_items(enriched, config, conn=None, now=None):
    """
    Decide which enriched items go to the Telegram digest.

    Gates, in order:
      1. drop same-batch duplicates (is_dup) and recycled events (is_known),
         and anything collapsed as a same-story copy from another source
         (`_superseded`) - all still stored for browsing;
      2. AGE GATE - an item published longer ago than push_max_age_hours is
         never pushed, whatever the AI scored it. This is the code-level
         version of "you already read this" (earnings results re-reported
         days later by another outlet). Regulatory items are exempt, because
         a penalty is news whenever it surfaces;
      3. REPEAT GATE - a candidate whose story was already pushed for this
         ticker inside the retention window is suppressed (`_repeat`), even
         if the AI did not notice;
      4. regulatory force-push: headlines matching penalty/regulatory keywords
         get boosted to >= 8 so they are never buried by generic scoring;
      5. importance floor (push_min_importance) + AI push veto;
      6. rank by importance (Chinese-language items tie-break higher), then
         allocate seats ROUND-ROBIN per ticker up to push_max_per_ticker, so
         one busy name can never take the whole digest while another name
         with real news gets nothing.

    Returns the pushed list (subset of enriched, in push order).
    """
    push_mode = config.get("push_mode", "all")  # "all" | "score"
    floor = _cfg_int(config, "push_min_importance", 4)
    min_score = _cfg_int(config, "push_min_score", 7)
    max_digest = _cfg_int(config, "max_digest_items", 10)
    max_per_ticker = max(1, _cfg_int(config, "push_max_per_ticker", 2))
    max_age = _cfg_int(config, "push_max_age_hours", PUSH_MAX_AGE_HOURS)
    event_window = _cfg_int(config, "event_repeat_window_days",
                            EVENT_REPEAT_WINDOW_DAYS)
    now = now or datetime.now(EASTERN)

    unique = [it for it in enriched
              if not it.get("is_dup") and not it.get("is_known")
              and not it.get("_superseded")]

    # Regulatory force-push: subsidiary penalties / regulatory action is the
    # core alpha - a code-level override so it is never buried by 1-10 scoring.
    # Computed BEFORE the age gate because regulatory items are exempt from it.
    reg_pattern = re.compile(
        r"(处罚|罚款|立案|约谈|调查|退市|监管|违规|delist|fraud|investigat|penalt|enforcement|regulat)",
        re.IGNORECASE)
    for it in unique:
        hay = f"{it.get('title', '')} {it.get('title_en', '')}"
        if reg_pattern.search(hay):
            it["_regulatory"] = True
            it["importance"] = max(it.get("importance") or 0, 8)
            if it.get("category") in (None, "", "other"):
                it["category"] = "regulatory"

    # (2) Age gate.
    if max_age > 0:
        fresh = []
        for it in unique:
            age = _item_age_hours(it)
            if age is not None and age > max_age and not it.get("_regulatory"):
                it["_too_old"] = True
                continue
            fresh.append(it)
        unique = fresh

    # (3) Repeat gate: has this STORY - or the corporate EVENT behind it -
    #     already been pushed for this ticker?
    if conn is not None:
        for it in unique:
            if it.get("_regulatory"):
                continue
            if _story_already_pushed(conn, it, now, event_window_days=event_window):
                it["_repeat"] = True
        unique = [it for it in unique if not it.get("_repeat")]

    # (5) Importance floor + AI veto: push=false is honored as a veto, and
    # nothing below the floor is pushed (kills low-score noise).
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

    # (6) Round-robin seat allocation. Within a ticker the order stays by
    # importance, but the digest alternates names so a single stock cannot
    # monopolise it.
    by_ticker = {}
    for it in candidates:
        by_ticker.setdefault(it.get("ticker", ""), []).append(it)
    order = sorted(by_ticker, key=lambda t: (-(by_ticker[t][0].get("importance") or 0), t))

    pushed = []
    counts = {}
    progressed = True
    while progressed and len(pushed) < max_digest:
        progressed = False
        for t in order:
            if len(pushed) >= max_digest:
                break
            if counts.get(t, 0) >= max_per_ticker:
                continue
            pool = by_ticker[t]
            while pool and pool[0] in pushed:
                pool.pop(0)
            if not pool:
                continue
            pushed.append(pool.pop(0))
            counts[t] = counts.get(t, 0) + 1
            progressed = True
    return pushed


def push_decision_counts(enriched):
    """Per-reason counters for the 'why wasn't this pushed' run log."""
    counts = {"too_old": 0, "repeat": 0, "event_repeat": 0, "same_story": 0,
              "dup": 0, "known": 0, "vetoed": 0, "below_floor": 0}
    for it in enriched:
        if it.get("_too_old"):
            counts["too_old"] += 1
        elif it.get("_repeat"):
            counts["event_repeat" if it.get("_event_repeat") else "repeat"] += 1
        elif it.get("_superseded"):
            counts["same_story"] += 1
        elif it.get("is_dup"):
            counts["dup"] += 1
        elif it.get("is_known"):
            counts["known"] += 1
        elif not it.get("push", True):
            counts["vetoed"] += 1
    return {k: v for k, v in counts.items() if v}


# ---------------------------------------------------------------------------
# Schedule guard
# ---------------------------------------------------------------------------
# Two runs a day, pinned to US market time. Both are deliberately placed
# OUTSIDE DeepSeek's peak-pricing windows (01:00-04:00 / 06:00-10:00 UTC
# Mon-Fri, when API tokens cost DOUBLE). Verified for both DST seasons:
#
#   Run 1  09:15 ET (15 min before the 9:30 open) -> 13:15 UTC (EDT, summer)
#                                                   14:15 UTC (EST, winter)
#   Run 2  17:00 ET (one hour after the 16:00 close) -> 21:00 UTC (EDT)
#                                                      22:00 UTC (EST)
#
# All four land off-peak, so every AI call is billed at half price. cron
# fires at fixed UTC times and installs BOTH seasons' jobs (see setup_cloud.sh);
# the guard below makes the out-of-season job an instant no-op, so exactly two
# real runs happen per day. The old third run at 23:00 ET (= 03:00/04:00 UTC,
# deep inside the peak window) was removed purely for AI cost.
SCHEDULE_RUN_TIMES = (dtime(9, 15), dtime(17, 0))
SCHEDULE_TOLERANCE_MIN = 5

# Label shown in the digest header so it is obvious WHICH run delivered it:
# the 09:15 ET run is the pre-open read, the 17:00 ET run is the post-close
# read (one hour after the 16:00 ET close).
SCHEDULE_LABELS = {
    dtime(9, 15): "pre-open",
    dtime(17, 0): "post-close",
}


def _run_label(now_et):
    """'pre-open' / 'post-close' for a scheduled run time, else 'manual'."""
    for target, label in SCHEDULE_LABELS.items():
        delta = abs((datetime.combine(now_et.date(), now_et.time())
                     - datetime.combine(now_et.date(), target)).total_seconds())
        if delta <= SCHEDULE_TOLERANCE_MIN * 60:
            return label
    return "manual"


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
          f"Outside scheduled times (9:15 / 17:00 ET) - skipping.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Parse --no-write FIRST so every mode below (including --rediscover)
    # respects it: no DB/lookup/history writes, no Telegram alerts, no
    # Tavily credit usage.
    #
    # --dry-run IMPLIES --no-write. They used to be independent, and that was a
    # trap: a "dry run" suppressed only the Telegram message, while every
    # database write is gated on NO_WRITE. So `--force --dry-run` (which the
    # installer ran on every deploy) marked the items it WOULD have sent as
    # pushed=1, wrote them into the repeat/event ledger and spent API credits -
    # without sending anything, permanently swallowing that window of news.
    # "Dry" must mean "changes nothing", so it now does.
    global NO_WRITE
    NO_WRITE = ("--no-write" in sys.argv) or ("--dry-run" in sys.argv)

    # ---- CLI modes first (never blocked by the schedule guard) ----
    # Match the PREFIX for modes that can carry a value (--purge-junk=3,
    # --rediscover=LU): an exact `"--mode" in sys.argv` test is False for
    # "--mode=value", which silently ran the normal pipeline instead.
    if any(a == "--purge-junk" or a.startswith("--purge-junk=") for a in sys.argv):
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

    if any(a == "--rediscover" or a.startswith("--rediscover=") for a in sys.argv):
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

    #   --dump-effective-meta   what the updater ACTUALLY searches with, per
    #                           ticker (config merged over the discovered
    #                           lookup, plus the ranked term lists). The panel
    #                           uses this to seed the names editor, so the user
    #                           sees the real picture instead of only their own
    #                           config.
    if "--dump-effective-meta" in sys.argv:
        config = load_config()
        out = {}
        for t in [str(x).strip().upper() for x in config.get("tickers", []) if str(x).strip()]:
            try:
                meta = ensure_company_meta(t, config, load_secrets())
            except Exception as exc:
                out[t] = {"error": str(exc)}
                continue
            entry = {
                "name_zh": meta.get("name_zh") or "",
                "name_en": meta.get("name_en") or "",
                "aliases_zh": list(meta.get("aliases_zh") or []),
                "subsidiaries_zh": list(meta.get("subsidiaries_zh") or []),
                "subsidiaries_other": list(meta.get("subsidiaries_other") or []),
                "keywords": list(meta.get("keywords") or []),
                "website": meta.get("website") or "",
                # The terms actually used for searching, in ranked order.
                "search_terms_zh": build_zh_terms(meta),
                "search_terms_en": build_en_terms(meta),
                # Which of those came from the user's config (editable) vs the
                # auto-discovery (informational).
                "from_config": sorted((config.get("ticker_meta") or {}).get(t, {}).keys()),
            }
            out[t] = entry
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)

    #   --purge-macro-noise      delete stored macro chatter + unscored leftovers
    #   --purge-macro-noise-dry  report what it would delete, change nothing
    # NOTE: do NOT combine this with --no-write: that swaps the session onto an
    # in-memory database (by design), so there is nothing to scan or delete.
    if "--purge-macro-noise" in sys.argv or "--purge-macro-noise-dry" in sys.argv:
        dry = "--purge-macro-noise-dry" in sys.argv
        conn = _db()
        deleted, scanned = purge_macro_noise(conn, dry_run=dry)
        conn.close()
        print(json.dumps({"ok": True, "deleted": deleted, "scanned": scanned,
                          "dry_run": dry}, ensure_ascii=False))
        sys.exit(0)

    # ---- translate stored headlines (panel button / backfill) ----
    #   --translate              translate untranslated stored rows (up to 200)
    #   --translate=50           ... a specific number of rows
    #   --translate-pushed       only rows that were pushed to Telegram
    #   --translate=LX           ... only one ticker (implies pushed_only=False)
    #   --translate-texts=FILE   translate the strings in a JSON file and print
    #                            them (used by the panel's per-row EN button;
    #                            reads/writes no database at all)
    if any(a.startswith("--translate-texts=") for a in sys.argv):
        path = next(a.split("=", 1)[1] for a in sys.argv
                    if a.startswith("--translate-texts="))
        config = load_config()
        secrets = load_secrets()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                texts = json.load(fh).get("texts") or []
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"could not read {path}: {exc}"}))
            sys.exit(1)
        translations = translate_titles([str(t) for t in texts], config, secrets)
        print(json.dumps({"ok": True, "translations": translations},
                         ensure_ascii=False))
        sys.exit(0)

    if any(a == "--translate" or a.startswith("--translate") for a in sys.argv):
        limit, ticker_arg, pushed_only = 200, None, False
        for a in sys.argv:
            if a.startswith("--translate="):
                v = a.split("=", 1)[1].strip()
                if v.isdigit():
                    limit = max(1, int(v))
                elif v:
                    ticker_arg = v.upper()
            elif a == "--translate-pushed":
                pushed_only = True
        config = load_config()
        secrets = load_secrets()
        conn = _db()
        n, considered = translate_stored_news(
            conn, config, secrets, limit=limit, pushed_only=pushed_only,
            ticker=ticker_arg, dry_run=NO_WRITE)
        conn.close()
        print(json.dumps({"ok": True, "translated": n, "considered": considered,
                          "pushed_only": pushed_only, "ticker": ticker_arg},
                         ensure_ascii=False))
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

    # ---- delete stored news (panel's per-row ✕ button) ----
    #   --delete-news=TICKER|SOURCE|ITEM_HASH          delete one stored row
    #   --delete-news-pushed=TICKER|SOURCE|ITEM_HASH   ... and forget it was
    #                                                  ever pushed
    # The `-pushed` form also clears the repeat/event ledger entry, so a story
    # the user deliberately removed can reach them again if it is re-reported.
    # The plain form keeps the dedup memory (delete from the browser view only).
    # NOTE: these modes are always passed WITH a value (--delete-news=A|B|C), so
    # match the prefix, not the bare flag: `"--delete-news" in sys.argv` is
    # False for `--delete-news=...` and the mode would silently fall through to
    # the schedule guard.
    if any(a == "--delete-news" or a.startswith("--delete-news=")
           or a.startswith("--delete-news-pushed=") for a in sys.argv):
        arg, keep_ledger = "", True
        for a in sys.argv:
            if a.startswith("--delete-news-pushed="):
                arg, keep_ledger = a.split("=", 1)[1], False
            elif a.startswith("--delete-news="):
                arg = a.split("=", 1)[1]
        parts = (arg or "").split("|")
        if len(parts) != 3 or not parts[0].strip() or not parts[2].strip():
            print(json.dumps({"ok": False,
                              "error": "expected TICKER|SOURCE|ITEM_HASH"}))
            sys.exit(1)
        ticker, source, item_hash = (parts[0].strip().upper(), parts[1].strip(),
                                     parts[2].strip())
        if NO_WRITE:
            print(json.dumps({"ok": False, "error": "--no-write set"}))
            sys.exit(1)
        conn = _db()
        res = delete_news_item(conn, ticker, source=source, item_hash=item_hash,
                               keep_ledger=keep_ledger)
        conn.close()
        print(json.dumps({"ok": res["deleted"] > 0, "ticker": ticker,
                          "source": source, "deleted": res["deleted"],
                          "ledger_removed": res["ledger_removed"]},
                         ensure_ascii=False))
        sys.exit(0 if res["deleted"] else 1)

    dry_run = "--dry-run" in sys.argv
    snapshot = "--snapshot" in sys.argv  # manual run: always deliver (current picture)

    # One run at a time: cron and the panel's "Run now" can otherwise overlap,
    # which risks a duplicate Telegram digest. Taken AFTER the CLI modes (those
    # are quick and single-purpose) and before any fetching.
    lock_state, run_lock = acquire_run_lock()
    if lock_state == "busy":
        print("  Another news_updater run is already in progress - exiting so it "
              "is not disturbed (prevents a duplicate digest).")
        sys.exit(0)

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
    # Hard freshness backstop window (see MAX_NEWS_AGE_HOURS): any ingested
    # item whose publish time is older than this many hours is dropped no
    # matter what its source claimed. Configurable via config_local.json
    # ("max_news_age_hours"); 0 disables the backstop entirely.
    max_news_age_hours = _cfg_int(config, "max_news_age_hours", MAX_NEWS_AGE_HOURS)
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
    # Per-ticker sector_watch candidates + drop counters for the run log.
    sector_report = {}
    dropped_report = {}

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

        # ---- relevance + quality context for THIS ticker ----
        relevance_resolved = resolve_relevance(meta)
        site_domains = relevance_resolved["domains"]
        sector_watch = bool(config.get("sector_watch", SECTOR_WATCH_DEFAULT))
        _sector_items = []
        sector_report[ticker] = _sector_items
        ingest_report = {}
        dropped_report[ticker] = ingest_report

        def src_status(key, strict=True, note=""):
            """
            The relevance policy for one source. 'strict' means: an item from
            this source must really mention the company to be kept (sector
            news is dropped, or routed to sector_watch when that tier is on).
            Sources whose query is already company-specific are not strict.
            """
            return {"status": "strict" if strict else "warn", "note": note}

        def fetch_source(source, fetch_fn, status):
            """Fetch one source and fold it through the shared ingest gate."""
            last = get_last_fetched(conn, ticker, source)
            since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                        if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
            items = fetch_fn(since_dt)
            if items is None:
                print(f"  [warn] {ticker} {source} fetch failed - NOT advancing "
                      f"delta (will retry from {since_dt.strftime('%Y-%m-%d %H:%M')}).")
                return None
            for item in items:
                item["_source_status"] = status
            keeps = [it for it in items if status.get("status") != "drop"]
            accept = ingest_items(conn, all_new, source, keeps, ticker, since_dt,
                                  max_news_age_hours,
                                  relevance_resolved=relevance_resolved,
                                  sector_watch=sector_watch,
                                  sector_items=_sector_items,
                                  run_start=run_start, report=ingest_report)
            set_last_fetched(conn, ticker, source, now_utc_str)
            return accept

        # 1) SEC filings (English, ADR regulatory coverage). A filing IS about
        #    the company by definition - no relevance filter needed.
        if src_on("sec"):
            fetch_source("SEC", lambda sd: fetch_sec_filings(ticker, sd, conn=conn),
                         src_status("sec", strict=False))

        # 2) Chinese sources (the alpha) - require Chinese search terms.
        if zh_terms:
            # Google News, Chinese edition.
            if src_on("google_news_zh"):
                zh_query = " OR ".join(zh_terms)
                fetch_source(
                    "GoogleNewsZH",
                    lambda sd: fetch_rss(google_news_url(zh_query, "zh"), ticker, sd,
                                         source="GoogleNewsZH", lang="zh"),
                    src_status("google_news_zh"))

            # Eastmoney search - one query per term (capped), already
            # title-filtered by the fetcher.
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
                fetch_source("Eastmoney", _em, src_status("eastmoney", strict=False))

            # Baidu news (best-effort, one query using the main name).
            if src_on("baidu"):
                fetch_source("Baidu",
                             lambda sd: fetch_baidu_news(zh_terms[0], sd, terms=zh_terms),
                             src_status("baidu", strict=False))

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
                    fetch_source(
                        "Tavily",
                        lambda sd: fetch_tavily(tav_query, secrets, config, sd,
                                                terms=zh_terms + en_terms),
                        src_status("tavily", strict=False))

        # 2.5) Google News restricted to the company's OWN websites (site:)
        #      - catches official announcements / press releases that no news
        #      outlet picks up. Free, no Tavily credits.
        if site_domains and src_on("google_news_site"):
            site_query = " OR ".join(f"site:{d}" for d in site_domains)
            print(f"  {ticker}: official-site search -> {site_query}")
            fetch_source(
                "GoogleNewsSite",
                lambda sd: fetch_rss(google_news_url(site_query, "zh"), ticker, sd,
                                     source="GoogleNewsSite", lang="zh"),
                src_status("google_news_site", strict=False))

        # 3) Google News English - includes the company's EN names/brands so
        #    subsidiary news is found even when the ticker symbol isn't in it.
        if src_on("google_news_en"):
            en_query = " OR ".join([f"{ticker} stock"] + en_terms[:3])
            fetch_source(
                "GoogleNews",
                lambda sd: fetch_rss(google_news_url(en_query, "en"), ticker, sd,
                                     source="GoogleNews", lang="en"),
                src_status("google_news_en"))

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

            def _wire(sd, raw=raw, src=wire_src, terms=zh_terms):
                """Filter a global wire by this ticker's Chinese terms.

                Macro-matched items belong to the 📢 CHINA MACRO section, and
                an item must really name the company (or a subsidiary) - the
                wire is a firehose of everything, so this filter is what makes
                it per-ticker."""
                out = []
                for r in raw:
                    hay = f"{r.get('title', '')} {r.get('snippet', '')}"
                    # Macro / global-markets items have their own sections -
                    # keep them out of the per-ticker digest.
                    if is_macro(hay, macro_extra) or is_global_markets(hay):
                        continue
                    if not any(_term_in_text(t, hay) for t in terms):
                        continue
                    it = dict(r)
                    it["source"] = src
                    out.append(it)
                return out

            # The wire query is the company's/subsidiaries' exact names, so
            # strict=True would double-filter and lose the semantic
            # match: the term filter above decides relevance.
            fetch_source(wire_src, _wire, src_status(wire_key, strict=False))

        # 3.6) EXA neural search - semantic recall: finds pages ABOUT the
        #      company that keyword search misses ("the Shenzhen-based insurer"
        #      instead of "Huize").
        #
        #      WHY THIS IS OFF BY DEFAULT NOW. EXA is the most expensive source
        #      per useful item and was the least productive:
        #        - an 8-result neural search is largely discarded before it can
        #          even be filtered (the log showed 5-8 of 8 dropped as
        #          stale/undated every ticker, every run);
        #        - the old query ended in the same sector words as the macro
        #          query (重大 监管 政策 影响 风险), so it returned sector
        #          articles that the relevance gate then rejected: 610 of 616
        #          stored Exa rows never mentioned the company.
        #      A paid credit per ticker per run bought ~0 usable items, so the
        #      per-ticker search is disabled unless you turn it back on with
        #      `exa_min_free_items` set high enough to matter. The EXA budget is
        #      better spent on the MACRO tier, where a semantic query genuinely
        #      finds differently-worded big news.
        if src_on("exa") and secrets.get("exa_api_key") \
                and config.get("exa_per_ticker", EXA_PER_TICKER_DEFAULT):
            free_count = sum(1 for it in all_new if it.get("ticker") == ticker)
            exa_min_free = _cfg_int(config, "exa_min_free_items", EXA_MIN_FREE_ITEMS)
            if free_count >= exa_min_free:
                print(f"  {ticker}: free sources already found {free_count} item(s) "
                      f"this run - skipping EXA (saving credits).")
            else:
                # Company-name-only neural query: no sector words, so the
                # results have a chance of surviving the relevance gate.
                exa_query = " ".join(zh_terms[:2] + en_terms[:1])
                fetch_source(
                    "Exa",
                    lambda sd: fetch_exa(exa_query, secrets, config, sd,
                                         limit=5, category="news"),
                    src_status("exa"))

        # 4) Company RSS feeds (configured per ticker) - source "RSS".
        feeds = config.get("rss_feeds", {}).get(ticker, [])
        if feeds:
            last = get_last_fetched(conn, ticker, "RSS")
            since_dt = (datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
                        if last else datetime.now(EASTERN) - timedelta(hours=initial_hours))
            rss_ok = True
            rss_items = []
            for feed_url in feeds:
                feed_items = fetch_rss(feed_url, ticker, since_dt, source="RSS", lang="en")
                if feed_items is None:
                    rss_ok = False
                    print(f"  [warn] {ticker} RSS {feed_url} failed - NOT advancing delta.")
                    continue
                rss_items.extend(feed_items)
            if rss_items:
                for item in rss_items:
                    item["_source_status"] = {"status": "warn", "note": "own feed"}
                ingest_items(conn, all_new, "RSS", rss_items, ticker, since_dt,
                             max_news_age_hours, relevance_resolved=relevance_resolved,
                             sector_watch=sector_watch, sector_items=_sector_items,
                             run_start=run_start, report=ingest_report)
            if rss_ok:
                set_last_fetched(conn, ticker, "RSS", now_utc_str)

        # Visibility: the relevance gate is the difference between a useful
        # digest and a firehose, so log exactly what it removed and why.
        kept_here = sum(1 for it in all_new if it.get("ticker") == ticker)
        if ingest_report:
            why = ", ".join(f"{k}={v}" for k, v in
                            sorted(ingest_report.items(), key=lambda kv: -kv[1]))
            print(f"  {ticker}: {kept_here} kept, {sum(ingest_report.values())} "
                  f"filtered out ({why}).")
        if _sector_items:
            print(f"  {ticker}: {len(_sector_items)} sector-watch item(s) "
                  f"(sector_watch is ON - separately capped).")
        if not site_domains:
            print(f"  {ticker}: no website in the lookup - official-site (site:) "
                  f"search skipped. Run --rediscover={ticker} to try to find it.")

    record["new_items"] = len(all_new)
    print(f"  {len(all_new)} new item(s) found (all stored in the news DB).")

    # ---- China macro + global markets: the "I HAVE TO KNOW" tiers ----
    # Huge China policy news (rate cuts, stimulus, assisted-loan regulation)
    # gated FREE by regex; matched items reach one tiny batched AI call. The
    # gate now requires a strong China-policy pattern (or two distinct hits),
    # so US Fed/payrolls headlines are routed to the separate GLOBAL MARKETS
    # tier instead of arriving labelled as "China macro".
    macro_items = collect_macro_items(conn, config, secrets, wire_cache, src_on,
                                      initial_hours, run_start, now_utc_str,
                                      max_age_hours=max_news_age_hours)
    for it in macro_items:
        it["_tier"] = news_tier(f"{it.get('title', '')} {it.get('snippet', '')}")
    macro_core = [it for it in macro_items if it.get("_tier") == "macro"]
    # Global items are stored under the "GLOBAL" pseudo-ticker.
    global_items = []
    for it in macro_items:
        if it.get("_tier") == "global":
            g = dict(it)
            g["ticker"] = "GLOBAL"
            global_items.append(g)

    macro_pushed = analyze_macro(macro_core, config, secrets, conn, run_start,
                                 pseudo="MACRO") if macro_core else []
    for it in macro_core:
        mark_pushed(conn, it, it in macro_pushed)
    if macro_core:
        print(f"  {len(macro_core)} China macro item(s) -> {len(macro_pushed)} "
              f"pushed (the 'must know' tier).")

    global_pushed = []
    if global_items and config.get("global_markets", GLOBAL_MARKETS_DEFAULT):
        global_pushed = analyze_macro(global_items, config, secrets, conn,
                                      run_start, pseudo="GLOBAL")
        for it in global_items:
            mark_pushed(conn, it, it in global_pushed)
        print(f"  {len(global_items)} global-markets item(s) -> "
              f"{len(global_pushed)} pushed.")
    elif global_items:
        for it in global_items:
            mark_pushed(conn, it, False)
        print(f"  {len(global_items)} global-markets item(s) stored, not pushed "
              f"(global_markets is OFF - enable it in the panel if you want a "
              f"GLOBAL MARKETS section).")

    macro_digest = format_macro(macro_pushed)
    global_digest = format_global_markets(global_pushed)

    # ---- Optional 🏭 SECTOR CONTEXT tier (sector_watch, default OFF) ----
    sector_pushed = collect_sector_watch(sector_report, conn, config, secrets,
                                         run_start, meta_map=effective_meta) \
        if any(sector_report.values()) else []
    for items in (sector_report or {}).values():
        for it in items:
            mark_pushed(conn, it, it in sector_pushed)
    if sector_pushed:
        print(f"  {len(sector_pushed)} sector-context item(s) pushed "
              f"(sector_watch is ON).")
    sector_digest = format_sector_watch(sector_pushed)

    # ---- collapse the same story arriving from several sources ----
    # The per-source hash cannot see across sources, so one earnings release
    # used to enter the pipeline 4-6 times (GoogleNewsZH + GoogleNews + Tavily
    # + Exa) and fill the digest with copies of one event. Only the best
    # sourced copy competes for a digest seat; the others are still analysed
    # and stored for browsing, but can never be pushed.
    all_new, collapsed = dedupe_same_story(all_new)
    if collapsed:
        print(f"  [dedup] collapsed {collapsed} same-story duplicate(s) from "
              f"other sources (kept the best-sourced copy of each).")

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

    # Trim AFTER dedup so far fewer items are lost to the cap, and release the
    # seen-mark of anything still trimmed: the old code left those rows in the
    # DB and in the `seen` ledger with importance NULL forever, so no later run
    # could ever analyze them (they were "already seen"). Released items are
    # re-fetched by a following run and are re-queued by rescue_orphans.
    max_to_filter = _cfg_int(config, "max_items_per_run", 40)
    if len(all_new) > max_to_filter:
        trimmed = all_new[max_to_filter:]
        all_new = all_new[:max_to_filter]
        released = 0
        for it in trimmed:
            if NO_WRITE:
                continue
            try:
                conn.execute("DELETE FROM seen WHERE ticker=? AND source=? AND item_hash=?",
                             (it.get("ticker"), it.get("source"), _hash_of(it)))
                released += 1
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass
        print(f"  Trimming to the {max_to_filter} most recent for AI analysis "
              f"({len(trimmed)} deferred, {released} re-queued for the next run).")

    # Any stored row that never reached the AI has no English title, which makes
    # the panel unreadable for a Chinese-only reader (this is exactly what left
    # 30 Chinese rows in the browser view). Translate them in one batched call -
    # cheap (a dozen headlines per call) and it means every stored item is always
    # readable, whether or not it made the digest.
    if not NO_WRITE:
        try:
            n_tr, n_seen_rows = translate_stored_news(conn, config, secrets,
                                                      limit=60)
            if n_tr:
                print(f"  [translate] filled in English titles for {n_tr} "
                      f"stored item(s) that had none.")
        except Exception as exc:
            print(f"  [warn] translating stored titles failed: {exc}", file=sys.stderr)

    if not all_new and not macro_pushed and not global_pushed and not sector_pushed:
        print("  No new items - nothing to do.")
        extra_digests = []
        if macro_digest:
            extra_digests.append(("CHINA MACRO", macro_digest))
        if global_digest:
            extra_digests.append(("GLOBAL MARKETS", global_digest))
        if sector_digest:
            extra_digests.append(("SECTOR CONTEXT", sector_digest))
        if extra_digests:
            if dry_run:
                for name, msg in extra_digests:
                    print("\n" + "=" * 60)
                    print(f"DRY RUN - {name}:")
                    print("=" * 60)
                    print(msg)
                    print("=" * 60)
            elif token and chat_id:
                for name, msg in extra_digests:
                    if not send_telegram(token, chat_id, msg):
                        record["alerts_failed"] = \
                            record.get("alerts_failed", []) + [name.lower()]
                    else:
                        print(f"  {name} digest sent.")
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
        pushed = select_push_items(enriched, config, conn=conn, now=start_time)
        for it in enriched:
            update_news_ai(conn, it)
        for it in enriched:
            mark_pushed(conn, it, it in pushed)
        # Remember what actually went out - the repeat gate reads this next run.
        record_pushed_stories(conn, pushed, now=start_time)
        record["sent_items"] = len(pushed)
        floor = _cfg_int(config, "push_min_importance", 4)
        max_digest = _cfg_int(config, "max_digest_items", 10)
        max_per_ticker = max(1, _cfg_int(config, "push_max_per_ticker", 2))
        print(f"  Pushing {len(pushed)} item(s) to Telegram "
              f"(importance >= {floor}, cap {max_digest}, max {max_per_ticker}/ticker). "
              f"{len(enriched) - len(pushed)} item(s) stored for browsing.")
        reasons = push_decision_counts(enriched)
        if reasons:
            print("  Not pushed: " + ", ".join(f"{k}={v}" for k, v in
                                               sorted(reasons.items(),
                                                      key=lambda kv: -kv[1])) + ".")

    digest = format_digest(pushed, len(tickers),
                           stored_count=len(all_new) - len(pushed),
                           run_label=_run_label(start_time)) if pushed else None
    # Every section that has content: macro -> global markets -> the per-stock
    # digest -> optional sector context (so the stock items stay the headline).
    digests = []
    if macro_digest:
        digests.append(("CHINA MACRO", macro_digest))
    if global_digest:
        digests.append(("GLOBAL MARKETS", global_digest))
    if digest:
        digests.append(("DIGEST", digest))
    if sector_digest:
        digests.append(("SECTOR CONTEXT", sector_digest))

    if digests:
        if dry_run:
            for name, msg in digests:
                print("\n" + "=" * 60)
                print(f"DRY RUN - {name} (would be sent to Telegram):")
                print("=" * 60)
                print(msg)
                print("=" * 60)
        elif token and chat_id:
            sent_any = False
            for name, msg in digests:
                if send_telegram(token, chat_id, msg):
                    sent_any = True
                else:
                    record["alerts_failed"] = \
                        record.get("alerts_failed", []) + [name.lower()]
            if sent_any:
                print(f"  Digest sent (macro {len(macro_pushed)} + global "
                      f"{len(global_pushed)} + regular {len(pushed)} + sector "
                      f"{len(sector_pushed)} item(s)).")
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
