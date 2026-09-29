#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test that the page honours filters passed in the URL.

This is what makes a link from a Telegram digest useful: it should land on a
filtered view, not the unfiltered list.

Headless with --virtual-time-budget is fine HERE (unlike the Firebase page):
this is the static build with no auth, so nothing depends on IndexedDB.
"""
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(tempfile.gettempdir(), "gl_params")

fail = 0


def check(label, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


r = subprocess.run([sys.executable, os.path.join(ROOT, "news_web.py"),
                    f"--out={OUT}", "--db=" + os.path.join(ROOT, "_audit", "news.db"),
                    "--embed"], capture_output=True, text=True, timeout=180)
if not os.path.exists(os.path.join(OUT, "index.html")):
    print("export failed:", (r.stdout + r.stderr)[:300])
    sys.exit(1)

probe = """
<script>
window.addEventListener('load', function(){
  setTimeout(function(){
    var pre = document.createElement('pre');
    pre.id = 'probe';
    pre.textContent = JSON.stringify({
      ticker: F.ticker, minImp: F.minImp, sort: F.sort,
      view: VIEW.length,
      wrongTicker: VIEW.filter(function(i){ return i.ticker !== F.ticker; }).length,
      belowFloor: VIEW.filter(function(i){ return i.importance == null || i.importance < 6; }).length,
      tickerSelect: document.getElementById('ticker').value
    });
    document.body.appendChild(pre);
  }, 400);
});
</script>
"""

base = open(os.path.join(OUT, "index.html"), encoding="utf-8").read()
probe_file = os.path.join(tempfile.gettempdir(), "gl_params_probe.html")
open(probe_file, "w", encoding="utf-8").write(base.replace("</body>", probe + "</body>"))

url = "file:///" + probe_file.replace("\\", "/") + "?ticker=QFIN&minImp=6&sort=importance"
dom = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                      "--virtual-time-budget=8000", "--dump-dom", url],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=180)
m = re.search(r'<pre id="probe"[^>]*>(.*?)</pre>', dom.stdout or "", re.S)
if not m:
    print("no probe output - page did not run")
    sys.exit(1)

import json  # noqa: E402
state = json.loads(m.group(1))
print("  page state:", state)
check("ticker filter applied from the URL", state["ticker"] == "QFIN", state["ticker"])
check("importance floor applied", state["minImp"] == 6, str(state["minImp"]))
check("sort applied", state["sort"] == "importance", state["sort"])
check("the dropdown reflects it", state["tickerSelect"] == "QFIN", state["tickerSelect"])
check("rows really filtered", state["view"] > 0 and state["wrongTicker"] == 0
      and state["belowFloor"] == 0,
      f"view={state['view']} wrong={state['wrongTicker']} low={state['belowFloor']}")

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
