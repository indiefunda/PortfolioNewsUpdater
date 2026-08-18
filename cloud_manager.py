#!/usr/bin/env python3
"""
PortfolioNewsUpdater - local Windows control panel.

Serves an HTML dashboard that manages the news updater on your Google Cloud
VM. It wraps the official `gcloud` CLI so you never type SSH commands.
Authentication uses Google's own OAuth login (opens in your browser).

Run it with:   python cloud_manager.py
Then open:     http://localhost:8001

This can share the same free VM as the price monitor - it just deploys the
news files and adds a separate 2x-daily cron job.
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
PORT = 8001

# VM defaults (reuse the same free VM as the price monitor)
VM_NAME = "stock-monitor"
VM_MACHINE = "e2-micro"
VM_IMAGE = "debian-12"
VM_ZONES = [
    "us-east1-b", "us-east1-c", "us-east1-d",
    "us-central1-a", "us-central1-b", "us-central1-c",
    "us-west1-a", "us-west1-b",
    "us-east4-a", "us-east4-b",
]

DEFAULT_CONFIG = {
    "tickers": [],
    "enabled": True,
    "initial_lookback_hours": 24,
    "max_items_per_run": 40,
    "max_digest_items": 10,
    "ai_provider": "deepseek",
    "ai_model": "deepseek-v4-flash",
    "ai_base_url": "https://api.deepseek.com",
    # Rolling retention: ~3 weeks of news AND dedup hashes (so recycled news
    # re-surfaces after the window; dedup still stops re-push inside it).
    "news_retention_days": 21,
    "seen_retention_days": 21,
    # "all" pushes the top-N by importance; "score" only pushes >= push_min_score.
    # Either way: nothing below push_min_importance (floor) is pushed, the AI's
    # per-item push veto is honored, and a ticker can't take more than
    # push_max_per_ticker slots while another name has news.
    "push_mode": "all",
    "push_min_score": 7,
    "push_min_importance": 4,
    "push_max_per_ticker": 3,
    # Tavily free plan = 1,000 credits/month; 1 basic search = 1 credit.
    # Daily cap 15 (~450/month worst case) + monthly hard cap 850, and a
    # ticker is skipped when free sources already covered it that run.
    "tavily_max_daily_searches": 15,
    "tavily_max_monthly_searches": 850,
    "tavily_min_free_items": 4,
    # Company lookup: re-run auto-discovery for a ticker after this many days
    # (monthly default - new subsidiaries found are alerted on Telegram).
    "lookup_refresh_days": 30,
    # China macro watch: huge policy/market news (rate cuts, stimulus,
    # assisted-loan regulation) gated FREE by regex; only matched items reach
    # the AI (one tiny batched call per run), pushed in their own digest
    # section, capped at macro_max_per_run. macro_translate=false = zero AI.
    "macro_enabled": True,
    "macro_max_per_run": 3,
    "macro_translate": True,
    "macro_keywords": [],
    # EXA neural search: semantic recall for big/impact news + subsidiary
    # discovery (free plan ~1,000/month, hard-budgeted like Tavily).
    "exa_max_daily_searches": 32,
    "exa_max_monthly_searches": 980,
    "exa_min_free_items": 6,
    "sources": {
        "sec": True,
        "google_news_zh": True,
        "google_news_site": True,  # official company websites (site: search)
        "google_news_macro": True,  # macro query (rate cuts / loan regulation)
        "eastmoney": True,
        "eastmoney_724": True,  # Eastmoney 7x24 fast-news wire (real-time tape)
        "sina_724": True,  # Sina 7x24 fast-news wire
        "exa": True,  # EXA neural per-ticker impact search
        "exa_macro": True,  # EXA neural macro search (semantic, no regex)
        "baidu": False,  # captcha-blocked from server IPs - off by default
        "tavily": True,
        "google_news_en": True,
    },
    # Per-ticker Chinese identity: the key to finding real Chinese news.
    #   name_zh         -> company's Chinese name (e.g. 乐信)
    #   name_en         -> English name (for the AI prompt)
    #   aliases_zh      -> other names the company goes by
    #   subsidiaries_zh -> brands/subsidiaries (e.g. 分期乐, 桔子理财)
    "ticker_meta": {},
    "rss_feeds": {},
}
DEFAULT_SECRETS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "ai_api_key": "",
    "tavily_api_key": "",
    "exa_api_key": "",
}


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _write_json(path, data):
    """Atomic write (tmp + os.replace) - a crash mid-write must never corrupt
    config_local.json / secrets_local.json into silently-wiped defaults."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = _read_json(CONFIG_FILE, dict(DEFAULT_CONFIG))
    merged = dict(DEFAULT_CONFIG); merged.update(cfg or {}); return merged


def load_secrets():
    sec = _read_json(SECRETS_FILE, dict(DEFAULT_SECRETS))
    merged = dict(DEFAULT_SECRETS); merged.update(sec or {}); return merged


def save_config(cfg): _write_json(CONFIG_FILE, cfg)
def save_secrets(sec): _write_json(SECRETS_FILE, sec)


# ---------------------------------------------------------------------------
# gcloud helpers
# ---------------------------------------------------------------------------
GCLOUD_CANDIDATES = [
    "gcloud",
    r"$USERPROFILE\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"$USERPROFILE\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud",
    r"C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
    r"$LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd",
]


def _find_gcloud():
    found = shutil.which("gcloud")
    if found:
        return found
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    for cand in GCLOUD_CANDIDATES:
        if cand.startswith("$LOCALAPPDATA") and localappdata:
            cand = cand.replace("$LOCALAPPDATA", localappdata)
        if cand.startswith("$USERPROFILE") and userprofile:
            cand = cand.replace("$USERPROFILE", userprofile)
        if cand and os.path.exists(cand):
            return cand
    return None


def gcloud_available():
    return _find_gcloud() is not None


def run_gcloud(args, timeout=120):
    gcloud = _find_gcloud()
    if not gcloud:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    cmd = [gcloud] + args
    try:
        # Decode as UTF-8 (gcloud may emit non-ASCII/UTF-8 bytes) and never
        # crash on a bad byte. Without this, subprocess uses the system locale
        # (e.g. cp1252 on Windows), which raises a UnicodeDecodeError and can
        # leave stdout/stderr as None, crashing callers that concatenate them.
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return proc.returncode == 0, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out."


def get_project():
    ok, out, _ = run_gcloud(["config", "get-value", "project", "--quiet"])
    if ok and out.strip() and out.strip() != "(unset)":
        return out.strip()
    ok2, out2, _ = run_gcloud(["projects", "list", "--format=value(projectId)",
                               "--limit=1", "--quiet"], timeout=60)
    if ok2 and out2.strip():
        project = out2.strip().splitlines()[0].strip()
        run_gcloud(["config", "set", "project", project, "--quiet"], timeout=60)
        return project
    return None


def auth_status():
    if not gcloud_available():
        return {"installed": False, "authed": False, "account": None}
    ok, out, _ = run_gcloud(["auth", "list", "--filter=status:ACTIVE",
                             "--format=value(account)", "--quiet"], timeout=60)
    account = out.strip() if ok and out.strip() else None
    return {"installed": True, "authed": bool(account), "account": account}


def find_vm_zone():
    project = get_project()
    if not project:
        return None
    ok, out, _ = run_gcloud(
        ["compute", "instances", "list", "--filter=name=" + VM_NAME,
         "--format=value(zone)", "--quiet"], timeout=60)
    if ok and out.strip():
        zone = out.strip().splitlines()[0].strip()
        if "/" in zone:
            zone = zone.rstrip("/").split("/")[-1]
        return zone
    return None


def vm_status():
    zone = find_vm_zone()
    if not zone:
        return None
    ok, out, _ = run_gcloud(
        ["compute", "instances", "describe", VM_NAME, "--zone", zone,
         "--format=value(status)", "--quiet"], timeout=60)
    if ok and out.strip():
        return out.strip()
    return None


def get_vm_home(zone):
    ok, home, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "echo $HOME", "--quiet"], timeout=60)
    if ok and home.strip():
        return home.strip()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"/home/{user}"


def fetch_vm_run_history():
    zone = find_vm_zone()
    if not zone:
        return []
    home = get_vm_home(zone)
    ok, out, err = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", f"cat {home}/news_run_history.json 2>/dev/null || echo '[]'",
        "--quiet"], timeout=60)
    if not ok:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_vm_cron_status():
    zone = find_vm_zone()
    if not zone:
        return None
    ok_active, active, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "systemctl is-active cron 2>/dev/null || echo 'inactive'",
        "--quiet"], timeout=60)
    cron_active = active.strip() if ok_active else "unknown"
    ok, out, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "crontab -l 2>/dev/null | grep 'news_updater.py' || true",
        "--quiet"], timeout=60)
    cron_line = out.strip() if ok else ""
    installed = bool(cron_line)
    return {
        "active": "active" if installed else "inactive",
        "cron_daemon_active": cron_active,
        "cron_line": cron_line,
    }


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PortfolioNewsUpdater — Cloud</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --border:#2a2e38; --text:#e8eaf0;
          --muted:#8b90a0; --accent:#3b82f6; --ok:#22c55e; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--text); padding:16px; }
  h1 { font-size:22px; margin:4px 0 2px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:12px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin:0 0 12px; }
  label { display:block; color:var(--muted); font-size:12px; margin:10px 0 4px; }
  input, select { width:100%; padding:12px; border-radius:8px;
                  border:1px solid var(--border); background:#12151c;
                  color:var(--text); font-size:15px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  .chip { display:inline-flex; align-items:center; gap:6px;
          background:#232733; border:1px solid var(--border);
          border-radius:20px; padding:6px 12px; font-size:14px; margin:4px; }
  .chip button { background:none; border:none; color:var(--err); font-size:16px; cursor:pointer; }
  button { border:none; border-radius:8px; padding:12px 16px; font-size:15px;
           font-weight:600; cursor:pointer; margin-top:8px; }
  .btn-primary { background:var(--accent); color:#fff; width:100%; }
  .btn-ghost { background:#232733; color:var(--text); }
  .btn-ok { background:#14532d; color:#fff; }
  .msg { display:none; padding:12px; border-radius:8px; margin-top:12px; font-size:14px; }
  .msg.ok { display:block; background:#13281a; color:var(--ok); }
  .msg.err { display:block; background:#2a1416; color:var(--err); }
  .status { padding:12px; border-radius:8px; background:#12151c; font-size:14px; margin-bottom:8px; }
  .status b { color:var(--text); }
  .status .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
  .dot.green { background:var(--ok); } .dot.red { background:var(--err); } .dot.gray { background:var(--muted); }
  .toggle { display:flex; align-items:center; justify-content:space-between; }
  .switch { position:relative; width:52px; height:30px; }
  .switch input { opacity:0; width:0; height:0; }
  .slider { position:absolute; inset:0; background:#2a2e38; border-radius:30px; transition:.3s; }
  .slider:before { content:""; position:absolute; height:22px; width:22px; left:4px; top:4px;
                   background:#fff; border-radius:50%; transition:.3s; }
  input:checked + .slider { background:var(--accent); }
  input:checked + .slider:before { transform:translateX(22px); }
  .hint { color:var(--muted); font-size:12px; margin-top:4px; }
  .log { background:#0a0c10; border:1px solid var(--border); border-radius:8px;
         padding:10px; font-family:monospace; font-size:12px; color:#9fe8a0;
         max-height:200px; overflow:auto; white-space:pre-wrap; margin-top:8px; }
  .tablewrap { overflow:auto; max-height:420px; border:1px solid var(--border);
               border-radius:8px; margin-top:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border);
           white-space:nowrap; }
  th { position:sticky; top:0; background:#12151c; color:var(--muted); font-size:12px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
  .badge.ok { background:#14532d; color:#a7f3d0; }
  .badge.err { background:#7c2d12; color:#fecaca; }
  .badge.gray { background:#2a2e38; color:#cbd5e1; }
  .empty { color:var(--muted); text-align:center; padding:24px; font-size:13px; }
</style>
</head>
<body>
  <h1>📰 PortfolioNewsUpdater — Cloud</h1>
  <div class="sub">Searches SEC (with 6-K/8-K content), Chinese news (乐信/分期乐… via Google News zh, Eastmoney, Tavily, official websites), English news and RSS — translates &amp; scores everything with AI, pushes the top items to Telegram. Runs twice a day at 9:15 ET &amp; 16:45 ET (auto-adjusts for DST).</div>
  <div class="msg" id="msg"></div>

  <div class="card">
    <h2>1. Connect to Google</h2>
    <div class="status" id="authStatus">Checking...</div>
    <button class="btn-ghost" onclick="authGoogle()">🔑 Authenticate to Google</button>
    <div class="hint">Opens Google's login page in your browser. If nothing opens, copy the printed URL.</div>
  </div>

  <div class="card">
    <h2>2. Your server</h2>
    <div class="status" id="vmStatus">—</div>
    <button class="btn-ok" onclick="createVM()">🖥️ Create/update free server</button>
    <button class="btn-ghost" onclick="refreshStatus()">↻ Refresh</button>
    <div class="hint">Uses the same free e2-micro VM as your price monitor.</div>
  </div>

  <div class="card">
    <h2>3. Configuration</h2>
    <label>Stocks</label>
    <div class="row" id="chips"></div>
    <div class="row" style="margin-top:8px">
      <input id="tickerInput" placeholder="e.g. HUIZ, AAPL" style="flex:1">
      <button class="btn-ghost" onclick="addTicker()">Add</button>
    </div>
    <label>AI provider</label>
    <select id="aiProvider" onchange="aiProviderChanged()">
      <option value="deepseek">DeepSeek (cheap, reliable)</option>
      <option value="gemini">Gemini (free tier — $0)</option>
      <option value="openai">OpenAI</option>
    </select>
    <label>AI model</label>
    <input id="aiModel" type="text" placeholder="deepseek-v4-flash">
    <label>AI base URL</label>
    <input id="aiBase" type="text" placeholder="https://api.deepseek.com">
    <label>Telegram bot token</label>
    <input id="token" type="text" placeholder="123456789:AAH...">
    <label>Telegram chat id</label>
    <input id="chatid" type="text" placeholder="e.g. 123456789">
    <label>AI API key (DeepSeek / Gemini)</label>
    <input id="aikey" type="password" placeholder="paste your key">
    <label>Tavily API key (free: 1,000 news searches/month)</label>
    <input id="tavilyKey" type="password" placeholder="paste your free Tavily key (optional)">
    <label>EXA AI key (free: ~1,000 semantic searches/month)</label>
    <input id="exaKey" type="password" placeholder="paste your free EXA key (optional)">
    <div class="row">
      <div class="status" id="tavilyUsage" style="flex:1;margin-bottom:0">Tavily usage: —</div>
      <button class="btn-ghost" onclick="loadUsage()" style="margin-top:0">↻ Check</button>
    </div>
    <label>Chinese names — ticker_meta (this is where the Chinese edge comes from)</label>
    <textarea id="tickerMeta" rows="8" style="width:100%;padding:12px;border-radius:8px;border:1px solid var(--border);background:#12151c;color:var(--text);font-family:monospace;font-size:13px;" placeholder='{"LX":{"name_zh":"乐信","name_en":"LexinFintech","aliases_zh":["乐信集团"],"subsidiaries_zh":["分期乐","桔子理财"]}}'></textarea>
    <div class="hint">Per ticker: Chinese name + aliases + subsidiary brands (e.g. LX → 分期乐). Used to search Google News zh-CN, Eastmoney, Baidu and Tavily.</div>
    <div class="row">
      <div style="flex:1">
        <label>Push mode</label>
        <select id="pushMode">
          <option value="all">All → push top-N by importance (cap below)</option>
          <option value="score">Only importance ≥ min score</option>
        </select>
      </div>
      <div style="flex:1">
        <label>Push min importance (1–10, for score mode)</label>
        <input id="pushMinScore" type="number" min="1" max="10" value="7">
      </div>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>Push floor (both modes, kills ⭐1–3 noise)</label>
        <input id="pushMinImportance" type="number" min="1" max="10" value="4">
      </div>
      <div style="flex:1">
        <label>Max items per ticker per digest</label>
        <input id="pushMaxPerTicker" type="number" min="1" value="3">
      </div>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>News retention (days, rolling)</label>
        <input id="retentionDays" type="number" min="1" value="21">
      </div>
      <div style="flex:1">
        <label>Tavily max searches/day</label>
        <input id="tavilyDaily" type="number" min="1" value="15">
      </div>
    </div>
    <div class="row">
      <div style="flex:1">
        <label>Tavily max searches/month</label>
        <input id="tavilyMonthly" type="number" min="1" value="850">
      </div>
      <div style="flex:1">
        <label>Tavily skip if free sources found ≥ (items)</label>
        <input id="tavilyMinFree" type="number" min="0" value="4">
      </div>
    </div>
    <div class="toggle" style="margin-top:14px">
      <span>Run the news updater (2x daily)</span>
      <label class="switch"><input type="checkbox" id="enabledToggle"><span class="slider"></span></label>
    </div>
    <button class="btn-primary" onclick="uploadConfig()">🚀 Upload config to server</button>
    <div class="hint">Sends your stocks, AI settings, and Telegram keys to the cloud server. Keys stay local and on your VM — never shared.</div>
  </div>

  <div class="card">
    <h2>4. Schedule & run history</h2>
    <div class="status" id="cronStatus">—</div>
    <button class="btn-ghost" onclick="loadCron()">↻ Check schedule</button>
    <button class="btn-ok" onclick="runNow()">▶ Run now (test)</button>
    <div class="log" id="log">Command output will appear here.</div>
    <div class="status" id="logStatus">—</div>
    <div id="logTableWrap"></div>
  </div>

  <div class="card">
    <h2>5. Stored news (last ~3 weeks, browsable)</h2>
    <div class="row" style="margin-bottom:8px">
      <input id="newsFilter" placeholder="Filter: ticker, category, title, source..." style="flex:1" onkeyup="renderNews()">
      <button class="btn-ghost" onclick="loadNews()">📥 Load stored news</button>
      <button class="btn-ghost" onclick="purgeJunk()" title="Delete stored, never-pushed items scored <= 2 (old filter gaps)">🧹 Purge junk</button>
    </div>
    <div class="status" id="newsStatus">Click "Load stored news" to fetch from the server.</div>
    <div id="newsTableWrap"></div>
    <div class="hint">Every item the updater found is stored here for ~3 weeks (rolling cleanup). Only the top-N by AI importance are pushed to Telegram.</div>
  </div>

  <div class="card">
    <h2>6. Company lookup (auto-discovered)</h2>
    <div class="row" style="margin-bottom:8px">
      <button class="btn-ghost" onclick="loadLookup()">📖 Load company lookup</button>
      <button class="btn-ok" onclick="rediscover()">🔍 Re-discover subsidiaries now</button>
    </div>
    <div class="status" id="lookupStatus">Shows what the updater knows about each company: Chinese names, aliases, subsidiaries (分期乐, Temu…) and their websites. New tickers are looked up and populated automatically.</div>
    <textarea id="lookupView" rows="10" readonly style="width:100%;padding:12px;border-radius:8px;border:1px solid var(--border);background:#12151c;color:var(--text);font-family:monospace;font-size:12px;"></textarea>
    <div class="hint">This grows automatically (monthly re-search per ticker; new subsidiaries are alerted on Telegram). "Re-discover now" forces it immediately — costs ~1–2 Tavily searches + 1 AI call per ticker. To override anything, edit the Chinese names JSON in Step 3 — config overrides always win.</div>
  </div>

<script>
let tickers = [];
let news = [];
function $(id){ return document.getElementById(id); }
function showMsg(t, type){ const m=$('msg'); m.textContent=t; m.className='msg '+type;
  setTimeout(()=>{ m.className='msg'; }, 8000); }

function renderChips(){ const el=$('chips'); el.innerHTML='';
  tickers.forEach(t=>{ const c=document.createElement('span'); c.className='chip';
    c.innerHTML=t+' <button onclick="removeTicker(\\''+t+'\\')">&times;</button>'; el.appendChild(c); }); }
function addTicker(){ // Sanitize: only A-Z 0-9 . - (also blocks HTML/JS injection via quotes)
  const v = $('tickerInput').value.trim().toUpperCase().replace(/[^A-Z0-9.-]/g,'');
  if(v && !tickers.includes(v)){ tickers.push(v); renderChips(); } $('tickerInput').value=''; }
function removeTicker(t){ tickers=tickers.filter(x=>x!==t); renderChips(); }

async function api(path, body){
  const opts = body ? {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)} : {};
  const r = await fetch(path, opts); return r.json();
}

// Switching the provider auto-fills the correct base URL + a sensible model
// (Gemini has a real free tier - the $0 option for sharing the app).
function aiProviderChanged(){
  const p = $('aiProvider').value;
  if(p === 'gemini'){ $('aiBase').value = 'https://generativelanguage.googleapis.com/v1beta/openai'; $('aiModel').value = 'gemini-2.0-flash'; }
  else if(p === 'openai'){ $('aiBase').value = 'https://api.openai.com/v1'; $('aiModel').value = 'gpt-4o-mini'; }
  else { $('aiBase').value = 'https://api.deepseek.com'; $('aiModel').value = 'deepseek-v4-flash'; }
}

async function load(){
  const d = await api('/api/config');
  // Sanitize on load too - a hand-edited config_local.json could otherwise
  // inject into the chip onclick handlers.
  tickers = (d.config.tickers || []).map(t => String(t).toUpperCase().replace(/[^A-Z0-9.-]/g,'')).filter(Boolean);
  $('aiProvider').value = d.config.ai_provider || 'deepseek';
  $('aiModel').value = d.config.ai_model || 'deepseek-v4-flash';
  $('aiBase').value = d.config.ai_base_url || 'https://api.deepseek.com';
  $('enabledToggle').checked = !!d.config.enabled;
  $('token').value = d.secrets.telegram_bot_token || '';
  $('chatid').value = d.secrets.telegram_chat_id || '';
  $('aikey').value = d.secrets.ai_api_key || '';
  $('tavilyKey').value = d.secrets.tavily_api_key || '';
  $('exaKey').value = d.secrets.exa_api_key || '';
  $('tickerMeta').value = JSON.stringify(d.config.ticker_meta || {}, null, 2);
  $('pushMode').value = d.config.push_mode || 'all';
  $('pushMinScore').value = d.config.push_min_score || 7;
  $('pushMinImportance').value = d.config.push_min_importance || 4;
  $('pushMaxPerTicker').value = d.config.push_max_per_ticker || 3;
  $('retentionDays').value = d.config.news_retention_days || 21;
  $('tavilyDaily').value = d.config.tavily_max_daily_searches || 15;
  $('tavilyMonthly').value = d.config.tavily_max_monthly_searches || 850;
  $('tavilyMinFree').value = d.config.tavily_min_free_items || 4;
  renderChips();
  refreshStatus(); loadCron(); loadLogs();
}

async function refreshStatus(){
  const d = await api('/api/status');
  const a = $('authStatus');
  if(!d.gcloud){ a.innerHTML='<span class="dot red"></span><b>Google CLI not installed.</b>'; }
  else if(d.auth.authed){ a.innerHTML='<span class="dot green"></span><b>Connected as:</b> '+(d.auth.account||'?'); }
  else { a.innerHTML='<span class="dot gray"></span><b>Not connected.</b>'; }
  const v = $('vmStatus');
  if(d.vm === null){ v.textContent = '— (create your free server below)'; }
  else { v.innerHTML = '<span class="dot '+(d.vm==='RUNNING'?'green':'gray')+'"></span><b>Server status:</b> '+d.vm; }
}

async function authGoogle(){ showMsg('Opening Google login in your browser...','ok');
  const d = await api('/api/auth'); showMsg(d.ok ? '✅ Connected to Google!' : '❌ '+d.error, d.ok?'ok':'err');
  refreshStatus(); }

async function createVM(){
  showMsg('Creating/updating your free server...','ok');
  const d = await api('/api/create_vm');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Server ready!' : '❌ '+d.error, d.ok?'ok':'err');
  refreshStatus(); }

async function uploadConfig(){
  showMsg('Uploading to server...','ok');
  let tickerMeta = {};
  try { tickerMeta = JSON.parse($('tickerMeta').value || '{}'); }
  catch(e){ showMsg('❌ ticker_meta is not valid JSON: '+e.message, 'err'); return; }
  const d = await api('/api/upload', {
    tickers, enabled: $('enabledToggle').checked,
    ai_provider: $('aiProvider').value, ai_model: $('aiModel').value.trim(),
    ai_base_url: $('aiBase').value.trim(),
    telegram_bot_token: $('token').value.trim(), telegram_chat_id: $('chatid').value.trim(),
    ai_api_key: $('aikey').value.trim(), tavily_api_key: $('tavilyKey').value.trim(),
    exa_api_key: $('exaKey').value.trim(),
    ticker_meta: tickerMeta,
    push_mode: $('pushMode').value, push_min_score: parseInt($('pushMinScore').value)||7,
    push_min_importance: parseInt($('pushMinImportance').value)||4,
    push_max_per_ticker: parseInt($('pushMaxPerTicker').value)||3,
    news_retention_days: parseInt($('retentionDays').value)||21,
    tavily_max_daily_searches: parseInt($('tavilyDaily').value)||15,
    tavily_max_monthly_searches: parseInt($('tavilyMonthly').value)||850,
    tavily_min_free_items: parseInt($('tavilyMinFree').value)||4,
    lookup_refresh_days: 30,
  });
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Config uploaded!' : '❌ '+d.error, d.ok?'ok':'err');
  loadCron(); loadNews(); }

async function loadUsage(){
  const d = await api('/api/tavily');
  if(!d.ok){ $('tavilyUsage').textContent = 'Usage: ❌ '+(d.error||'?'); return; }
  const u = d.usage || {};
  const tv = u.tavily || {}, ex = u.exa || {};
  const td = $('tavilyDaily').value || 15, tm = $('tavilyMonthly').value || 850;
  const ed = 32, em = 980; // EXA free-plan caps
  $('tavilyUsage').textContent = 'Tavily: '+(tv.count||0)+'/'+td+' today · '+(tv.month_count||0)+'/'+tm+' mo | EXA: '+(ex.count||0)+'/'+ed+' today · '+(ex.month_count||0)+'/'+em+' mo';
}

async function loadNews(){
  $('newsStatus').textContent = 'Fetching stored news from the server...';
  const d = await api('/api/news');
  if(!d.ok){ $('newsStatus').textContent = '❌ '+(d.error||'could not fetch news'); return; }
  news = d.news || [];
  $('newsStatus').textContent = news.length + ' stored item(s) (last ~3 weeks).';
  renderNews();
}

async function purgeJunk(){
  if(!confirm('Delete all STORED (never-pushed) items scored <= 2? This cleans out old filter-gap junk (Heineken for LX, etc.). Pushed items and higher-scored stored items are kept.')) return;
  $('newsStatus').textContent = '🧹 Purging junk on the server...';
  const d = await api('/api/purge_junk');
  $('newsStatus').textContent = d.ok ? '✅ '+d.output : '❌ '+(d.error||'failed');
  loadNews();
}

function renderNews(){
  const q = $('newsFilter').value.trim().toLowerCase();
  const rows = news.filter(n => !q || JSON.stringify(n).toLowerCase().includes(q));
  if(!rows.length){ $('newsTableWrap').innerHTML = '<div class="empty">No stored news (or filter matches nothing).</div>'; return; }
  let html = '';
  for(const n of rows){
    const title = n.title_en || n.title_raw || '';
    const imp = n.importance!=null ? '⭐'+n.importance : '—';
    const pushed = n.pushed ? '<span class="badge ok">pushed</span>' : '<span class="badge gray">stored</span>';
    const src = (n.source||'') + (n.lang==='zh' ? ' 🇨🇳' : '');
    html += '<tr><td>'+escapeHtml(n.first_seen||'')+'</td><td>'+escapeHtml(n.ticker||'')+'</td>'+
      '<td>'+escapeHtml(src)+'</td><td>'+escapeHtml(n.category||'')+'</td><td>'+imp+'</td>'+
      '<td>'+pushed+'</td><td>'+(n.url?'<a href="'+escapeHtml(n.url)+'" target="_blank">'+escapeHtml(title)+'</a>':escapeHtml(title))+'</td></tr>';
  }
  $('newsTableWrap').innerHTML = '<div class="tablewrap"><table><thead><tr><th>Seen (ET)</th><th>Ticker</th><th>Source</th><th>Cat</th><th>Imp</th><th>Status</th><th>Title (EN)</th></tr></thead><tbody>'+html+'</tbody></table></div>';
}

async function loadLookup(){
  $('lookupStatus').textContent = 'Fetching company lookup from the server...';
  const d = await api('/api/lookup');
  if(!d.ok){ $('lookupStatus').textContent = '❌ '+(d.error||'could not fetch lookup'); return; }
  $('lookupView').value = JSON.stringify(d.lookup || {}, null, 2);
  const n = Object.keys(d.lookup || {}).length;
  $('lookupStatus').textContent = n + ' company profile(s) on the server. New tickers get looked up + populated automatically on the next run.';
}

async function rediscover(){
  if(!confirm('Force a re-discovery of all tickers now? (~1-2 Tavily searches + 1 AI call per ticker; new subsidiaries will be alerted on Telegram)')) return;
  $('lookupStatus').textContent = '🔍 Re-discovering subsidiaries on the server...';
  const d = await api('/api/rediscover');
  $('lookupStatus').textContent = d.ok ? '✅ Discovery done: '+d.output : '❌ '+(d.error||'failed');
  loadLookup();
}

async function loadCron(){
  $('cronStatus').textContent = 'Checking schedule...';
  const d = await api('/api/cron');
  const c = d.cron; const el = $('cronStatus');
  if(!c){ el.innerHTML='<span class="dot gray"></span><b>Could not reach server.</b>'; return; }
  const daemon = String(c.cron_daemon_active||'').trim();
  if(c.active === 'active'){
    el.innerHTML = '<span class="dot green"></span><b>Schedule armed (2x daily, US market time).</b> Runs at 9:15 ET &amp; 16:45 ET, auto-adjusts for DST. cron: '+
      (daemon==='active'?'running':'NOT running')+'<br><span style="color:var(--muted)">'+escapeHtml(c.cron_line||'')+'</span>';
  } else {
    el.innerHTML = '<span class="dot red"></span><b>Schedule not installed.</b> Upload config (Step 3) to install it. cron: '+(daemon==='active'?'running':'NOT running');
  }
}

async function runNow(){
  showMsg('Running news updater on the server now...','ok');
  const d = await api('/api/run_now');
  $('log').textContent = d.output || '';
  showMsg(d.ok ? '✅ Ran successfully.' : '❌ '+d.error, d.ok?'ok':'err');
  loadLogs(); }

async function loadLogs(){
  $('logStatus').textContent = 'Fetching run history...';
  const d = await api('/api/logs');
  const logs = d.logs || [];
  const wrap = $('logTableWrap');
  if(!logs.length){ $('logStatus').textContent='No runs recorded yet.'; wrap.innerHTML='<div class="empty">No run history yet.</div>'; return; }
  $('logStatus').textContent = logs.length + ' run(s) recorded.';
  let rows='';
  for(const r of logs.slice(0,10)){
    const st = r.status==='ran' ? '<span class="badge ok">ran</span>' : '<span class="badge err">'+(r.status||'?')+'</span>';
    rows += '<tr><td>'+escapeHtml(r.timestamp||'—')+'</td><td>'+st+'</td>'+
      '<td>'+escapeHtml(r.tickers_checked||0)+'</td><td>'+escapeHtml(r.new_items||0)+'</td>'+
      '<td>'+escapeHtml(r.sent_items||0)+'</td><td>'+(r.duration_sec!=null?r.duration_sec+'s':'—')+'</td></tr>';
  }
  wrap.innerHTML = '<div class="tablewrap"><table><thead><tr><th>Time (ET)</th><th>Status</th><th>Tickers</th><th>New</th><th>Sent</th><th>Duration</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTML)
        elif parsed.path == "/api/config":
            self._send_json({"config": load_config(), "secrets": load_secrets()})
        elif parsed.path == "/api/status":
            self._send_json({
                "gcloud": gcloud_available(),
                "auth": auth_status(),
                "vm": vm_status(),
            })
        elif parsed.path == "/api/logs":
            self._send_json({"ok": True, "logs": fetch_vm_run_history()})
        elif parsed.path == "/api/cron":
            self._send_json({"ok": True, "cron": fetch_vm_cron_status()})
        elif parsed.path == "/api/auth":
            ok, out, err = run_gcloud(["auth", "login", "--no-launch-browser",
                                       "--brief"], timeout=300)
            self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})
        elif parsed.path == "/api/create_vm":
            self._handle_create_vm()
        elif parsed.path == "/api/run_now":
            self._handle_run_now()
        elif parsed.path == "/api/news":
            self._handle_news()
        elif parsed.path == "/api/lookup":
            self._handle_lookup()
        elif parsed.path == "/api/tavily":
            self._handle_tavily_usage()
        elif parsed.path == "/api/purge_junk":
            self._handle_purge_junk()
        elif parsed.path == "/api/rediscover":
            self._handle_rediscover()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_create_vm(self):
        project = get_project()
        if not project:
            self._send_json({"ok": False, "error":
                             "No Google Cloud project found. First open "
                             "https://console.cloud.google.com once, accept the terms and "
                             "enable billing (the Always-Free tier stays free), then come "
                             "back here and click Authenticate again."})
            return
        # Fresh projects need the Compute Engine API enabled before any
        # 'gcloud compute' call works. Idempotent; ~10-30s on first use.
        # Surface a failure instead of leaving the user with a confusing
        # create error (e.g. billing not enabled).
        ok_api, out_api, err_api = run_gcloud(
            ["services", "enable", "compute.googleapis.com",
             "--project", project, "--quiet"], timeout=120)
        api_note = ""
        if not ok_api:
            api_note = (f"  [warn] Could not enable Compute Engine API: "
                        f"{(err_api or out_api).strip()[:200]}\n")
        existing = vm_status()
        if existing:
            ok, out, err = self._deploy_to_vm(project)
            self._send_json({"ok": ok, "error": (err or "") if not ok else "",
                             "output": (f"VM already exists (status: {existing}).\n" + out + err)})
            return
        ok, out, err = False, api_note, ""
        created_zone = None
        for zone in VM_ZONES:
            ok, out, err = run_gcloud([
                "compute", "instances", "create", VM_NAME,
                "--zone", zone, "--machine-type", VM_MACHINE,
                "--image-family", VM_IMAGE, "--image-project", "debian-cloud",
                "--boot-disk-size", "10GB", "--tags", "http-server", "--quiet",
            ], timeout=300)
            if ok:
                created_zone = zone
                break
            if "ZONE_RESOURCE_POOL_EXHAUSTED" not in err and "resource_availability" not in err:
                break
        if ok:
            ok2, out2, err2 = self._deploy_to_vm(project)
            ok = ok2; out += out2; err += err2
            if created_zone:
                out = f"(created in zone {created_zone})\n" + out
        self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})

    def _deploy_to_vm(self, project):
        zone = find_vm_zone()
        if not zone:
            return False, "", "VM not found."
        home = get_vm_home(zone)
        out, err = "", ""
        files = ["news_updater.py", "requirements.txt", "setup_cloud.sh",
                 "config_local.json", "secrets_local.json"]
        for f in files:
            src = os.path.join(BASE_DIR, f)
            if os.path.exists(src):
                ok, o, e = run_gcloud([
                    "compute", "scp", "--zone", zone, src,
                    f"{VM_NAME}:{home}/", "--quiet"], timeout=120)
                out += o; err += e
                if not ok:
                    return False, out, err
        ok, o, e = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", "cd ~ && bash setup_cloud.sh", "--quiet"], timeout=300)
        out += o; err += e
        return ok, out, err

    def _handle_run_now(self):
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --force 2>&1",
            "--quiet"], timeout=600)
        self._send_json({"ok": ok, "error": (err or "") if not ok else "", "output": out + err})

    def _handle_news(self):
        """Fetch the stored news DB from the VM via the updater's --dump-news mode."""
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --dump-news 2>/dev/null",
            "--quiet"], timeout=90)
        if not ok:
            self._send_json({"ok": False, "error": (err or "SSH failed").strip()[:400]})
            return
        try:
            data = json.loads(out)
            self._send_json({"ok": True, "news": data if isinstance(data, list) else []})
        except Exception:
            self._send_json({"ok": False, "error": "Could not parse stored news from the server."})

    def _handle_lookup(self):
        """Fetch the auto-grown company lookup from the VM (--dump-lookup)."""
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --dump-lookup 2>/dev/null || echo '{{}}'",
            "--quiet"], timeout=90)
        if not ok:
            self._send_json({"ok": False, "error": (err or "SSH failed").strip()[:400]})
            return
        try:
            data = json.loads(out)
            self._send_json({"ok": True, "lookup": data if isinstance(data, dict) else {}})
        except Exception:
            self._send_json({"ok": False, "error": "Could not parse company lookup from the server."})

    def _handle_tavily_usage(self):
        """Fetch the Tavily + EXA usage trackers from the VM (--dump-usage)."""
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --dump-usage 2>/dev/null || echo '{{}}'",
            "--quiet"], timeout=60)
        if not ok:
            self._send_json({"ok": False, "error": (err or "SSH failed").strip()[:400]})
            return
        try:
            data = json.loads(out)
            if "tavily" in data and "exa" in data:
                self._send_json({"ok": True, "usage": data})
            else:
                # Old layout (single dict) -> wrap it
                self._send_json({"ok": True, "usage": {"tavily": data, "exa": {}}})
        except Exception:
            self._send_json({"ok": False, "error": "Could not parse usage from the server."})

    def _handle_rediscover(self):
        """Force a full company re-discovery on the VM (--rediscover)."""
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --rediscover 2>&1",
            "--quiet"], timeout=600)
        tail = (out + err).strip().splitlines()
        tail = tail[-15:] if len(tail) > 15 else tail
        self._send_json({"ok": ok, "error": (err or "") if not ok else "",
                         "output": "\n".join(tail)})

    def _handle_purge_junk(self):
        """Delete stored junk (never-pushed, importance<=2) on the VM."""
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        home = get_vm_home(zone)
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --purge-junk 2>&1",
            "--quiet"], timeout=120)
        tail = (out + err).strip().splitlines()
        tail = tail[-10:] if len(tail) > 10 else tail
        self._send_json({"ok": ok, "error": (err or "") if not ok else "",
                         "output": "\n".join(tail)})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            cfg = load_config(); secrets = load_secrets()
            # Sanitize tickers (A-Z 0-9 . -) - also blocks HTML/JS injection.
            cfg["tickers"] = [
                ''.join(c for c in t.strip().upper() if c.isascii() and (c.isalnum() or c in '.-'))
                for t in data.get("tickers", []) if t.strip()]
            cfg["enabled"] = bool(data.get("enabled", True))
            # Only update keys the panel actually sends - this preserves any
            # hand-tuned values (initial_lookback_hours, max_items_per_run,
            # seen_retention_days, lookup_refresh_days, ...) instead of
            # silently resetting them on every upload.
            for key, cast in (("initial_lookback_hours", int), ("max_items_per_run", int),
                              ("max_digest_items", int), ("lookup_refresh_days", int)):
                if key in data:
                    try:
                        cfg[key] = cast(data[key])
                    except (TypeError, ValueError):
                        pass
            if "seen_retention_days" in data:
                try:
                    cfg["seen_retention_days"] = max(int(data.get("news_retention_days", 21)),
                                                     int(data["seen_retention_days"]))
                except (TypeError, ValueError):
                    pass
            cfg["ai_provider"] = data.get("ai_provider", cfg.get("ai_provider", "deepseek"))
            cfg["ai_model"] = data.get("ai_model", cfg.get("ai_model", "deepseek-v4-flash"))
            cfg["ai_base_url"] = data.get("ai_base_url", cfg.get("ai_base_url", "https://api.deepseek.com"))
            cfg["ticker_meta"] = data.get("ticker_meta", cfg.get("ticker_meta", {})) or {}
            cfg["push_mode"] = data.get("push_mode", cfg.get("push_mode", "all"))
            for key, default in (("push_min_score", 7), ("push_min_importance", 4),
                                 ("push_max_per_ticker", 3), ("news_retention_days", 21),
                                 ("tavily_max_daily_searches", 15),
                                 ("tavily_max_monthly_searches", 850),
                                 ("tavily_min_free_items", 4)):
                if key in data:
                    try:
                        cfg[key] = int(data[key])
                    except (TypeError, ValueError):
                        pass
            secrets["telegram_bot_token"] = data.get("telegram_bot_token", "")
            secrets["telegram_chat_id"] = data.get("telegram_chat_id", "")
            secrets["ai_api_key"] = data.get("ai_api_key", "")
            secrets["tavily_api_key"] = data.get("tavily_api_key", "")
            secrets["exa_api_key"] = data.get("exa_api_key", "")
            save_config(cfg); save_secrets(secrets)
            project = get_project()
            if not project:
                self._send_json({"ok": False, "error":
                                 "No Google Cloud project found. First open "
                                 "https://console.cloud.google.com once, accept the terms and "
                                 "enable billing (the Always-Free tier stays free), then click "
                                 "Authenticate again."})
                return
            ok, out, err = self._deploy_to_vm(project)
            self._send_json({"ok": ok, "error": err or ("" if ok else out), "output": out + err})
        else:
            self._send_json({"error": "not found"}, 404)


def main():
    if not gcloud_available():
        print("=" * 50)
        print(" gcloud is not installed.")
        print(" Install it: https://cloud.google.com/sdk/docs/install")
        print("=" * 50)
        return
    port = PORT
    # Bind only to loopback so the panel (which serves API keys) is never
    # reachable from other machines on the network.
    HOST = "127.0.0.1"
    try:
        server = ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        port = 8002
        server = ThreadingHTTPServer((HOST, port), Handler)
    print(f"PortfolioNewsUpdater panel: http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
