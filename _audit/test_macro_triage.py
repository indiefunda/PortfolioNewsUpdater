import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Gate: the macro tier must be filtered by the AI's relevance verdict, not by a
# keyword list - and must never fall back to storing the raw tape.
# Offline: the AI is mocked, so it costs nothing.
import os
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

# The user's actual complaints plus genuine macro news.
JUNK = [
    "三部门：同意京津冀等5个城市群开展氢能综合应用试点工作",
    "斯里兰卡人民币清算行在科伦坡开业",
    "纳斯达克中国金龙指数跌超0.5%，天演药业跌7.50%，世纪互联跌4.32%，迅雷跌4.32%。",
]
REAL = [
    "中互金协会披露互联网贷款自律筹备组进展：15家助贷平台提交专项审计报告",
    "涉及10.7亿人！央行征信新规征求意见",
    "国常会部署扩大内需政策",
]

config = nu.load_config()
secrets = {"ai_api_key": "test-key"}

def make_items(titles, run_id):
    items = []
    for i, t in enumerate(titles, 1):
        it = {"ticker": "MACRO", "source": "Eastmoney724", "id": f"id{i}",
              "title": t, "snippet": "", "lang": "zh",
              "published_at": "2026-10-16 12:00:00", "_run_id": run_id}
        items.append(it)
    return items

def fresh_db(path):
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    nu.DB_FILE = path
    nu.NO_WRITE = False
    conn = nu._db()
    conn.execute("DELETE FROM news")
    conn.execute("DELETE FROM staging")
    conn.commit()
    return conn

path = os.path.join(BASE, "_triage_test.db")

print("=== 1. the AI's verdict decides what is stored ===")
run_id = "2026-10-16 12:24:00"
conn = fresh_db(path)
items = make_items(JUNK + REAL, run_id)
for it in items:
    nu.stage_item(conn, it)
check("all candidates are staged first",
      conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 6)

def mock_ai(batch, cfg, sec, meta_map, conn=None, run_start=None,
            prompt_mode=None, prompt_extra=None):
    out = []
    for i, it in enumerate(batch, 1):
        it = dict(it)
        it["title_en"] = "EN: " + it.get("title", "")
        it["category"] = "fintech_reg" if "助贷" in it["title"] else "monetary"
        it["summary"] = "s"
        it["sentiment"] = "neutral"
        it["reason"] = "r"
        it["impact"] = "i"
        it["is_dup"] = False
        it["is_known"] = False
        it["_number"] = i
        it["_dup_of"] = None
        # What the model must answer: the three junk items are irrelevant.
        junk = it["title"] in JUNK
        it["relevant"] = not junk
        it["importance"] = 1 if junk else (8 if "助贷" in it["title"] else 7)
        it["push"] = not junk
        out.append(it)
    return out

nu.ai_analyze = mock_ai
pushed = nu.analyze_macro(items, config, secrets, conn, run_id, pseudo="MACRO")
stored = [r[0] for r in conn.execute("SELECT title_raw FROM news ORDER BY id")]
print("  stored:")
for t in stored:
    print("    -", t[:64])
for t in JUNK:
    check(f"REJECTED: {t[:38]}", t not in stored)
for t in REAL:
    check(f"kept    : {t[:38]}", t in stored)
check("only relevant rows reached the archive", len(stored) == len(REAL), len(stored))
check("only relevant rows can be pushed",
      all(it.get("relevant") for it in pushed) and len(pushed) == len(REAL), len(pushed))
check("rejected staging rows are marked handled (no later resurrection)",
      conn.execute("SELECT COUNT(*) FROM staging WHERE promoted=0").fetchone()[0] == 0)
conn.close()

print("\n=== 2. no AI verdict -> strict gate, never the raw tape ===")
run_id2 = "2026-10-16 17:00:00"
conn = fresh_db(path)
items2 = make_items(JUNK + REAL, run_id2)
for it in items2:
    nu.stage_item(conn, it)

def mock_ai_no_verdict(batch, cfg, sec, meta_map, conn=None, run_start=None,
                       prompt_mode=None, prompt_extra=None):
    out = []
    for i, it in enumerate(batch, 1):
        it = dict(it)
        it["title_en"] = it.get("title", "")
        it["is_dup"] = it["is_known"] = False
        it["_number"], it["_dup_of"] = i, None
        it["importance"], it["push"] = 1, False
        it.pop("relevant", None)          # model answered nothing about relevance
        out.append(it)
    return out

nu.ai_analyze = mock_ai_no_verdict
pushed2 = nu.analyze_macro(items2, config, secrets, conn, run_id2, pseudo="MACRO")
stored2 = [r[0] for r in conn.execute("SELECT title_raw FROM news ORDER BY id")]
for t in JUNK:
    check(f"still not stored without a verdict: {t[:30]}", t not in stored2)
check("strict gate kept the lending regulation", any("助贷" in t for t in stored2), stored2)
check("the raw tape was NOT stored", len(stored2) < len(JUNK) + len(REAL), len(stored2))
conn.close()

print("\n=== 3. no AI key at all -> strict gate, rows still archived ===")
run_id3 = "2026-10-16 19:00:00"
conn = fresh_db(path)
items3 = make_items(JUNK + REAL, run_id3)
for it in items3:
    nu.stage_item(conn, it)
nu.analyze_macro(items3, config, {}, conn, run_id3, pseudo="MACRO")
stored3 = [r[0] for r in conn.execute("SELECT title_raw FROM news ORDER BY id")]
check("lending regulation archived even with no AI", any("助贷" in t for t in stored3), stored3)
check("junk not archived with no AI", not any("氢能" in t for t in stored3), stored3)
check("micro-cap index recap not archived", not any("金龙指数" in t for t in stored3))
conn.close()

print("\n=== 4. the triage pool is wider than the storage cap ===")
check("pool default is 60 candidates", nu.MACRO_TRIAGE_POOL_MAX == 60,
      nu.MACRO_TRIAGE_POOL_MAX)
check("storage cap stays small", nu.MACRO_STORE_MAX_PER_RUN <= 12,
      nu.MACRO_STORE_MAX_PER_RUN)
check("the relevance test names the reject categories",
      all(w in nu.RELEVANCE_TEST_HEADER for w in
          ("Sri Lanka", "hydrogen", "index recap", "geopolitics")))
check("the relevance test is built from the USER's config, not hardcoded",
      "QFIN" not in nu.relevance_test_prompt({"tickers": ["AAPL"],
                                              "ticker_meta": {}})
      and "AAPL" in nu.relevance_test_prompt({"tickers": ["AAPL"],
                                              "ticker_meta": {}}))
check("the real config names every holding",
      all(t in nu.portfolio_brief(config) for t in config.get("tickers", [])),
      nu.portfolio_brief(config))

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
