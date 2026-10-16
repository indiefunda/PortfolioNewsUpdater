# Gate: the archive clean-up - junk-content filtering, the multi-rule purge,
# and the AI event regroup (mocked, so it costs nothing).
import os
import sqlite3
import sys

BASE = r"F:\MyRepository\PortfolioNewsUpdater"
sys.path.insert(0, BASE)
os.chdir(BASE)
import news_updater as nu

fails = []
def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), "-", name, ("| " + str(detail)) if detail else "")
    if not cond:
        fails.append(name)

print("=== claim-farming / quote-page filter ===")
junk = [
 "Stockholder Alert: Robbins LLP Informs Investors of the Qfin Holdings, Inc. Class Action",
 "QFIN Class Action Notice: Qfin Holdings Sued for Securities Fraud After Regulatory",
 "Deadline Soon: Qfin Holdings, Inc. (QFIN) Shareholders Who Lost Money Urged To",
 "Lufax Holding Ltd (LU) Stock Price, News, Quote & History - Yahoo Finance",
 "LexinFintech Holdings (NasdaqGS:LX) Stock Valuation, Peer Comparison & Price Target",
 "Lufax (NYSE:LU) Reaches New 1-Year Low - Here's What Happened",
 "LU Stock Holds Ground As Ownership Shifts And Overhaul Nears Completion",
 "Yuanbao Inc ADR (YB-Q) Press Releases",
 "Earnings Calendar for:",
 "Lufax Holding Ltd Options Chain",
]
real = [
 "Lufax to implement a 1-for-10 reverse ADS split effective October 23, 2026",
 "Morgan Stanley cuts Qifu Technology price target to $11 on earnings outlook",
 "LexinFintech announces $300 million share repurchase programme",
 "中互金协会披露互联网贷款自律筹备组进展：15 家助贷平台提交专项审计报告",
 "Qfin Holdings, Inc. - 144 filed 2026-09-28",
 "Lufax shareholders approve notes extension to 2027 | LU SEC Filing - Form 6-K",
]
for t in junk:
    if nu.is_junk_content(t):
        print(f"  PASS - rejected: {t[:62]}")
    else:
        check(f"junk rejected: {t[:40]}", False, "NOT filtered")
for t in real:
    if not nu.is_junk_content(t):
        print(f"  PASS - kept    : {t[:62]}")
    else:
        check(f"real news kept: {t[:40]}", False, "wrongly filtered")
check("analyst price-target news survives the filter",
      not nu.is_junk_content("Morgan Stanley cuts Qifu Technology price target to $11"))

print("\n=== the multi-rule purge ===")
path = os.path.join(BASE, "_arch_test.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)
nu.DB_FILE = path
nu.NO_WRITE = False
conn = nu._db()
rows = [
    # (ticker, title, importance, pushed)
    ("MACRO", "沙特称胡塞武装袭击利雅得机场共致17死333伤", 7, 1),   # off-topic, pushed
    ("MACRO", "肯德基接近达成协议 在美国数千家餐厅开展百事可乐换购活动", 3, 0),
    ("MACRO", "中互金协会：15家助贷平台提交专项审计报告", 8, 1),   # sector reg -> keep
    ("MACRO", "美国9月非农就业人数增加2.9万人 大幅低于预期", 9, 1),  # systemic -> keep
    ("QFIN", "QFIN Class Action Notice: Qfin Holdings Sued for Securities Fraud", 7, 1),
    ("LX", "LexinFintech Holdings (NasdaqGS:LX) Stock Price, News, Quote & History", 3, 0),
    ("LX", "Ivorian (BRVM) Investor Community", 3, 0),             # unrelated, unpushed
    ("LX", "乐信发布第二季度财报：营收超预期", 9, 1),               # real news, pushed
    ("YB", "长盈通：签订1.47亿元保偏光纤产品订单", 4, 0),           # unrelated, unpushed
]
for i, (tk, title, imp, pushed) in enumerate(rows, 1):
    conn.execute(
        "INSERT INTO news (ticker, source, lang, item_hash, title_raw, snippet,"
        " importance, pushed, first_seen) VALUES (?,?,?,?,?,?,?,?,?)",
        (tk, "Test", "zh", f"h{i}", title, "", imp, pushed, "2026-10-16 09:15:00"))
conn.commit()
conn.close()

sys.argv = ["news_updater.py", "--purge-junk"]
try:
    nu.main()
except SystemExit:
    pass
conn = sqlite3.connect(path)
left = [r[0] for r in conn.execute("SELECT title_raw FROM news ORDER BY id")]
conn.close()
for t in left:
    print("    kept:", t[:62])
check("off-topic MACRO removed even though it was pushed",
      not any("胡塞" in t for t in left))
check("KFC macro noise removed", not any("肯德基" in t for t in left))
check("claim-farming row removed even though it was pushed",
      not any("Class Action" in t for t in left))
check("quote-page row removed", not any("Quote & History" in t for t in left))
check("unrelated LX row removed", not any("Ivorian" in t for t in left))
check("unrelated YB row removed (长盈通)", not any("长盈通" in t for t in left))
check("sector regulation kept", any("15家助贷平台" in t for t in left))
check("systemic macro kept", any("非农" in t for t in left))
check("real company news kept", any("乐信发布第二季度财报" in t for t in left))

print("\n=== AI archive regroup (mocked) ===")
conn = sqlite3.connect(path)
conn.execute("DELETE FROM news")
audit = [
    ("中互金协会披露互联网贷款自律筹备组进展：15 家助贷平台提交专项审计报告", 6, 0),
    ("滴滴金融、小赢、分期乐、洋钱罐等15家助贷平台专项审计报告：不存在挪用信贷资金", 6, 0),
    ("15家助贷平台提交审计报告，互联网贷款自律筹备组有望扩容，提振资金方信心", 6, 1),
    ("助贷新规落地周年：利润跳水与行业分化", 8, 0),
    ("涉及10.7亿人！央行征信新规征求意见", 8, 1),
]
for i, (t, imp, pushed) in enumerate(audit, 1):
    conn.execute("INSERT INTO news (ticker, source, lang, item_hash, title_raw,"
                 " importance, pushed, first_seen) VALUES (?,?,?,?,?,?,?,?)",
                 ("MACRO", "Sector", "zh", f"a{i}", t, imp, pushed,
                  "2026-10-16 09:17:00"))
conn.commit()
conn.close()

config, secrets = nu.load_config(), {"ai_api_key": "test"}
try:
    _probe = sqlite3.connect(path)
    groups, victims, err = nu.regroup_events(_probe, dry_run=True, config=config,
                                            secrets={})
    _probe.close()
except Exception as exc:
    groups, victims, err = [], [], f"raised {exc}"
check("a missing AI key reports an error instead of deleting", bool(err), err)

# The rows' real ids: the table is AUTOINCREMENT, so they are not 1..5.
_ids = []
_probe = sqlite3.connect(path)
_ids = [r[0] for r in _probe.execute("SELECT id FROM news ORDER BY id")]
_probe.close()
print("    real row ids:", _ids)
A, B, C, D, E = _ids
# A usable answer groups the three audit copies and keeps the PUSHED one (C).
nu._chat = lambda *a, **k: '{"groups": [[%d, %d, %d], [%d], [%d]]}' % (A, B, C, D, E)
conn = sqlite3.connect(path)
groups, victims, err = nu.regroup_events(conn, dry_run=True, config=config, secrets=secrets)
check("the AI grouping is parsed into groups", len(groups) == 1 and not err,
      (len(groups), err))
check("two copies are the victims", len(victims) == 2, len(victims))
check("the keeper prefers a DELIVERED row",
      bool(groups) and groups[0]["keeper"][4] and "提振资金方信心" in (groups[0]["keeper"][2] or ""),
      groups[0]["keeper"][2][:40] if groups else None)
check("unrelated events are left alone (distinct rows kept)",
      all(v[2] not in ("助贷新规落地周年：利润跳水与行业分化", "涉及10.7亿人！央行征信新规征求意见")
          for v in victims))

# A garbled answer must delete nothing.
nu._chat = lambda *a, **k: "sorry, I cannot help with that"
g2, v2, err2 = nu.regroup_events(conn, dry_run=True, config=config, secrets=secrets)
check("a garbled AI answer deletes NOTHING", v2 == [] and bool(err2), (len(v2), err2))

# Real run deletes only the victims.
nu._chat = lambda *a, **k: '{"groups": [[%d, %d, %d], [%d], [%d]]}' % (A, B, C, D, E)
groups, victims, err = nu.regroup_events(conn, dry_run=False, config=config, secrets=secrets)
left = [r[0] for r in conn.execute("SELECT title_raw FROM news ORDER BY id")]
conn.close()
for t in left:
    print("    kept:", t[:62])
check("the regroup leaves one row per event", len(left) == 3, len(left))
check("the delivered copy is the survivor",
      any("提振资金方信心" in t for t in left))
check("different events survive",
      any("央行征信新规" in t for t in left) and any("助贷新规落地周年" in t for t in left))

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)
print()
print("FAILURES:", len(fails), fails)
print("RESULT: " + ("ALL TESTS PASSED" if not fails else f"{len(fails)} FAILURE(S)"))
sys.exit(1 if fails else 0)
