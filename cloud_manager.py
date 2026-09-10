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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_local.json")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_local.json")
PORT = 8001
# Written at startup with the URL the panel actually bound to, so the launcher
# opens THIS panel rather than whatever else is listening on the default port.
PANEL_URL_FILE = "panel_url.txt"

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
    # per-item push veto is honored, and seats are handed out round-robin per
    # ticker (max push_max_per_ticker each) so one busy name can't fill the
    # digest while another name with real news gets nothing.
    "push_mode": "all",
    "push_min_score": 7,
    "push_min_importance": 4,
    "push_max_per_ticker": 2,
    # Nothing published longer ago than this is ever pushed (stored only), no
    # matter how the AI scored it - this is the "I already read this days ago"
    # guard for recycled coverage. Regulatory items are exempt. 0 disables.
    "push_max_age_hours": 72,
    # Event-level guard: a corporate event (an earnings release for a given
    # fiscal period, an EGM, a dividend) is pushed ONCE. Every later article
    # about that same event - another outlet, a translated headline - is
    # suppressed for this many days. 0 disables.
    "event_repeat_window_days": 7,
    # Optional extra sections (both OFF by default).
    #   sector_watch   - industry news that never mentions the company, shown
    #                    in its own 🏭 SECTOR CONTEXT section instead of being
    #                    mixed into the per-stock items.
    #   global_markets - systemic US/global items (Fed, CPI, payrolls) in their
    #                    own 🌍 GLOBAL MARKETS section, kept apart from
    #                    CHINA MACRO. A daily index recap is scored low and
    #                    dropped.
    "sector_watch": False,
    "sector_watch_max_per_run": 2,
    "global_markets": False,
    "global_markets_max_per_run": 2,
    # Tavily free plan = 1,000 credits/month; 1 basic search = 1 credit.
    # Daily cap 30 (~900/month worst case) + monthly hard cap 900, and a
    # ticker is skipped when free sources already covered it that run.
    "tavily_max_daily_searches": 30,
    "tavily_max_monthly_searches": 900,
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
# The secret fields the panel round-trips. Placeholder sent to the browser
# instead of the real value; if an upload echoes it back, the stored key is kept.
SECRET_KEYS = tuple(DEFAULT_SECRETS.keys())
SECRET_MASK = "__KEEP__"

# Endpoints that CHANGE something. They are POST-only and Origin-checked so a
# random web page you visit cannot trigger them with <img src="..."> or a form.
POST_ONLY_API = ("/api/upload", "/api/delete_news", "/api/auth_code")
# Endpoints kept for backwards compatibility with older panel builds that issue
# them as GET; they are refused unless the request is same-origin (see
# _origin_ok), which is what actually blocks the CSRF/DNS-rebinding vector.
MUTATING_API = ("/api/auth", "/api/create_vm", "/api/run_now", "/api/purge_junk",
                "/api/rediscover")


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


# Serialises read-modify-write of config/secrets. ThreadingHTTPServer serves
# each request in its own thread, so two Upload clicks could interleave.
CONFIG_LOCK = threading.Lock()


def _write_json(path, data):
    """
    Atomic write: a uniquely-named temp file in the SAME directory, then
    os.replace.

    The old version used a fixed `path + ".tmp"`: two concurrent writers opened
    and truncated the same file, so one could publish the other's half-written
    JSON, and the loser raised FileNotFoundError on replace. Because _read_json
    swallows parse errors and returns the defaults, that corruption showed up as
    a silently EMPTY panel - which the next upload then pushed to the VM.
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".",
                                   prefix=os.path.basename(path) + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


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


def run_gcloud(args, timeout=120, stdin_devnull=False):
    """
    Run gcloud and capture its output.

    `stdin_devnull` detaches stdin (DEVNULL). Commands that would otherwise stop
    and wait for interactive input - `gcloud auth login` asking for a
    verification code - then fail fast with a clear message instead of hanging
    until the timeout with no output for the user to act on.
    """
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
            stdin=subprocess.DEVNULL if stdin_devnull else None,
        )
        return proc.returncode == 0, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return False, "", "gcloud not found. Install the Google Cloud CLI."
    except subprocess.TimeoutExpired as exc:
        # Return whatever gcloud managed to print before it blocked. This is
        # how the `gcloud auth login` URL is captured: gcloud prints it and
        # then waits for a verification code that only the browser knows.
        partial_out = exc.stdout or ""
        partial_err = exc.stderr or ""
        if isinstance(partial_out, bytes):
            partial_out = partial_out.decode("utf-8", "replace")
        if isinstance(partial_err, bytes):
            partial_err = partial_err.decode("utf-8", "replace")
        return False, partial_out, (partial_err
                                    or "Command timed out (still waiting?).")
    except OSError as exc:
        # e.g. the interpreter could not spawn the process at all
        return False, "", f"Could not run gcloud: {exc}"


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


def ensure_vm_running(zone):
    """
    Make sure the VM is RUNNING before we try to SSH into it.

    A stopped instance reports TERMINATED; every SSH then fails with a raw
    gcloud error, which reads as a broken panel instead of "your server is
    switched off". Starting it takes ~20s and is a no-op when it is already up.
    Returns (ok, message).
    """
    status = (vm_status() or "").upper()
    if not status:
        return False, "Could not determine the server status."
    if status == "RUNNING":
        return True, ""
    if status in ("TERMINATED", "STOPPED", "SUSPENDED"):
        print(f"  server is {status} - starting it...")
        ok, out, err = run_gcloud(
            ["compute", "instances", "start", VM_NAME, "--zone", zone, "--quiet"],
            timeout=180)
        if not ok:
            return False, ("The server is %s and could not be started: %s"
                           % (status, (err or out).strip()[:300]))
        status = (vm_status() or "").upper()
        if status != "RUNNING":
            return False, ("The server was started but is still %s - try again in a "
                           "minute." % status)
        return True, ""
    if status in ("STAGING", "PROVISIONING", "REPAIRING"):
        return False, ("The server is %s - give it a minute and try again." % status)
    return True, ""


def get_vm_home(zone):
    ok, home, _ = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", "echo $HOME", "--quiet"], timeout=60)
    if ok and home.strip():
        return home.strip()
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"/home/{user}"


def fetch_vm_run_history():
    """Return (logs, error). A missing list and an unreachable VM are NOT the
    same thing, and the old version collapsed both into an empty list, so the UI
    could not tell "never ran" from "cannot reach the server"."""
    zone = find_vm_zone()
    if not zone:
        return [], "No server found."
    home = get_vm_home(zone)
    ok, out, err = run_gcloud([
        "compute", "ssh", "--zone", zone, VM_NAME,
        "--command", f"cat {home}/news_run_history.json 2>/dev/null || echo '[]'",
        "--quiet"], timeout=60)
    if not ok:
        return [], (err or out or "Could not read the run history.").strip()[:300]
    try:
        data = json.loads(out)
        return (data if isinstance(data, list) else []), ""
    except Exception:
        return [], "The run history on the server is not valid JSON."


def fetch_vm_cron_status():
    """
    Report what is actually scheduled.

    Two things the old version got wrong: it only inspected the USER crontab
    (so a working /etc/cron.d install read as "not installed"), and it treated
    the mere presence of a line as "armed" even when the cron daemon was dead.
    """
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
        "--command", ("crontab -l 2>/dev/null | grep -F news_updater; "
                      "sudo cat /etc/cron.d/news-summer /etc/cron.d/news-winter "
                      "2>/dev/null | grep -F news_updater; true"),
        "--quiet"], timeout=60)
    cron_line = out.strip() if ok else ""
    installed = bool(cron_line)
    daemon_ok = cron_active == "active"
    return {
        # "active" only when jobs exist AND the daemon can run them.
        "active": "active" if (installed and daemon_ok) else "inactive",
        "installed": installed,
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
  /* Links: the browser default (#0000EE) and :visited (#551A8B) are almost
     invisible on this dark theme - always specify both. */
  a, a:visited { color:#7db1ff; text-decoration:none; }
  a:hover { color:#a5c8ff; text-decoration:underline; }
  /* Per-row delete button (stored-news table) */
  .btn-x { background:transparent; border:1px solid var(--border); color:var(--muted);
           border-radius:6px; padding:1px 7px; font-size:13px; line-height:1.3;
           cursor:pointer; }
  .btn-x:hover { background:#7c2d12; border-color:#b45309; color:#fff; }
  .btn-x[disabled] { opacity:.45; cursor:default; }
</style>
</head>
<body>
  <h1>📰 PortfolioNewsUpdater — Cloud</h1>
  <div class="sub">Searches SEC (with 6-K/8-K content), Chinese news (乐信/分期乐… via Google News zh, Eastmoney, Tavily, official websites), English news and RSS — translates &amp; scores everything with AI, pushes the top items to Telegram. Runs twice a day at 9:15 ET (pre-open) &amp; 17:00 ET (post-close), auto-adjusted for DST.</div>
  <div class="msg" id="msg"></div>

  <div class="card">
    <h2>1. Connect to Google</h2>
    <div class="status" id="authStatus">Checking...</div>
    <button class="btn-ghost" onclick="authGoogle()">🔑 Authenticate to Google</button>
    <div id="authFlow" style="display:none;margin-top:10px">
      <div class="hint">1. Open this URL if your browser did not open it:</div>
      <input id="authUrl" readonly style="width:100%;font-size:11px;font-family:monospace">
      <div class="row" style="margin-top:8px">
        <input id="authCode" placeholder="2. Paste the verification code Google shows you" style="flex:1">
        <button class="btn-ok" onclick="submitAuthCode()">✔ Finish login</button>
      </div>
    </div>
    <div class="hint">Google asks you to copy a code back here. Nothing to type if the browser opened it and you already authorized.</div>
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
      <div class="status" id="tavilyUsage" style="flex:1;margin-bottom:0">Search budget — Tavily | EXA: —</div>
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
    <div class="row">
      <button class="btn-ghost" onclick="loadCron()">↻ Check schedule</button>
      <button class="btn-ok" onclick="runNow(false)">▶ Run now (test)</button>
      <button class="btn-ok" onclick="runNow(true)" title="Real run; if nothing is NEW it sends a 📊 snapshot of the current picture instead of nothing">📊 Run now + snapshot</button>
    </div>
    <div class="hint">Both buttons run the REAL updater (fetch + AI + Telegram). If nothing is new since the last run, no digest is sent (dedup) — the snapshot button always delivers the current picture.</div>
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
  // The server never sends real secrets any more - only a "configured" mask.
  // If the field is left untouched we send the mask back, which tells the
  // server to KEEP the stored key; typing a value replaces it.
  fillSecret('token', d.secrets.telegram_bot_token);
  fillSecret('chatid', d.secrets.telegram_chat_id);
  fillSecret('aikey', d.secrets.ai_api_key);
  fillSecret('tavilyKey', d.secrets.tavily_api_key);
  fillSecret('exaKey', d.secrets.exa_api_key);
  $('tickerMeta').value = JSON.stringify(d.config.ticker_meta || {}, null, 2);
  $('pushMode').value = d.config.push_mode || 'all';
  // Defaults come from the server's DEFAULT_CONFIG, so the three places that
  // used to disagree (2 vs 3, 30 vs 15, 900 vs 850) can no longer drift.
  const dflt = d.defaults || {};
  $('pushMinScore').value = d.config.push_min_score || dflt.push_min_score || 7;
  $('pushMinImportance').value = d.config.push_min_importance || dflt.push_min_importance || 4;
  $('pushMaxPerTicker').value = d.config.push_max_per_ticker || dflt.push_max_per_ticker || 2;
  $('retentionDays').value = d.config.news_retention_days || dflt.news_retention_days || 21;
  $('tavilyDaily').value = d.config.tavily_max_daily_searches || dflt.tavily_max_daily_searches || 30;
  $('tavilyMonthly').value = d.config.tavily_max_monthly_searches || dflt.tavily_max_monthly_searches || 900;
  $('tavilyMinFree').value = d.config.tavily_min_free_items || dflt.tavily_min_free_items || 4;
  window._exaDaily = d.config.exa_max_daily_searches || dflt.exa_max_daily_searches || 32;
  window._exaMonthly = d.config.exa_max_monthly_searches || dflt.exa_max_monthly_searches || 980;
  // These have no input in the form, but they are real settings: carry the
  // current values through the upload so they are not silently reset.
  window._lookback = d.config.initial_lookback_hours || dflt.initial_lookback_hours || 24;
  window._maxItems = d.config.max_items_per_run || dflt.max_items_per_run || 40;
  window._maxDigest = d.config.max_digest_items || dflt.max_digest_items || 10;
  renderChips();
  refreshStatus(); loadCron(); loadLogs(); loadUsage();
}

// A secret input: show a placeholder when one is stored, and mark it so save()
// knows the user did not replace it.
const SECRET_MASK = '__KEEP__';
function fillSecret(id, value){
  const el = $(id);
  el.value = (value === SECRET_MASK) ? '' : (value || '');
  el.placeholder = (value === SECRET_MASK) ? '•••••••• (saved - type to replace)' : '';
  el.dataset.saved = (value === SECRET_MASK) ? '1' : '';
}
function secretValue(id){
  const el = $(id);
  const v = el.value.trim();
  if(!v && el.dataset.saved) return SECRET_MASK;   // untouched -> keep stored key
  return v;                                        // typed, or deliberately cleared
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

async function authGoogle(){
  showMsg('Starting Google login...','ok');
  $('authFlow').style.display = 'none';
  const d = await api('/api/auth', {});
  if(d.authed){ showMsg('✅ Already connected'+(d.account?' as '+d.account:''),'ok'); refreshStatus(); return; }
  if(d.url){
    $('authUrl').value = d.url;
    $('authFlow').style.display = 'block';
    showMsg('Open the URL below, then paste the code Google gives you.','ok');
  } else {
    showMsg('❌ Could not start the login'+(d.output?': '+d.output.slice(0,160):''),'err');
  }
}

async function submitAuthCode(){
  const code = ($('authCode').value || '').trim();
  if(!code){ showMsg('Paste the verification code first.','err'); return; }
  showMsg('Finishing login...','ok');
  const d = await api('/api/auth_code', {code});
  if(d.ok){ showMsg('✅ Connected'+(d.account?' as '+d.account:''),'ok'); $('authFlow').style.display='none'; }
  else { showMsg('❌ '+(d.error||'login failed'),'err'); }
  refreshStatus();
}

async function createVM(){
  showMsg('Creating/updating your free server...','ok');
  const d = await api('/api/create_vm', {});
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
    telegram_bot_token: secretValue('token'), telegram_chat_id: secretValue('chatid'),
    ai_api_key: secretValue('aikey'), tavily_api_key: secretValue('tavilyKey'),
    exa_api_key: secretValue('exaKey'),
    ticker_meta: tickerMeta,
    push_mode: $('pushMode').value, push_min_score: parseInt($('pushMinScore').value)||7,
    push_min_importance: parseInt($('pushMinImportance').value)||4,
    push_max_per_ticker: parseInt($('pushMaxPerTicker').value)||2,
    news_retention_days: parseInt($('retentionDays').value)||21,
    tavily_max_daily_searches: parseInt($('tavilyDaily').value)||30,
    tavily_max_monthly_searches: parseInt($('tavilyMonthly').value)||900,
    tavily_min_free_items: parseInt($('tavilyMinFree').value)||4,
    initial_lookback_hours: window._lookback || undefined,
    max_items_per_run: window._maxItems || undefined,
    max_digest_items: window._maxDigest || undefined,
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
  const ed = window._exaDaily || 32, em = window._exaMonthly || 980; // EXA caps (from your config)
  $('tavilyUsage').textContent = 'Tavily: '+(tv.count||0)+'/'+td+' today · '+(tv.month_count||0)+'/'+tm+' mo ('+(tm?Math.round(100*(tv.month_count||0)/tm):0)+'% used) | EXA: '+(ex.count||0)+'/'+ed+' today · '+(ex.month_count||0)+'/'+em+' mo ('+(em?Math.round(100*(ex.month_count||0)/em):0)+'% used)';
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
  const d = await api('/api/purge_junk', {});
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
    // Index into the FULL array, so deletion targets the right row even while
    // a filter is active and the table is only showing a subset.
    const idx = news.indexOf(n);
    html += '<tr><td><button class="btn-x" title="Delete this item from the stored news" '+
      'onclick="deleteNews('+idx+')">✕</button></td>'+
      '<td>'+escapeHtml(n.first_seen||'')+'</td><td>'+escapeHtml(n.ticker||'')+'</td>'+
      '<td>'+escapeHtml(src)+'</td><td>'+escapeHtml(n.category||'')+'</td><td>'+imp+'</td>'+
      '<td>'+pushed+'</td><td>'+(n.url?'<a href="'+escapeHtml(n.url)+'" target="_blank" rel="noopener">'+escapeHtml(title)+'</a>':escapeHtml(title))+'</td></tr>';
  }
  $('newsTableWrap').innerHTML = '<div class="tablewrap"><table><thead><tr><th></th><th>Seen (ET)</th><th>Ticker</th><th>Source</th><th>Cat</th><th>Imp</th><th>Status</th><th>Title (EN)</th></tr></thead><tbody>'+html+'</tbody></table></div>';
}

async function deleteNews(idx){
  const n = news[idx];
  if(!n) return;
  const title = (n.title_en || n.title_raw || '').slice(0, 90);
  // NOTE: this panel is a Python triple-quoted string, so newline ESCAPES in
  // it become real newlines in the served JavaScript and break the script.
  // Use <br> instead - Chrome renders it as a line break in confirm dialogs.
  let msg = 'Delete this stored item?<br><br>'+title+'<br><br>'+
            (n.ticker||'')+' - '+(n.source||'')+' - '+(n.first_seen||'');
  if(n.pushed) msg += '<br><br>It was already PUSHED to Telegram. This also clears the '+
                      'dedup memory for it, so the same story can reach you again '+
                      'if it is re-reported.';
  else msg += '<br><br>It was never pushed, so this only removes it from the browser view.';
  if(!confirm(msg)) return;
  $('newsStatus').textContent = 'Deleting item on the server...';
  const d = await api('/api/delete_news', {ticker:n.ticker, source:n.source,
                                           item_hash:n.item_hash, pushed: n.pushed?1:0});
  if(d.ok){
    news.splice(idx, 1);          // keep the local copy in sync
    $('newsStatus').textContent = '✅ Deleted. '+news.length+' stored item(s) left.';
    renderNews();
  } else {
    $('newsStatus').textContent = '❌ '+(d.error||'delete failed');
  }
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
  const d = await api('/api/rediscover', {});
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
    el.innerHTML = '<span class="dot green"></span><b>Schedule armed (2x daily, US market time).</b> Runs at 9:15 ET (15 min before the open) and 17:00 ET (1 hour after the close). Both are outside DeepSeek peak pricing; auto-adjusts for DST.<br><span style="color:var(--muted)">'+escapeHtml(c.cron_line||'')+'</span>';
  } else if(c.installed && daemon !== 'active'){
    // Jobs exist but cron cannot run them - say so instead of "not installed".
    el.innerHTML = '<span class="dot red"></span><b>Schedule installed but the cron daemon is '+escapeHtml(daemon||'not running')+'.</b> Fix on the server: <code>sudo systemctl enable --now cron</code><br><span style="color:var(--muted)">'+escapeHtml(c.cron_line||'')+'</span>';
  } else {
    el.innerHTML = '<span class="dot red"></span><b>Schedule not installed.</b> Upload config (Step 3) to install it. cron: '+escapeHtml(daemon||'unknown');
  }
}

async function runNow(withSnapshot){
  showMsg('Running the real updater on the server now...','ok');
  const d = await api('/api/run_now' + (withSnapshot ? '?snapshot=1' : ''), {snapshot: withSnapshot?1:0});
  $('log').textContent = d.output || '';
  // Reflect what actually happened instead of a generic "test works" message.
  const out = d.output || '';
  let msg = '';
  if(out.includes('Digest sent') || out.includes('snapshot sent')) msg = '✅ Real run done - Telegram message sent.';
  else if(out.includes('No items to push') || out.includes('No new items') || out.includes('nothing to do') || out.includes('nothing sent')) msg = 'ℹ️ Run done - nothing NEW to send (dedup). ' + (withSnapshot ? 'No notable news in the last 24h.' : 'Try 📊 Run now + snapshot for the current picture.');
  else msg = d.ok ? '✅ Real run completed (see log).' : '❌ '+(d.error||'run failed');
  showMsg(msg, d.ok?'ok':'err');
  loadLogs(); }

async function loadLogs(){
  $('logStatus').textContent = 'Fetching run history...';
  const d = await api('/api/logs');
  const logs = d.logs || [];
  const wrap = $('logTableWrap');
  // "No runs yet" and "cannot reach the server" used to look identical.
  if(d.error){ $('logStatus').textContent = '❌ '+d.error; wrap.innerHTML='<div class="empty">'+escapeHtml(d.error)+'</div>'; return; }
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # the browser navigated away mid-response; not an error

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _origin_ok(self):
        """
        Reject requests that did not come from the panel itself.

        Binding to 127.0.0.1 does NOT protect a local server from the browser:
        a page on evil.com can point its own hostname at 127.0.0.1
        (DNS rebinding), at which point it is same-origin and can both read
        /api/* and POST to it. So every request must present a loopback Host
        header, and any Origin/Referer that IS present must be the panel's own
        origin. A plain `fetch` from the panel sends no Origin for same-origin
        GETs but does send it for POSTs; a cross-site page always sends one.
        """
        def host_of(value):
            try:
                return (urlparse(value).hostname or "").lower()
            except Exception:
                return ""

        host = str(self.headers.get("Host") or "")
        host = host.split(":")[0].strip().lower()
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            self._send_json({"ok": False, "error":
                             "Refused: this endpoint only answers requests addressed "
                             "to localhost."}, 403)
            return False
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if value and host_of(value) not in ("127.0.0.1", "localhost", "::1", ""):
                self._send_json({"ok": False, "error":
                                 "Refused: cross-site request blocked (Origin %s)."
                                 % host_of(value)}, 403)
                return False
        return True

    def _read_json_body(self):
        """Read+parse a JSON body, returning (data, error_message)."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None, "Bad Content-Length header."
        if length < 0 or length > 8 * 1024 * 1024:
            return None, "Request body missing or too large."
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return (json.loads(raw) if raw.strip() else {}), None
        except Exception as exc:
            return None, "Could not parse the request body as JSON (%s)." % exc

    def do_GET(self):
        parsed = urlparse(self.path)
        if not self._origin_ok():
            return
        if parsed.path in POST_ONLY_API:
            # Mutating endpoints must not be reachable by a plain GET/<img>.
            self._send_json({"ok": False, "error":
                             "This endpoint requires POST."}, 405)
            return
        if parsed.path in MUTATING_API and self.headers.get("Origin"):
            # Keep old panel builds working, but never from a foreign origin.
            self._send_json({"ok": False, "error":
                             "Cross-origin request blocked. Reload the panel."}, 403)
            return
        if parsed.path in ("/", "/index.html"):
            self._send_html(HTML)
        elif parsed.path == "/api/config":
            # NEVER return the real secrets. This endpoint is readable by any
            # page that can reach the panel (DNS rebinding makes 127.0.0.1
            # reachable from a remote origin), so the response only says WHICH
            # keys are set. The form shows them masked; /api/upload keeps a key
            # that the request omits or echoes back as the mask.
            secrets = load_secrets()
            masked = {}
            for key in SECRET_KEYS:
                value = str(secrets.get(key) or "")
                masked[key] = SECRET_MASK if value else ""
            self._send_json({"config": load_config(), "secrets": masked,
                             "secrets_masked": True,
                             # Single source of truth for the form's fallbacks.
                             "defaults": DEFAULT_CONFIG})
        elif parsed.path == "/api/status":
            self._send_json({
                "gcloud": gcloud_available(),
                "auth": auth_status(),
                "vm": vm_status(),
            })
        elif parsed.path == "/api/logs":
            logs, logs_error = fetch_vm_run_history()
            self._send_json({"ok": not logs_error, "logs": logs,
                             "error": logs_error})
        elif parsed.path == "/api/cron":
            self._send_json({"ok": True, "cron": fetch_vm_cron_status()})
        elif parsed.path == "/api/news":
            self._handle_news()
        elif parsed.path == "/api/lookup":
            self._handle_lookup()
        elif parsed.path == "/api/tavily":
            self._handle_tavily_usage()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_auth(self):
        """
        Start `gcloud auth login` and return the URL to the PANEL.

        The old flow used `--no-launch-browser --brief` and captured the URL
        into a pipe while rendering only ok/error, so the URL was never shown:
        clicking Authenticate opened nothing, printed nothing, and blocked until
        the 300s timeout with no way forward.

        Now the URL comes back to the browser (and we try to open it too), and
        the panel posts the verification code to /api/auth_code, which is what
        actually completes the login. gcloud is expected to block waiting for
        stdin, so `run_gcloud` returns the output it printed before timing out.
        """
        url = None
        ok, out, err = run_gcloud(
            ["auth", "login", "--no-launch-browser", "--brief"], timeout=25)
        text = ((out or "") + "\n" + (err or "")).strip()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("https://"):
                url = line
                break
        if url:
            try:
                if os.name == "nt":
                    os.startfile(url)                       # noqa: S606
                else:
                    subprocess.Popen(["xdg-open", url],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            except Exception:
                pass   # the URL is shown in the panel either way
        auto = auth_status()
        self._send_json({"ok": True, "url": url,
                         "authed": bool(auto.get("authed")),
                         "account": auto.get("account"),
                         "output": text[-800:]})

    def _handle_auth_code(self):
        """Finish `gcloud auth login` with the code the browser displayed."""
        data, error = self._read_json_body()
        if error:
            self._send_json({"ok": False, "error": error}, 400)
            return
        code = str((data or {}).get("code") or "").strip()
        if not code:
            self._send_json({"ok": False, "error": "No verification code given."}, 400)
            return
        # A code is a short opaque token. Validate it here because it becomes a
        # process argument (never passed through a shell).
        if not re.fullmatch(r"[A-Za-z0-9_/+\-=]{4,256}", code):
            self._send_json({"ok": False, "error":
                             "That does not look like a verification code."}, 400)
            return
        ok, out, err = run_gcloud(
            ["auth", "login", "--no-launch-browser", "--brief", code],
            timeout=120, stdin_devnull=True)
        status = auth_status()
        self._send_json({"ok": bool(ok and status.get("authed")),
                         "account": status.get("account"),
                         "error": "" if ok else ((err or out).strip()[-400:]),
                         "output": ((out or "") + (err or "")).strip()[-400:]})

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
        running, why = ensure_vm_running(zone)
        if not running:
            return False, "", why
        home = get_vm_home(zone)
        out, err = "", ""
        files = ["news_updater.py", "requirements.txt", "setup_cloud.sh",
                 "config_local.json", "secrets_local.json"]
        for f in files:
            src = os.path.join(BASE_DIR, f)
            if os.path.exists(src):
                ok, o, e = run_gcloud([
                    "compute", "scp", "--zone", zone, src,
                    f"{VM_NAME}:{home}/", "--quiet"], timeout=180)
                out += o; err += e
                if not ok:
                    return False, out, (err + "\n" +
                                        "Upload failed while copying %s." % f)
        # This runs `apt-get update` + a pip install of edgartools + the cron
        # install, which on an e2-micro routinely exceeds 5 minutes; the old
        # 300s limit killed the LOCAL gcloud while the remote work continued,
        # so the retry then failed on a dpkg lock that looked unrelated.
        ok, o, e = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", "cd ~ && bash setup_cloud.sh", "--quiet"], timeout=900)
        out += o; err += e
        return ok, out, err

    def _handle_run_now(self, snapshot=False):
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        running, why = ensure_vm_running(zone)
        if not running:
            self._send_json({"ok": False, "error": why})
            return
        home = get_vm_home(zone)
        extra = " --snapshot" if snapshot else ""
        # A real run takes ~5-15 minutes on an e2-micro (the run history shows
        # up to ~18 min on busy days), so allow a generous margin instead of the
        # old 600s, which could cut a run off mid-flight.
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py --force{extra} 2>&1",
            "--quiet"], timeout=1800)
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

    def _handle_delete_news(self):
        """
        Delete ONE stored news row on the VM (the panel's per-row ✕ button).

        Identified by (ticker, source, item_hash) exactly as --dump-news
        returned them. Every field is sanitized before it reaches the SSH
        command - this is user-supplied input going into a shell string.
        """
        zone = find_vm_zone()
        if not zone:
            self._send_json({"ok": False, "error": "VM not found. Create the server first."})
            return
        payload, error = self._read_json_body()
        if error:
            self._send_json({"ok": False, "error": error}, 400)
            return

        def clean(value, allowed, maxlen):
            return "".join(ch for ch in str(value or "") if ch in allowed)[:maxlen]

        ticker = clean(payload.get("ticker"), set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-"), 12)
        source = clean(payload.get("source"),
                       set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"), 24)
        item_hash = clean(payload.get("item_hash"), set("0123456789abcdef"), 64)
        pushed = bool(payload.get("pushed"))
        if not ticker or not item_hash:
            self._send_json({"ok": False, "error": "Missing ticker or item hash."})
            return
        home = get_vm_home(zone)
        mode = "--delete-news-pushed" if pushed else "--delete-news"
        arg = f"{ticker}|{source}|{item_hash}"
        ok, out, err = run_gcloud([
            "compute", "ssh", "--zone", zone, VM_NAME,
            "--command", f"cd {home} && python3 news_updater.py {mode}='{arg}' 2>&1",
            "--quiet"], timeout=120)
        text = (out + err).strip()
        deleted = 0
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    deleted = json.loads(line).get("deleted", 0)
                    break
                except Exception:
                    continue
        self._send_json({"ok": ok and deleted > 0, "deleted": deleted,
                         "error": "" if deleted else (text or "nothing deleted"),
                         "output": text})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._origin_ok():
            return
        # Mutating routes are POST-only: a foreign page can still make a simple
        # POST, but it cannot do so with a JSON content type without a CORS
        # preflight it will fail, and _origin_ok has already rejected it.
        if parsed.path == "/api/run_now":
            self._handle_run_now(snapshot="snapshot=1" in parsed.query)
            return
        if parsed.path == "/api/create_vm":
            self._handle_create_vm()
            return
        if parsed.path == "/api/purge_junk":
            self._handle_purge_junk()
            return
        if parsed.path == "/api/rediscover":
            self._handle_rediscover()
            return
        if parsed.path == "/api/auth":
            self._handle_auth()
            return
        if parsed.path == "/api/auth_code":
            self._handle_auth_code()
            return
        if parsed.path == "/api/delete_news":
            self._handle_delete_news()
            return
        if parsed.path == "/api/upload":
            data, error = self._read_json_body()
            if error:
                self._send_json({"ok": False, "error": error}, 400)
                return
            # One upload at a time: the read-modify-write below must not
            # interleave with a second click, and the deploy that follows is
            # heavy (scp + ssh) so a duplicate is almost certainly accidental.
            if not CONFIG_LOCK.acquire(blocking=False):
                self._send_json({"ok": False, "error":
                                 "Another upload is already running - wait for it "
                                 "to finish."}, 409)
                return
            try:
                self._handle_upload(data)
            finally:
                CONFIG_LOCK.release()
            return
        self._send_json({"error": "not found"}, 404)

    def _handle_upload(self, data):
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
                                 ("push_max_per_ticker", DEFAULT_CONFIG["push_max_per_ticker"]),
                                 ("news_retention_days", 21),
                                 ("tavily_max_daily_searches",
                                  DEFAULT_CONFIG["tavily_max_daily_searches"]),
                                 ("tavily_max_monthly_searches",
                                  DEFAULT_CONFIG["tavily_max_monthly_searches"]),
                                 ("tavily_min_free_items", 4)):
                if key in data:
                    try:
                        cfg[key] = int(data[key])
                    except (TypeError, ValueError):
                        pass
            # Secrets: ONLY overwrite a key the request actually carries.
            # `data.get(key, "")` used to erase any field the browser did not
            # send - and since the panel fills these inputs from /api/config, a
            # single failed config load left them empty and one "Upload config"
            # click wiped the Telegram token and all three API keys, locally and
            # on the VM. An empty string still means "clear this key" (the user
            # deliberately emptied the box), but a MISSING key means "keep it".
            # The mask sentinel means "the field still shows the saved value".
            for key in SECRET_KEYS:
                if key not in data:
                    continue                      # omitted -> keep stored value
                value = str(data[key] or "").strip()
                if value == SECRET_MASK:
                    continue                      # untouched masked field
                secrets[key] = value
            save_config(cfg); save_secrets(secrets)
            project = get_project()
            if not project:
                self._send_json({"ok": False, "saved_locally": True, "error":
                                 "Config saved on this PC, but no Google Cloud project was "
                                 "found so nothing was uploaded. First open "
                                 "https://console.cloud.google.com once, accept the terms and "
                                 "enable billing (the Always-Free tier stays free), then click "
                                 "Authenticate again."})
                return
            ok, out, err = self._deploy_to_vm(project)
            # Make the two states explicit: local config is written BEFORE the
            # deploy, so a failed upload must not read as a total failure (the
            # user's edits were in fact saved).
            msg = err or ("" if ok else out)
            if not ok:
                msg = ("Config saved on this PC, but uploading to the server FAILED. "
                       "The server is still running the previous configuration.\n\n" + msg)
            self._send_json({"ok": ok, "saved_locally": True, "error": msg,
                             "output": out + err})


def main():
    if not gcloud_available():
        print("=" * 50)
        print(" gcloud is not installed.")
        print(" Install it: https://cloud.google.com/sdk/docs/install")
        print("=" * 50)
        return
    # Bind only to loopback so the panel is not reachable from other machines.
    # (Note: loopback alone does NOT stop a browser from reaching it - see
    # _origin_ok, which validates Host/Origin on every request.)
    HOST = "127.0.0.1"
    server = None
    port = PORT
    for candidate in (PORT, 8002, 8003):
        try:
            server = ThreadingHTTPServer((HOST, candidate), Handler)
            port = candidate
            break
        except OSError as exc:
            print(f"  port {candidate} unavailable ({exc}); trying the next one...")
    if server is None:
        print("=" * 50)
        print(" Could not start: ports 8001-8003 are all in use.")
        print(" Close whatever is using them and run this again.")
        print("=" * 50)
        return
    if port != PORT:
        print(f"  NOTE: using port {port} instead of {PORT}.")
    url = f"http://localhost:{port}"
    # Record the REAL url so start_cloud.bat opens this panel and not whatever
    # else happens to be listening on the default port.
    try:
        with open(os.path.join(BASE_DIR, PANEL_URL_FILE), "w", encoding="utf-8") as f:
            f.write(url)
    except OSError:
        pass
    print(f"PortfolioNewsUpdater panel: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
