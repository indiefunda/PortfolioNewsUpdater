#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression gate for the "can I swap the whole watchlist?" fixes.

Covers, in order:
  * config files that carry a UTF-8 BOM (and any invalid JSON) are read, and the
    failure is LOUD instead of looking like "no config"
  * English short names ("Toyota" from Toyota Motor Corporation) match, and the
    generic words they must NOT produce ("china", "general", "fintech")
  * explicit aliases_en work for names that cannot be derived (Alphabet->Google)
  * the names editor can REMOVE an auto-discovered name (authoritative)
  * stale ticker_meta/lookup entries from a previous watchlist are ignored
  * no AI key fails CLOSED (stored, never pushed) for tickers and macro
  * US auto-generated filler titles are junk; a real analyst action is not
  * the panel's Origin check refuses `null`, foreign origins and wrong ports
  * news_web publishes the email allowlist (lowercased) for firestore.rules
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail)) if detail else "")
    if not cond:
        fails.append(name)


def verdict(ticker, title, meta):
    return nu.passes_relevance({"ticker": ticker, "title": title, "snippet": ""},
                               meta, nu.resolve_relevance(meta))


spec = importlib.util.spec_from_file_location(
    "nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

spec2 = importlib.util.spec_from_file_location(
    "cm", os.path.join(ROOT, "cloud_manager.py"))
cm = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(cm)

spec3 = importlib.util.spec_from_file_location(
    "nw", os.path.join(ROOT, "news_web.py"))
nw = importlib.util.module_from_spec(spec3)
spec3.loader.exec_module(nw)

TMP = tempfile.mkdtemp(prefix="swap_ready_")


def write(path, text, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)


print("=== config I/O: a BOM (PowerShell / VS Code 'UTF-8 with BOM') must not erase the config ===")
bom_cfg = os.path.join(TMP, "bom.json")
with open(bom_cfg, "w", encoding="utf-8-sig", newline="\n") as fh:
    fh.write('{"tickers": ["AAPL"], "enabled": true}')
nu._READ_ERRORS.clear()
got = nu._read_json(bom_cfg, {})
check("updater reads a BOM config", got.get("tickers") == ["AAPL"], got)
check("no false parse error recorded", bom_cfg not in nu._READ_ERRORS)

broken = os.path.join(TMP, "broken.json")
write(broken, '{"tickers": [}')
default = {"sentinel": True}
check("invalid JSON falls back to the default", nu._read_json(broken, default) is default)
check("invalid JSON is RECORDED, not swallowed", broken in nu._READ_ERRORS,
      nu._READ_ERRORS.get(broken))

cm._READ_ERRORS.clear()
check("panel reads a BOM config", cm._read_json(bom_cfg, {}).get("tickers") == ["AAPL"])
cm._read_json(broken, {})
check("panel records the parse error too", broken in cm._READ_ERRORS)

print()
print("=== English short names: official legal names must match headline forms ===")
SHORT = [
    ("TM", "Toyota Motor Corporation", "Toyota posts record quarterly profit"),
    ("F", "Ford Motor Company", "Ford recalls 500,000 SUVs over fire risk"),
    ("F", "Ford Motor Company", "Ford's EV unit loses $1bn"),
    ("MC.PA", "LVMH Moet Hennessy Louis Vuitton SE", "LVMH sales slide on weaker China demand"),
    ("AMZN", "Amazon.com, Inc.", "Amazon to cut 30,000 corporate jobs"),
    ("DIS", "The Walt Disney Company", "Disney posts streaming profit"),
    ("XOM", "Exxon Mobil Corporation", "Exxon profit falls on weaker refining"),
    ("GOOGL", "Alphabet Inc.", "Alphabet beats revenue estimates"),
    ("META", "Meta Platforms, Inc.", "Meta unveils new AI glasses"),
    ("GM", "General Motors Company", "General Motors raises guidance"),
]
for tk, name, title in SHORT:
    v = verdict(tk, title, {"ticker": tk, "name_en": name, "name_zh": ""})
    check(f"{tk}: {title[:38]}", v == "company", v)

print()
print("=== derived short names must not become generic noise ===")
for name, banned in (
        ("China Automotive Systems", "china"),
        ("Up Fintech Holding", "fintech"),
        ("General Motors Company", "general"),
        ("JPMorgan Chase & Co.", "chase"),
        ("Bank of America Corp", "america"),
        ("First Solar, Inc.", "solar")):
    variants = {v.lower() for v in nu._latin_name_variants(name)}
    check(f"{name}: no '{banned}' alias", banned not in variants, sorted(variants))

print()
print("=== aliases_en: the escape hatch for names that cannot be derived ===")
google = {"ticker": "GOOGL", "name_en": "Alphabet Inc.", "name_zh": "",
          "aliases_en": ["Google"]}
check("alias makes 'Google faces antitrust ruling' company news",
      verdict("GOOGL", "Google faces antitrust ruling in Europe", google) == "company")
check("alias is used as an English search term",
      any("Google" in t for t in nu.build_en_terms(google)),
      nu.build_en_terms(google))
check("short forms are searched too",
      "Ford" in nu.build_en_terms({"name_en": "Ford Motor Company"}),
      nu.build_en_terms({"name_en": "Ford Motor Company"}))

print()
print("=== prose collisions still rejected (Visa / Delta are also words) ===")
check("'Visa rules for tourists' is not company news",
      verdict("V", "Visa rules for tourists change in 2027",
              {"ticker": "V", "name_en": "Visa Inc.", "name_zh": ""}) != "company")
check("'Visa expands stablecoin settlement' is company news",
      verdict("V", "Visa expands stablecoin settlement",
              {"ticker": "V", "name_en": "Visa Inc.", "name_zh": ""}) == "company")

print()
print("=== the names editor can REMOVE a discovered name (authoritative) ===")
lookup_file = os.path.join(TMP, "company_lookup.json")
write(lookup_file, json.dumps({
    "AAPL": {"name_zh": "", "name_en": "Apple Inc.",
             "aliases_en": ["Apple"], "subsidiaries_other": ["Beats"],
             "last_updated": "2099-01-01", "lookup_attempted": True}}))
old_lookup = nu.COMPANY_LOOKUP_FILE
nu.COMPANY_LOOKUP_FILE = lookup_file
base_cfg = {"tickers": ["AAPL"], "ticker_meta": {}}
plain = nu.ensure_company_meta("AAPL", base_cfg, {}, persist=False,
                               allow_discovery=False)
check("without authoritative, discovered names stay", "Beats" in plain.get("subsidiaries_other", []),
      plain.get("subsidiaries_other"))
auth_cfg = {"tickers": ["AAPL"], "ticker_meta": {"AAPL": {
    "authoritative": True, "aliases_en": ["Apple"], "subsidiaries_other": []}}}
slim = nu.ensure_company_meta("AAPL", auth_cfg, {}, persist=False,
                              allow_discovery=False)
check("authoritative REPLACES the list (removal works)",
      slim.get("subsidiaries_other") == [] and slim.get("aliases_en") == ["Apple"],
      {"subs": slim.get("subsidiaries_other"), "aliases_en": slim.get("aliases_en")})
nu.COMPANY_LOOKUP_FILE = old_lookup

print()
print("=== stale watchlist entries are ignored after a swap ===")
lookup = {}
cfg = {"tickers": ["AAPL"], "ticker_meta": {
    "AAPL": {"name_en": "Apple Inc."},
    "OLDTICK": {"name_zh": "旧", "subsidiaries_zh": ["旧品牌"]}}}
nu.seed_lookup_from_config(cfg, lookup)
check("only configured tickers are seeded", "OLDTICK" not in lookup, sorted(lookup))

prune_lookup = {
    "OLDTICK": {"name_en": "Old Company", "keywords": ["Tencent"]},
    "0700.HK": {"name_en": "Tencent Holdings", "keywords": ["Tencent"]},
}
ambiguous = nu.prune_cross_ticker_keywords(prune_lookup, active=["0700.HK"])
check("a removed ticker cannot claim a live ticker's brand",
      "Tencent" in prune_lookup["0700.HK"]["keywords"], ambiguous)

print()
print("=== no AI key fails CLOSED (stored, never pushed) ===")
items = [{"ticker": "AAPL", "title": "Apple unveils new iPhone", "snippet": ""}]
nu.ai_analyze(items, {"ai_model": "x"}, {}, {"AAPL": {}})
check("ticker item is not pushable without a key",
      items[0]["push"] is False and items[0]["importance"] == 1,
      {"push": items[0]["push"], "importance": items[0]["importance"]})

macro_items = [{"ticker": "MACRO", "source": "Eastmoney724", "id": "m1",
                "title": "央行宣布降准0.5个百分点", "snippet": ""}]
# Isolate the fail-closed decision from the keyword gate: pretend the strict
# screen admitted this item, then check that NOTHING is pushed without a key.
_old_wants = nu.wants_macro_tier
_old_noise = nu.market_noise
nu.wants_macro_tier = lambda text, extra=None: True
nu.market_noise = lambda text: False
try:
    out = nu.analyze_macro(macro_items, {"macro_translate": True}, {}, None,
                           "2026-01-01 09:00:00", pseudo="MACRO")
finally:
    nu.wants_macro_tier = _old_wants
    nu.market_noise = _old_noise
check("macro tier pushes nothing without a key", out == [], len(out))
check("but the macro item is still stored-only",
      macro_items[0]["push"] is False and macro_items[0]["importance"] == 1,
      {"push": macro_items[0]["push"], "importance": macro_items[0]["importance"]})

print()
print("=== US auto-generated filler is junk; a real analyst action is not ===")
JUNK = [
    "Berkshire Hathaway B (BRK.B) Gains As Market Dips: What You Should Know",
    "25,685 Alibaba Group Holding Limited Shares Sold by Sustainable Growth Advisers",
    "Alibaba Group Stock Price Down 1.9% - Here's Why",
    "Should you buy Apple stock now?",
]
for title in JUNK:
    check(f"junk: {title[:44]}", nu.is_junk_content(title), title)
check("real analyst action survives",
      not nu.is_junk_content("Morgan Stanley cuts QFIN price target to $8"))

print()
print("=== panel Origin check: null / foreign / wrong port are refused ===")


class OriginStub:
    def __init__(self, host, origin=None, referer=None, port=8001):
        self.headers = {"Host": host}
        if origin is not None:
            self.headers["Origin"] = origin
        if referer is not None:
            self.headers["Referer"] = referer
        self.server = SimpleNamespace(server_port=port)
        self.sent = None

    def _send_json(self, obj, status=200):
        self.sent = (obj, status)


def origin_ok(**kw):
    stub = OriginStub(**kw)
    return cm.Handler._origin_ok(stub), stub


ok, _ = origin_ok(host="127.0.0.1:8001", origin="http://localhost:8001")
check("same-origin is allowed", ok)
ok, stub = origin_ok(host="127.0.0.1:8001", origin="null")
check("Origin: null is refused", not ok and stub.sent[1] == 403, stub.sent)
ok, _ = origin_ok(host="127.0.0.1:8001", origin="https://evil.example")
check("a foreign origin is refused", not ok)
ok, _ = origin_ok(host="127.0.0.1:8001", origin="http://127.0.0.1:9999")
check("a loopback origin on the WRONG port is refused", not ok)
ok, _ = origin_ok(host="evil.example:8001")
check("a non-loopback Host is refused", not ok)
ok, _ = origin_ok(host="localhost:8001", referer="http://localhost:8001/index.html")
check("same-origin Referer is allowed", ok)
ok, _ = origin_ok(host="localhost:8001")
check("absent Origin (non-browser client / older same-origin POST) is allowed", ok)

print()
print("=== hosted-page allowlist is parsed, lowercased and fail-closed ===")
web_db = os.path.join(TMP, "news.db")
write(os.path.join(TMP, "config_local.json"),
      json.dumps({"tickers": ["AAPL"], "web_allowed_emails": ["Me@Gmail.com", " other@x.com "]}))

emails = nw.allowed_emails(web_db)
check("emails lowercased and trimmed", emails == ["me@gmail.com", "other@x.com"], emails)
write(os.path.join(TMP, "config_local.json"), json.dumps({"tickers": ["AAPL"]}))
check("absent allowlist means nobody can read", nw.allowed_emails(web_db) == [],
      nw.allowed_emails(web_db))

print()
print("=== upload: stale metadata is dropped, secrets survive an omitted field ===")
panel = os.path.join(TMP, "panel")
os.makedirs(panel, exist_ok=True)
cm.BASE_DIR = panel
cm.CONFIG_FILE = os.path.join(panel, "config_local.json")
cm.SECRETS_FILE = os.path.join(panel, "secrets_local.json")
write(cm.CONFIG_FILE, json.dumps({
    "tickers": ["OLD"], "ticker_meta": {"OLD": {"name_en": "Old Co"}},
    "rss_feeds": {"OLD": ["https://old.example/feed"]}}))
write(cm.SECRETS_FILE, json.dumps({"ai_api_key": "keep-me"}))
cm.get_project = lambda: None       # never deploy from a unit test


class UploadStub:
    def __init__(self):
        self.sent = None

    def _send_json(self, obj, status=200):
        self.sent = (obj, status)


up = UploadStub()
cm.Handler._handle_upload(up, {
    "tickers": ["AAPL"],
    "ticker_meta": {"AAPL": {"name_en": "Apple Inc."},
                    "OLD": {"name_en": "Old Co"}},
    "web_allowed_emails": "Me@Gmail.com, not-an-email",
})
saved = json.load(open(cm.CONFIG_FILE, encoding="utf-8"))
check("only the posted tickers are kept", saved["tickers"] == ["AAPL"], saved["tickers"])
check("metadata for a removed ticker is dropped",
      set(saved["ticker_meta"]) == {"AAPL"}, saved["ticker_meta"])
check("feeds for a removed ticker are dropped",
      saved.get("rss_feeds") == {}, saved.get("rss_feeds"))
check("allowlist normalised, invalid address ignored",
      saved.get("web_allowed_emails") == ["me@gmail.com"],
      saved.get("web_allowed_emails"))
secrets_saved = json.load(open(cm.SECRETS_FILE, encoding="utf-8"))
check("an omitted secret is preserved, not wiped",
      secrets_saved.get("ai_api_key") == "keep-me", secrets_saved)

before = open(cm.CONFIG_FILE, encoding="utf-8").read()
cm.Handler._handle_upload(up, {"tickers": []})
after = open(cm.CONFIG_FILE, encoding="utf-8").read()
check("an empty upload is refused and writes nothing",
      up.sent[1] == 400 and before == after, up.sent)

shutil.rmtree(TMP, ignore_errors=True)
print()
if fails:
    for f in fails:
        print("  failed:", f)
print("RESULT: " + ("ALL TESTS PASSED" if not fails else f"{len(fails)} FAILURE(S)"))
sys.exit(1 if fails else 0)
