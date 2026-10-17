import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Gate: does the pipeline behave for ANY type of stock, not just Chinese ADRs?
# Findings from the 2026-10-16 review, each pinned here as a regression guard.
import os
import re
import sqlite3
import sys

BASE = ROOT
sys.path.insert(0, BASE)
os.chdir(BASE)
import news_updater as nu

fails = []
def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail)) if detail else "")
    if not cond:
        fails.append(name)

def verdict(ticker, title, meta):
    return nu.passes_relevance({"ticker": ticker, "title": title, "snippet": ""},
                               meta, nu.resolve_relevance(meta))

print("=== F2: word-like tickers must not match ordinary English ===")
# Real US symbols that are also common words. Before the fix every one of these
# was classified as company news and stored/pushed.
WORDY = {"OPEN": "Opendoor Technologies", "KEY": "KeyCorp", "ALL": "Allstate",
         "NOW": "ServiceNow", "FIVE": "Five Below", "PLAY": "Dave & Buster's",
         "LOVE": "Lovesac", "TRUE": "TrueCar", "MOVE": "Movano"}
PROSE = {
    "OPEN": ["Markets open higher as inflation cools",
             "Open question: will the Fed cut again?"],
    "KEY": ["Key question for markets in 2027", "The key risk nobody is pricing"],
    "ALL": ["All eyes on the Fed this week"],
    "NOW": ["Now is the time to buy bonds, says strategist"],
    "FIVE": ["Five reasons the rally may stall"],
    "PLAY": ["Play the long game, strategists say"],
    "LOVE": ["Love it or list it: the housing market stalls"],
    "TRUE": ["True inflation may be lower than it looks"],
    "MOVE": ["Move over bonds: cash is king again"],
}
for tk, titles in PROSE.items():
    meta = {"ticker": tk, "name_en": WORDY[tk], "name_zh": ""}
    for t in titles:
        v = verdict(tk, t, meta)
        check(f"{tk}: ordinary prose is not company news", v != "company", f"{v} | {t}")

print("\n=== the same word-like tickers must still catch REAL news ===")
REAL = [
    ("OPEN", "Opendoor Technologies (OPEN) reports Q3 results, revenue beats"),
    ("KEY", "KeyCorp (KEY) announces $1bn buyback after earnings beat"),
    ("FIVE", "Five Below (FIVE) cuts guidance as traffic slows"),
    ("NOW", "ServiceNow (NOW) upgraded to overweight at Morgan Stanley"),
    ("ALL", "Allstate (ALL) shares fall after catastrophe losses"),
    ("PLAY", "Dave & Buster's (PLAY) plunges on weak same-store sales"),
]
for tk, t in REAL:
    meta = {"ticker": tk, "name_en": WORDY[tk], "name_zh": ""}
    v = verdict(tk, t, meta)
    check(f"{tk}: real story kept", v == "company", f"{v} | {t}")

print("\n=== unambiguous symbols and CJK behaviour are unchanged ===")
for tk, name, t, zh in (
        ("AAPL", "Apple Inc", "Apple Inc (AAPL) announces new iPhone lineup", ""),
        ("GME", "GameStop Corp", "GameStop (GME) surges after Cohen stake", ""),
        ("0700.HK", "Tencent Holdings", "腾讯控股公布第二季度业绩", "腾讯控股")):
    v = verdict(tk, t, {"ticker": tk, "name_en": name, "name_zh": zh})
    check(f"{tk}: kept", v == "company", v)
check("a Chinese title needs the Chinese name (English name alone is not enough)",
      verdict("0700.HK", "腾讯控股公布第二季度业绩",
              {"ticker": "0700.HK", "name_en": "Tencent Holdings",
               "name_zh": ""}) != "company")
lx = {"ticker": "LX", "name_zh": "乐信", "name_en": "LexinFintech Holdings",
      "subsidiaries_zh": ["分期乐"]}
check("CJK substring collision still rejected (音乐信息 ≠ 乐信)",
      verdict("LX", "音乐信息平台迎来监管", lx) != "company")
check("real Chinese headline kept", verdict("LX", "乐信发布财报", lx) == "company")
check("subsidiary headline kept", verdict("LX", "分期乐被投诉", lx) == "company")
check("another company's ticker in the title is not ours",
      verdict("LX", "LX Pantos to build a logistics center", lx) != "company")

print("\n=== F4: EDGAR is only attempted for US-style symbols ===")
for sym, want in (("AAPL", True), ("BRK.B", True), ("0700.HK", False),
                  ("600519.SS", False), ("VOD.L", False), ("7203.T", False)):
    got = bool(nu.US_SYMBOL_RE.match(sym))
    check(f"{sym}: EDGAR {'attempted' if want else 'skipped'}", got == want, got)

print("\n=== F3: the prompts describe the USER's watchlist ===")
cfg = nu.load_config()
brief = nu.portfolio_brief(cfg)
check("every configured holding appears in the brief",
      all(t in brief for t in cfg.get("tickers", [])), brief)
other = {"tickers": ["AAPL", "OPEN"], "ticker_meta": {"AAPL": {"name_en": "Apple Inc"}}}
p = nu.relevance_test_prompt(other)
check("a different portfolio gets a different relevance test",
      "AAPL" in p and "QFIN" not in p)
check("the user's own note is honoured",
      "consumer lending" in nu.portfolio_brief(
          {"tickers": ["LX"], "ticker_meta": {},
           "portfolio_note": "US-listed Chinese consumer lending"}))

print("\n=== F1: the paid English search is not locked inside the Chinese block ===")
src = open("news_updater.py", encoding="utf-8").read().splitlines()
ind = {}
for i, line in enumerate(src):
    if 'if src_on("tavily")' in line:
        ind["tavily"] = len(line) - len(line.lstrip())
    if 'if site_domains and src_on("google_news_site")' in line:
        ind["site"] = len(line) - len(line.lstrip())
    if line.strip() == "if zh_terms:":
        ind["zh"] = len(line) - len(line.lstrip())
check("the Tavily block sits OUTSIDE the 'if zh_terms:' block",
      ind.get("tavily") == ind.get("site") and ind.get("tavily", 0) <= ind.get("zh", 99),
      ind)

print("\n=== F5: a crashed run's staged items are recovered at once ===")
path = os.path.join(BASE, "_stocktype_test.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)
nu.DB_FILE = path
nu.NO_WRITE = False
conn = nu._db()
conn.execute("DELETE FROM news")
conn.execute("DELETE FROM staging")
conn.execute(
    "INSERT INTO staging (run_id, ticker, source, lang, item_hash, title_raw,"
    " snippet, url, published_at, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("2026-10-16 09:15:00", "LX", "GoogleNewsZH", "zh", "crash1", "乐信crash前新闻",
     "", "https://x/1", "2026-10-16 01:00:00",
     datetime_now := __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
# A DIFFERENT run id means that run never finished -> recover now, not in 48h.
recovered = nu.promote_stale_staging(conn, current_run_id="2026-10-16 17:00:00")
check("a previous run's leftover is promoted immediately", recovered == 1, recovered)
check("its row is in the archive",
      conn.execute("SELECT COUNT(*) FROM news WHERE item_hash='crash1'").fetchone()[0] == 1)
# A row from the CURRENT run must NOT be resurrected while the run is in flight.
conn.execute(
    "INSERT INTO staging (run_id, ticker, source, lang, item_hash, title_raw,"
    " snippet, url, published_at, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("2026-10-16 17:00:00", "LX", "GoogleNewsZH", "zh", "inflight1", "乐信本次运行中",
     "", "https://x/2", "2026-10-16 12:00:00",
     __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
conn.commit()
again = nu.promote_stale_staging(conn, current_run_id="2026-10-16 17:00:00")
check("an in-flight run's own rows are left alone", again == 0, again)
conn.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        try:
            os.remove(path + suffix)
        except OSError:
            pass

print()
print("FAILURES:", len(fails), fails)
print("RESULT: " + ("ALL TESTS PASSED" if not fails else f"{len(fails)} FAILURE(S)"))
sys.exit(1 if fails else 0)
