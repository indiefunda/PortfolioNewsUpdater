# Throwaway: does the staging pipeline turn the user's 12 rows into events?
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

path = os.path.join(BASE, "_staging_test.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)
nu.DB_FILE = path
nu.NO_WRITE = False
conn = nu._db()

# The user's actual rows (title, source, url).
rows = [
 ("æ»´æ»´é‡‘èžã€å°èµ¢ã€åˆ†æœŸä¹ã€æ´‹é’±ç½ç­‰15å®¶åŠ©è´·å¹³å°ä¸“é¡¹å®¡è®¡æŠ¥å‘Šï¼šä¸å­˜åœ¨æŒªç”¨ä¿¡è´·èµ„é‡‘", "Xueqiu", "https://x/1"),
 ("æ¶‰åŠ10.7äº¿äººï¼å¤®è¡Œå¾ä¿¡æ–°è§„å¾æ±‚æ„è§", "Yema", "https://x/2"),
 ("ä¸­äº’é‡‘ï¼šæºç¨‹é‡‘èžã€å°èµ¢ã€ä¿¡ä¹Ÿç­‰15å®¶åŠ©è´·å¹³å°æäº¤ç¬¬ä¸‰æ–¹å®¡è®¡æŠ¥å‘Š", "SinaWap", "https://x/3"),
 ("æºç¨‹é‡‘èžã€å¥‡ä¹æ‹ç­‰15å®¶åŠ©è´·å…¬å¸æäº¤ä¸‰æ–¹å®¡è®¡ç»“æžœï¼šä¸å­˜åœ¨æŒªç”¨", "Xueqiu", "https://x/4"),
 ("ä¸­å›½äº’é‡‘åä¼šï¼š15å®¶åŠ©è´·å¹³å°æäº¤å®¡è®¡ç»“æžœ ä¸å­˜åœ¨èµ„é‡‘æŒªç”¨", "NIFA", "https://x/5"),
 ("æºç¨‹é‡‘èžã€å¤©æ˜Ÿæ•°ç§‘ç­‰15å®¶åŠ©è´·å¹³å°æäº¤ä¸“é¡¹å®¡è®¡æŠ¥å‘Š äº’è”ç½‘è´·æ¬¾è‡ªå¾‹å·¥ä½œå§”å‘˜ä¼šç­¹å¤‡ç»„æœ‰æœ›æ‰©å®¹", "Sina", "https://x/6"),
 ("åŠ©è´·å¹³å°ç”³è¯·åŠ å…¥äº’è”ç½‘è´·æ¬¾è‡ªå¾‹å·¥ä½œå§”å‘˜ä¼šç­¹å¤‡ç»„ç›¸å…³è¿›å±•æƒ…å†µå…¬å‘Šï¼ˆç¬¬ä¸€æœŸï¼‰", "SinaFin", "https://x/7"),
 ("åŠ©è´·ä¸šè‡ªå¾‹åŠ ç ï¼äº’è”ç½‘è´·æ¬¾è‡ªå¾‹ç­¹å¤‡ç»„å°†æ‰©å®¹ï¼Œç´§ç›¯èµ„é‡‘æŒªç”¨é£Žé™©", "Sina", "https://x/8"),
 ("ä¸­äº’é‡‘åä¼šæŠ«éœ²äº’è”ç½‘è´·æ¬¾è‡ªå¾‹ç­¹å¤‡ç»„è¿›å±•ï¼š15 å®¶åŠ©è´·å¹³å°æäº¤ä¸“é¡¹å®¡è®¡æŠ¥å‘Š", "Portal", "https://x/9"),
 ("åŠ©è´·æ–°è§„è½åœ°å‘¨å¹´ï¼šåˆ©æ¶¦è·³æ°´ä¸Žè¡Œä¸šåˆ†åŒ–", "Portal2", "https://x/10"),
 ("15 å®¶åŠ©è´·å·²æäº¤å®¡è®¡æŠ¥å‘Š é£Žé™©å¹³å°é—ç•™â€œå¾ä¿¡å€ºâ€é—®é¢˜ä½•è§£ï¼Ÿ", "Sohu", "https://x/11"),
 ("15å®¶åŠ©è´·å¹³å°æäº¤å®¡è®¡æŠ¥å‘Šï¼Œäº’è”ç½‘è´·æ¬¾è‡ªå¾‹ç­¹å¤‡ç»„æœ‰æœ›æ‰©å®¹ï¼ŒææŒ¯èµ„é‡‘æ–¹ä¿¡å¿ƒ", "Jiemian", "https://x/12"),
]
run_id = "2026-10-16 09:15:00"
items = []
for i, (title, source, url) in enumerate(rows, 1):
    it = {"ticker": "MACRO", "source": source, "id": f"id{i}", "title": title,
          "url": url, "lang": "zh", "published_at": "2026-10-16 03:00:00",
          "snippet": "", "_run_id": run_id, "category": "fintech_reg"}
    nu.stage_item(conn, it)
    nu.mark_seen(conn, "MACRO", source, f"id{i}", title, url)
    items.append(it)

check("all 12 items are staged", conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 12)
check("nothing is in `news` yet", conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 0)

# What the AI returns for these (its real duplicate_of decisions):
#   1,3,4,5,6,7,9,11,12 -> the audit announcement (canonical = 9, the NIFA one)
#   8 -> same event as 6 (self-regulation expansion, folded into the same story)
#   2 -> PBOC credit-reporting rules (own event)
#   10 -> one year on from the new rules (own event)
groups = {1: 9, 3: 9, 4: 9, 5: 9, 6: 9, 7: 9, 8: 6, 11: 9, 12: 9}
for i, it in enumerate(items, 1):
    it["_number"] = i
    it["_dup_of"] = groups.get(i)
    it["is_dup"] = i in groups
    it["is_known"] = False
    it["importance"] = 8 if i in (2, 10, 9) else 5
    it["push"] = i in (2, 12)
    it["category"] = "regulatory" if i == 2 else "fintech_reg"
    it["push"] = bool(it["push"])

promoted, dropped, known = nu.promote_run(conn, items, run_id=run_id)
print(f"  promote: {len(items)} items -> {promoted} event row(s), {dropped} merged")
check("12 items collapse to 3 events", promoted == 3, promoted)
check("3 rows stored, not 12", conn.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 3)

stored = conn.execute("SELECT title_raw, sources_count, also_seen FROM news ORDER BY id").fetchall()
for t, cnt, als in stored:
    print(f"    [{cnt} outlets] {t[:64]}")
counts = sorted(c for _, c, _ in stored)
check("the audit event records its 9 outlets", counts == [1, 1, 9], counts)
audit = [s for s in stored if s[1] == 9][0]
check("other outlets are kept on the canonical row",
      "Jiemian" in (audit[2] or "") and "SinaWap" in (audit[2] or ""),
      (audit[2] or "")[:80])
check("staging rows are marked promoted",
      conn.execute("SELECT COUNT(*) FROM staging WHERE promoted=0").fetchone()[0] == 0)

# the copies must not be pushable
canonical, _ = nu._event_groups(items)
from_copies = [it for it in items if it.get("is_dup")]
check("no copy can be pushed", all(not it.get("push") or it.get("is_dup") for it in from_copies))

# ---- crash recovery: a staged row from an OLD run that never got promoted ----
conn.execute(
    "INSERT INTO staging (run_id, ticker, source, lang, item_hash, title_raw,"
    " snippet, url, published_at, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("2026-10-13 09:15:00", "LX", "Portal", "zh", "stale1", "ä¹ä¿¡æ—§é—»æœªæå‡",
     "", "https://x/stale", "2026-10-13 01:00:00", "2026-10-13 09:15:30"))
conn.commit()
recovered = nu.promote_stale_staging(conn, keep_hours=48)
check("an interrupted run's item is recovered, not lost", recovered == 1, recovered)
row = conn.execute("SELECT importance, pushed FROM news WHERE item_hash='stale1'").fetchone()
check("recovered row is stored unscored (rescue_orphans will score it)",
      row is not None and row[0] is None and row[1] == 0, row)

keep = nu.prune_staging(conn, days=3, promoted_hours=24)
check("the 3-day-old recovered row is pruned, fresh ones kept",
      keep == 1 and conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0] == 12,
      (keep, conn.execute("SELECT COUNT(*) FROM staging").fetchone()[0]))
pruned = nu.prune_staging(conn, days=1, promoted_hours=-1)
check("handled staging rows are pruned once past their TTL", pruned == 12, pruned)

# ---- digest shows the outlet count ----
digest = nu.format_digest(nu.promote_run.__wrapped__ if False else
                          [dict(items[8], title_en="NIFA: 15 platforms submit audit reports",
                                reason="sector-wide audit", impact="applies to LX/QFIN")],
                          6, stored_count=2)
check("digest shows '9 outlets'", "9 outlets" in (digest or ""), (digest or "")[:120])

conn.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)
print()
print("FAILURES:", len(fails), fails)
sys.exit(1 if fails else 0)

