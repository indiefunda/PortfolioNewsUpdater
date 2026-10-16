# Gate: the Deep search tab must get the same anti-crap treatment as the
# pipeline - junk hosts filtered, same-event copies merged, recycled stories
# flagged and kept out of the sentiment read. Offline: sources and the AI are
# mocked, so it costs nothing.
import os
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

meta = {
    "name_zh": "乐信", "name_en": "LexinFintech Holdings",
    "aliases_zh": ["乐信集团"], "subsidiaries_zh": ["分期乐"], "subsidiaries_other": [],
}
config = nu.load_config()
secrets = {"ai_api_key": "test-key-not-used"}

# Four copies of ONE event, one content-farm link, one recycled old story,
# and one genuinely different story.
COPIES = [
    ("乐信发布二季度财报：营收超预期", "GoogleNewsZH", "https://news.example/1"),
    ("乐信二季度财报营收超预期 净利增长", "Eastmoney", "https://news.example/2"),
    ("LexinFintech Q2 results beat expectations", "GoogleNews", "https://news.example/3"),
    ("乐信公布二季度业绩 营收超预期", "Tavily", "https://news.example/4"),
]
JUNK = ("乐信财经快讯：分期乐最新消息", "GoogleNewsZH", "https://cheap-casino-xyz.buzz/1")
RECYCLED = ("乐信2024年财报回顾", "GoogleNewsZH", "https://news.example/5")
OTHER = ("乐信宣布3亿美元回购计划", "GoogleNewsZH", "https://news.example/6")

def fake_rss(url, ticker, since, source=None, lang=None, **kw):
    return [{"title": t, "url": u, "snippet": "", "date": "2026-10-15",
             "source": source, "lang": lang or "zh"}
            for t, s, u in (COPIES + [JUNK, RECYCLED, OTHER]) if s == source]

nu.fetch_rss = fake_rss
nu.fetch_eastmoney_search = lambda term, since=None, terms=None: [
    {"title": t, "url": u, "snippet": "", "date": "2026-10-15", "source": "Eastmoney", "lang": "zh"}
    for t, s, u in COPIES if s == "Eastmoney"]
nu.fetch_tavily = lambda q, sec, cfg, since=None, limit=8, topic="news", terms=None: [
    {"title": t, "url": u, "snippet": "", "date": "2026-10-15", "source": "Tavily", "lang": "en"}
    for t, s, u in COPIES if s == "Tavily"]

items, cost = nu.adhoc_collect("LX", meta, config, secrets, days=30)
urls = [i.get("url", "") for i in items]
titles = [i.get("title", "") for i in items]
print("  collected:", len(items))
for t in titles:
    print("    -", t[:60])
check("a content-farm result is NOT in the scan", not any("buzz" in u for u in urls), urls)
check("the company's real stories survive the junk filter", any("回购" in t for t in titles))

# ---- the AI's same-event verdicts (mocked) ----
def is_earnings(t):
    return ("财报" in t or "results" in t or "业绩" in t) and not t.startswith("乐信2024")

def fake_ai_analyze(batch, cfg, sec, meta_map, conn=None, run_start=None,
                    prompt_mode=None, prompt_extra=None):
    canon = None
    for i, it in enumerate(batch, 1):
        if is_earnings(it.get("title", "")):
            canon = i
            break
    out = []
    for i, it in enumerate(batch, 1):
        it = dict(it)
        t = it.get("title", "")
        it["_number"] = i
        dup = is_earnings(t) and i != canon
        it["is_dup"], it["_dup_of"] = dup, (canon if dup else None)
        it["is_known"] = t.startswith("乐信2024")
        it["title_en"] = "EN: " + t
        it["importance"] = 3 if it["is_known"] else (9 if "回购" in t else 8)
        it["sentiment"] = "positive"
        it["category"] = "earnings"
        it["reason"], it["impact"] = "why", "impact"
        out.append(it)
    return out

nu.ai_analyze = fake_ai_analyze
seen_prompts = {}
def fake_chat(base, model, key, system, prompt):
    seen_prompts["p"] = prompt
    return ('{"sentiment":"bullish","confidence":7,"summary":"ok",'
            '"themes":["earnings"],"drivers":[]}')
nu._chat = fake_chat

articles, overall, merged = nu.adhoc_analyze(items, "LX", meta, config, secrets)
print("  articles:", len(articles), "| same-event copies merged:", merged)
for a in articles:
    print(f"    [{a['sources_count']} outlet(s)] recycled={a['recycled']} "
          f"star={a['importance']} {a['title'][:44]}")

check("same-event copies are merged in the scan too", merged == 2, merged)
canon = [a for a in articles if (a.get("sources_count") or 1) > 1]
check("the canonical row records its outlets",
      bool(canon) and canon[0]["sources_count"] == merged + 1,
      [a["sources_count"] for a in articles])
check("the copies' links are kept on the canonical row",
      bool(canon) and len(canon[0].get("also_seen") or []) == canon[0]["sources_count"] - 1,
      (canon[0].get("also_seen") if canon else None))
check("recycled stories are KEPT for research, flagged and last",
      articles[-1]["recycled"] is True and len(articles) == 3,
      [(a["recycled"], a["title"][:18]) for a in articles])
check("recycled stories are kept out of the sentiment read",
      "2024" not in seen_prompts.get("p", ""), "2024" in seen_prompts.get("p", ""))
check("the sentiment brief carries the outlet count",
      "outlets" in seen_prompts.get("p", ""))
check("articles are ordered most-important-first",
      (articles[0].get("importance") or 0) >= (articles[1].get("importance") or 0),
      [a.get("importance") for a in articles])

# ---- adhoc_scan must pass the merged count through to the panel ----
nu.ensure_company_meta = lambda *a, **k: dict(meta)
res = nu.adhoc_scan("LX", config, secrets, days=30, refresh_profile=False)
check("the scan result reports the merged count",
      (res.get("cost") or {}).get("duplicates_merged") == 2,
      (res.get("cost") or {}).get("duplicates_merged"))
check("the scan result carries the outlet count",
      any((a.get("sources_count") or 1) > 1 for a in res.get("articles", [])))

print()
print("FAILURES:", len(fails), fails)
print("RESULT: " + ("ALL TESTS PASSED" if not fails
                    else f"{len(fails)} FAILURE(S)"))
sys.exit(1 if fails else 0)
