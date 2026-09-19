#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export the stored news as a static, mobile-first web page.

READ-ONLY BY CONSTRUCTION. This module opens news.db with mode=ro and writes
exactly two files into the output directory. It never touches gcloud, never
writes to the database, and is deliberately separate from cloud_manager.py -
the panel is a control plane (it can run the updater, upload config and delete
rows) and must never be the thing exposed to the internet.

Usage:
    python3 news_web.py --out=DIR [--db=PATH] [--embed]

    --out=DIR    directory to write index.html + news.json into
    --db=PATH    database to read (default: news.db next to this script)
    --embed      inline the data into index.html as well, so the page also
                 works straight from file:// with no web server

The same index.html is the UI for the hosted (Firebase) version: the data
loader checks for a Firestore handle first and falls back to news.json, so
hosting it only changes the loader, not the page.
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EASTERN = ZoneInfo("America/New_York")

# ---- data ------------------------------------------------------------------

ITEM_SQL = """
SELECT id,
       COALESCE(ticker, '')                              AS ticker,
       COALESCE(source, '')                              AS source,
       COALESCE(lang, '')                                AS lang,
       COALESCE(NULLIF(title_en, ''), title_raw, '')     AS title,
       COALESCE(title_raw, '')                           AS title_raw,
       COALESCE(summary, '')                             AS summary,
       importance,
       COALESCE(sentiment, '')                           AS sentiment,
       COALESCE(category, '')                            AS category,
       COALESCE(pushed, 0)                               AS pushed,
       COALESCE(url, '')                                 AS url,
       COALESCE(published_at, '')                        AS published_at,
       COALESCE(first_seen, '')                          AS first_seen,
       COALESCE(reason, '')                              AS reason,
       COALESCE(impact, '')                              AS impact
FROM news
ORDER BY first_seen DESC, id DESC
"""


def read_items(db_path):
    """Every stored row, newest first. Opens the database read-only."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(ITEM_SQL).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def build_payload(items):
    """The JSON the page consumes. Kept small: only fields the UI renders."""
    tickers = sorted({i["ticker"] for i in items if i["ticker"]})
    return {
        "generated_at": datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "count": len(items),
        "pushed_count": sum(1 for i in items if i["pushed"]),
        "tickers": tickers,
        "items": items,
    }


# ---- page ------------------------------------------------------------------
# NOTE: the JavaScript below deliberately contains NO backslash escape
# sequences outside of JSON, and never a literal newline escape inside a
# string. This HTML lives in a Python string, so a "\n" here would become a
# real newline and split the JS string (the exact bug that broke the panel).
# Every string that needs a line break in the UI builds it with join() or uses
# markup instead.

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Portfolio news</title>
__HOSTED_HEAD__
<style>
  :root{
    --bg:#0b0d12; --card:#12151c; --border:#232733; --text:#e6e9ef;
    --muted:#8b93a7; --accent:#4f8cff; --ok:#a7f3d0; --okbg:#14532d;
    --err:#fecaca; --errbg:#7c2d12; --warn:#fde68a; --warnbg:#78350f;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-text-size-adjust:100%}
  a{color:var(--accent)}
  header{position:sticky;top:0;z-index:5;background:rgba(11,13,18,.97);
    border-bottom:1px solid var(--border);padding:10px 12px calc(10px + env(safe-area-inset-bottom,0))}
  h1{margin:0 0 2px;font-size:16px;font-weight:600}
  .sub{color:var(--muted);font-size:12px}
  .filters{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
  .filters .wide{grid-column:1 / -1}
  input,select{width:100%;background:var(--card);color:var(--text);
    border:1px solid var(--border);border-radius:8px;padding:8px;font-size:14px}
  input[type=search]{-webkit-appearance:none}
  main{padding:8px 12px 40px}
  .row{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:10px;margin-bottom:8px;cursor:pointer}
  .row.pushed{border-left:3px solid var(--okbg)}
  .meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
    font-size:11px;color:var(--muted);margin-bottom:5px}
  .tk{font-weight:700;color:var(--text);letter-spacing:.3px}
  .imp{background:#1d2433;border-radius:6px;padding:1px 6px;font-weight:700;color:var(--text)}
  .imp.hi{background:var(--okbg);color:var(--ok)}
  .imp.mid{background:var(--warnbg);color:var(--warn)}
  .chip{border-radius:10px;padding:1px 7px;font-weight:600}
  .pos{background:var(--okbg);color:var(--ok)}
  .neg{background:var(--errbg);color:var(--err)}
  .neu{background:#2a2e38;color:#cbd5e1}
  .title{font-size:14px;line-height:1.35}
  .body{display:none;margin-top:8px;border-top:1px solid var(--border);padding-top:8px}
  .row.open .body{display:block}
  .sum{color:#c3cad9;font-size:13px;margin:0 0 8px;white-space:pre-wrap}
  .raw{color:var(--muted);font-size:12px;margin:0 0 8px;white-space:pre-wrap}
  .links{display:flex;gap:14px;flex-wrap:wrap;font-size:13px}
  .empty{color:var(--muted);text-align:center;padding:40px 0}
  .more{display:block;width:100%;margin:10px 0 0;padding:11px;border-radius:9px;
    background:var(--card);color:var(--text);border:1px solid var(--border);font-size:14px}
  #signin{max-width:420px;margin:60px auto;padding:20px;text-align:center}
  #signin button{padding:11px 18px;border-radius:9px;border:1px solid var(--border);
    background:var(--card);color:var(--text);font-size:15px;cursor:pointer}
</style>
</head>
<body>
<header>
  <h1>Portfolio news</h1>
  <div class="sub" id="stats">Loading…</div>
  <div class="filters">
    <input class="wide" type="search" id="q" placeholder="Search title or summary…" autocomplete="off">
    <select id="ticker"><option value="">All tickers</option></select>
    <select id="status">
      <option value="">Pushed + stored</option>
      <option value="pushed">Pushed only</option>
      <option value="stored">Stored only</option>
    </select>
    <select id="minImp">
      <option value="0">Any importance</option>
      <option value="4">4+</option>
      <option value="6">6+</option>
      <option value="8">8+</option>
    </select>
    <select id="sentiment">
      <option value="">Any sentiment</option>
      <option value="positive">positive</option>
      <option value="negative">negative</option>
      <option value="neutral">neutral</option>
    </select>
    <select class="wide" id="sort">
      <option value="newest">Newest first</option>
      <option value="oldest">Oldest first</option>
      <option value="importance">Most important first</option>
    </select>
  </div>
</header>
<main>
  <div id="list"></div>
  <button class="more" id="more" style="display:none">Show more</button>
</main>
<script>
// ---- data loader -----------------------------------------------------------
// Two sources, one page. The hosted build sets window.__GL_FIRESTORE__ to a
// query function; the static export fetches news.json (or uses the copy
// embedded below). Everything after this block is source-agnostic.
var EMBEDDED = __EMBEDDED_DATA__;

async function loadData(){
  if (typeof window.__GL_FIRESTORE__ === 'function') {
    return await window.__GL_FIRESTORE__();
  }
  if (EMBEDDED) { return EMBEDDED; }
  var r = await fetch('news.json', {cache:'no-store'});
  return await r.json();
}

// ---- state -----------------------------------------------------------------
var ALL = [], VIEW = [], SHOWN = 0, PAGE = 150;
var F = {q:'', ticker:'', status:'', minImp:0, sentiment:'', sort:'newest'};

function esc(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}

function fmtDate(s){
  // "2026-09-11 17:00:58" -> "09-11 17:00" (no timezone maths; already ET)
  if (!s || s.length < 16) return s || '';
  return s.slice(5, 10) + ' ' + s.slice(11, 16);
}

function impClass(n){
  if (n == null) return '';
  if (n >= 8) return 'hi';
  if (n >= 5) return 'mid';
  return '';
}

function sentClass(s){
  s = String(s || '').toLowerCase();
  if (s === 'positive' || s === 'bullish') return 'pos';
  if (s === 'negative' || s === 'bearish') return 'neg';
  return 'neu';
}

function match(it){
  if (F.ticker && it.ticker !== F.ticker) return false;
  if (F.status === 'pushed' && !it.pushed) return false;
  if (F.status === 'stored' && it.pushed) return false;
  if (F.sentiment && String(it.sentiment || '').toLowerCase() !== F.sentiment) return false;
  if (F.minImp > 0 && (it.importance == null || it.importance < F.minImp)) return false;
  if (F.q){
    var hay = ((it.title || '') + ' ' + (it.title_raw || '') + ' ' +
               (it.summary || '')).toLowerCase();
    if (hay.indexOf(F.q) === -1) return false;
  }
  return true;
}

function applyFilters(){
  VIEW = ALL.filter(match);
  if (F.sort === 'oldest') VIEW.reverse();
  else if (F.sort === 'importance'){
    VIEW.sort(function(a,b){
      var d = (b.importance == null ? -1 : b.importance) - (a.importance == null ? -1 : a.importance);
      return d !== 0 ? d : String(b.first_seen).localeCompare(String(a.first_seen));
    });
  }
  SHOWN = 0;
  document.getElementById('list').innerHTML = '';
  renderMore();
  updateStats();
}

function updateStats(){
  var el = document.getElementById('stats');
  var pushed = VIEW.filter(function(i){ return i.pushed; }).length;
  el.textContent = VIEW.length + ' of ' + ALL.length + ' items · ' + pushed +
    ' pushed · updated ' + (META.generated_at || '');
}

function rowHtml(it){
  var links = [];
  if (it.url) links.push('<a href="' + esc(it.url) + '" target="_blank" rel="noopener">Original</a>');
  if (it.url && it.lang === 'zh')
    links.push('<a href="https://translate.google.com/translate?sl=auto&amp;tl=en&amp;u=' +
      encodeURIComponent(it.url) + '" target="_blank" rel="noopener">Translate</a>');
  var body = [];
  if (it.summary) body.push('<p class="sum">' + esc(it.summary) + '</p>');
  if (it.title_raw && it.title_raw !== it.title)
    body.push('<p class="raw">' + esc(it.title_raw) + '</p>');
  if (it.reason) body.push('<p class="raw">Why: ' + esc(it.reason) + '</p>');
  if (links.length) body.push('<div class="links">' + links.join('') + '</div>');

  return '<div class="row' + (it.pushed ? ' pushed' : '') + '" data-i="' + it.id + '">' +
    '<div class="meta">' +
      '<span class="tk">' + esc(it.ticker) + '</span>' +
      (it.importance != null ? '<span class="imp ' + impClass(it.importance) + '">' + esc(it.importance) + '</span>' : '') +
      (it.sentiment ? '<span class="chip ' + sentClass(it.sentiment) + '">' + esc(it.sentiment) + '</span>' : '') +
      '<span>' + esc(fmtDate(it.first_seen)) + '</span>' +
      '<span>' + esc(it.source) + '</span>' +
      (it.pushed ? '<span class="chip pos">pushed</span>' : '') +
    '</div>' +
    '<div class="title">' + esc(it.title) + '</div>' +
    (body.length ? '<div class="body">' + body.join('') + '</div>' : '') +
  '</div>';
}

function renderMore(){
  var slice = VIEW.slice(SHOWN, SHOWN + PAGE);
  if (!slice.length){
    if (SHOWN === 0)
      document.getElementById('list').innerHTML = '<div class="empty">Nothing matches these filters.</div>';
    document.getElementById('more').style.display = 'none';
    return;
  }
  document.getElementById('list').insertAdjacentHTML('beforeend', slice.map(rowHtml).join(''));
  SHOWN += slice.length;
  document.getElementById('more').style.display = SHOWN < VIEW.length ? 'block' : 'none';
  document.getElementById('more').textContent = 'Show more (' + (VIEW.length - SHOWN) + ' left)';
}

function saveFilters(){
  try { localStorage.setItem('gl_filters', JSON.stringify(F)); } catch(e){}
}

function loadFilters(){
  try {
    var raw = localStorage.getItem('gl_filters');
    if (!raw) return;
    var s = JSON.parse(raw);
    Object.keys(F).forEach(function(k){ if (s[k] !== undefined) F[k] = s[k]; });
  } catch(e){}
}

var META = {generated_at:''};
var WIRED = false;

// Single entry point for data, whichever source it came from. The hosted build
// sets window.__GL_HOSTED__ and calls this from its auth module once the user
// is signed in; the static build calls it from boot() below.
window.__glSetData = function(data){
  ALL = (data && data.items) || [];
  META.generated_at = (data && data.generated_at) || '';

  var sel = document.getElementById('ticker');
  var have = {};
  Array.prototype.forEach.call(sel.options, function(o){ have[o.value] = 1; });
  var tickers = (data && data.tickers) || [];
  if (!tickers.length){
    tickers = ALL.map(function(i){ return i.ticker; })
      .filter(function(t, idx, arr){ return t && arr.indexOf(t) === idx; })
      .sort();
  }
  tickers.forEach(function(t){
    if (have[t]) return;
    var o = document.createElement('option');
    o.value = t; o.textContent = t; sel.appendChild(o);
  });

  if (!WIRED){ loadFilters(); wire(); WIRED = true; }
  applyFilters();
};

function wire(){
  var ids = ['q','ticker','status','minImp','sentiment','sort'];
  ids.forEach(function(id){
    var el = document.getElementById(id);
    el.value = F[id];
    var ev = (id === 'q') ? 'input' : 'change';
    el.addEventListener(ev, function(){
      F[id] = (id === 'minImp') ? parseInt(el.value,10) || 0 : el.value;
      saveFilters();
      clearTimeout(window.__t);
      window.__t = setTimeout(applyFilters, id === 'q' ? 120 : 0);
    });
  });
  document.getElementById('more').addEventListener('click', renderMore);
  document.getElementById('list').addEventListener('click', function(ev){
    var row = ev.target.closest('.row');
    if (row && !ev.target.closest('a')) row.classList.toggle('open');
  });
}

async function boot(){
  // The hosted build loads data through its own auth module instead.
  if (window.__GL_HOSTED__) return;
  var data;
  try {
    data = await loadData();
  } catch(e){
    document.getElementById('stats').textContent = 'Could not load data: ' + e.message;
    return;
  }
  window.__glSetData(data);
}

boot();
</script>
</body>
</html>
"""


def build_index(payload, embed, hosted_head=""):
    """The page HTML.

    `embed` inlines the data so file:// works with no server. `hosted_head`
    injects the Firebase bootstrap for the hosted build; when it is empty the
    page is a plain static file.
    """
    if embed:
        # Escape '<' so a title containing "</script>" cannot close the tag.
        blob = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    else:
        blob = "null"
    return PAGE.replace("__EMBEDDED_DATA__", blob).replace("__HOSTED_HEAD__", hosted_head)


HOSTED_HEAD = """<script>
  // Set BEFORE the page script runs: the hosted build gets its data from the
  // auth module below, not from news.json.
  window.__GL_HOSTED__ = true;
  window.__GL_FIREBASE_CONFIG__ = __FIREBASE_CONFIG__;
</script>
<script type="module" src="firebase-boot.js"></script>"""


def export(db_path, out_dir, embed=False, hosted=False, firebase_config=None):
    """Write the site. Returns (path, count).

    hosted=True emits the Firestore-backed page and copies firebase-boot.js
    instead of writing news.json - the data lives in Firestore behind auth.
    """
    if not os.path.exists(db_path):
        raise SystemExit(f"database not found: {db_path}")
    items = read_items(db_path)
    payload = build_payload(items)
    os.makedirs(out_dir, exist_ok=True)

    head = ""
    if hosted:
        if not firebase_config:
            raise SystemExit("--hosted needs --firebase-config=PATH (the JSON "
                             "Firebase console gives you for a web app)")
        head = HOSTED_HEAD.replace(
            "__FIREBASE_CONFIG__",
            json.dumps(firebase_config, ensure_ascii=False))
        src = os.path.join(BASE_DIR, "web", "firebase-boot.js")
        with open(src, encoding="utf-8") as fh:
            boot = fh.read()
        with open(os.path.join(out_dir, "firebase-boot.js"), "w",
                  encoding="utf-8") as fh:
            fh.write(boot)
    else:
        with open(os.path.join(out_dir, "news.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(build_index(payload, embed, hosted_head=head))
    return os.path.join(out_dir, "index.html"), len(items)


def payload_tickers(db_path):
    """Small summary line: how many tickers are represented."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        n = conn.execute("SELECT COUNT(DISTINCT ticker) FROM news").fetchone()[0]
    finally:
        conn.close()
    return f"{n} ticker(s)"


# ---------------------------------------------------------------------------
# Firestore sync
# ---------------------------------------------------------------------------
# Pushes the stored rows to Firestore so the hosted page can read them behind a
# Google sign-in. Firestore rather than a public JSON file is the whole point:
# an auth-gated page over an unprotected JSON file hides only the UI, not the
# data - anyone with the URL could still fetch the archive.
#
# Uses the Firestore REST API with a token from the GCE metadata server, so it
# needs NO key file on the VM and NO new Python dependency (this project only
# uses `requests`). A local manifest means a sync costs ZERO Firestore reads:
# only rows whose content changed are written, and only vanished ids deleted.
FS_BASE = ("https://firestore.googleapis.com/v1/projects/{project}"
           "/databases/(default)/documents")
FS_COMMIT_LIMIT = 500        # Firestore's cap per commit call
MANIFEST_NAME = "web_sync_manifest.json"


def _fs_token():
    """Access token for the VM's own service account, from the metadata server."""
    import requests
    resp = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fs_value(v):
    """Firestore REST is explicitly typed - a bare int is rejected."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    return {"stringValue": str(v)}


def _fs_fields(item):
    keys = ("ticker", "source", "lang", "title", "title_raw", "summary",
            "importance", "sentiment", "category", "pushed", "url",
            "published_at", "first_seen", "reason", "impact")
    return {k: _fs_value(item.get(k)) for k in keys}


def _row_fingerprint(item):
    import hashlib
    blob = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _manifest_path(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), MANIFEST_NAME)


def sync_firestore(db_path, project, dry_run=False):
    """Mirror news.db into Firestore. Returns (written, deleted, unchanged)."""
    items = read_items(db_path)
    manifest_file = _manifest_path(db_path)
    try:
        with open(manifest_file, encoding="utf-8") as fh:
            previous = json.load(fh)
    except Exception:
        previous = {}

    wanted, writes, unchanged = {}, [], 0
    for it in items:
        key = str(it["id"])
        fp = _row_fingerprint(it)
        wanted[key] = fp
        if previous.get(key) == fp:
            unchanged += 1
            continue
        writes.append({"update": {
            "name": f"projects/{project}/databases/(default)/documents/news/{key}",
            "fields": _fs_fields(it)}})
    deletes = [{"delete": f"projects/{project}/databases/(default)/documents/news/{k}"}
               for k in previous if k not in wanted]

    # One tiny meta doc so the page can tell whether its cache is stale with a
    # single read instead of re-fetching every row on every visit.
    meta = {"generated_at": datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "count": len(items),
            "pushed_count": sum(1 for i in items if i["pushed"])}
    writes.append({"update": {
        "name": f"projects/{project}/databases/(default)/documents/meta/status",
        "fields": {k: _fs_value(v) for k, v in meta.items()}}})

    print(f"  [firebase] {len(items)} row(s): {len(writes) - 1} to write, "
          f"{len(deletes)} to delete, {unchanged} unchanged")
    if dry_run:
        print("  [firebase] --dry-run: nothing sent.")
        return len(writes), len(deletes), unchanged

    if not writes and not deletes:
        print("  [firebase] already in sync.")
        return 0, 0, unchanged
    import requests
    token = _fs_token()
    url = FS_BASE.format(project=project) + ":commit"
    ops = writes + deletes
    sent = 0
    for start in range(0, len(ops), FS_COMMIT_LIMIT):
        chunk = ops[start:start + FS_COMMIT_LIMIT]
        resp = requests.post(url, timeout=120,
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json"},
                             json={"writes": chunk})
        if resp.status_code >= 300:
            raise SystemExit(f"  [firebase] commit failed {resp.status_code}: "
                             f"{resp.text[:400]}")
        sent += len(chunk)
        print(f"  [firebase] committed {sent}/{len(ops)} operation(s)")

    # Record the manifest only after Firestore accepted everything, so a failed
    # run retries next time instead of silently skipping those rows forever.
    with open(manifest_file, "w", encoding="utf-8") as fh:
        json.dump(wanted, fh)
    print(f"  [firebase] synced. manifest -> {manifest_file}")
    return len(writes), len(deletes), unchanged


def main():
    out_dir = None
    db_path = os.path.join(BASE_DIR, "news.db")
    embed = "--embed" in sys.argv
    project = None
    for a in sys.argv:
        if a.startswith("--out="):
            out_dir = a.split("=", 1)[1].strip()
        elif a.startswith("--db="):
            db_path = a.split("=", 1)[1].strip()
        elif a.startswith("--project="):
            project = a.split("=", 1)[1].strip()

    if "--sync-firebase" in sys.argv:
        if not project:
            print("Usage: python3 news_web.py --sync-firebase --project=PROJECT "
                  "[--db=PATH] [--dry-run]")
            sys.exit(2)
        sync_firestore(db_path, project, dry_run="--dry-run" in sys.argv)
        return

    if not out_dir:
        print("Usage: python3 news_web.py --out=DIR [--db=PATH] [--embed]")
        print("       python3 news_web.py --out=DIR --hosted --firebase-config=FILE")
        print("       python3 news_web.py --sync-firebase --project=PROJECT [--dry-run]")
        sys.exit(2)
    firebase_config = None
    for a in sys.argv:
        if a.startswith("--firebase-config="):
            with open(a.split("=", 1)[1].strip(), encoding="utf-8") as fh:
                firebase_config = json.load(fh)
    path, count = export(db_path, out_dir, embed=embed,
                         hosted="--hosted" in sys.argv,
                         firebase_config=firebase_config)
    if "--hosted" in sys.argv:
        print(f"  [web] hosted build -> {path} (+ firebase-boot.js, no news.json)")
    else:
        size = os.path.getsize(os.path.join(out_dir, "news.json"))
        print(f"  [web] exported {count} item(s) -> {path} "
              f"(+ news.json, {size // 1024} KB)")
    print(f"  [web] archive: {payload_tickers(db_path)}")


if __name__ == "__main__":
    main()
