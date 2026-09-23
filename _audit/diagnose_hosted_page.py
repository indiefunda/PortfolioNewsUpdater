#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnose why the hosted page never reaches the sign-in gate.

Serves a copy of the built site from localhost (an authorised Firebase domain,
so behaviour matches the deployed site) with error capture injected before the
module runs, then reports what the browser actually said.
"""
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web_public")
TMP = os.path.join(tempfile.gettempdir(), "gl_diag")
PORT = 8137

shutil.rmtree(TMP, ignore_errors=True)
shutil.copytree(SRC, TMP)

# 1. Capture errors BEFORE anything else runs.
html = open(os.path.join(TMP, "index.html"), encoding="utf-8").read()
capture = """<script>
window.__log = [];
window.addEventListener('error', function(e){
  window.__log.push('ERROR: ' + e.message + ' @ ' + (e.filename||'') + ':' + (e.lineno||''));
}, true);
window.addEventListener('unhandledrejection', function(e){
  var r = e.reason;
  window.__log.push('REJECTION: ' + ((r && (r.message || r.code || r.name)) || String(r)));
});
window.__log.push('cfg present: ' + (typeof window.__GL_FIREBASE_CONFIG__));
</script>"""
html = html.replace("<head>", "<head>" + capture, 1)

# 2. Instrument the module itself: log as soon as the file starts executing, so
#    "never ran" and "ran and threw" are distinguishable.
boot_path = os.path.join(TMP, "firebase-boot.js")
boot = open(boot_path, encoding="utf-8").read()
boot = ("window.__log && window.__log.push('MODULE FILE EXECUTING');\n"
        "window.__log && window.__log.push('cfg in module: ' + (typeof window.__GL_FIREBASE_CONFIG__));\n"
        + boot)

# Fine-grained progress markers at each await/step, to find the exact stall.
marks = [
    ("const app = initializeApp(cfg);",
     "const app = initializeApp(cfg); window.__log && window.__log.push('1 initializeApp OK');"),
    ("  const auth = getAuth(app);",
     "  window.__log && window.__log.push('2 dynamic imports resolved');\n"
     "  const auth = getAuth(app);\n"
     "  window.__log && window.__log.push('3 getAuth+getFirestore OK');"),
    ("  onAuthStateChanged(auth, async (user) => {",
     "  window.__log && window.__log.push('4 registering onAuthStateChanged');\n"
     "  onAuthStateChanged(auth, async (user) => {\n"
     "    window.__log && window.__log.push('5 AUTH CALLBACK fired, user=' + (user ? user.email : 'null'));"),
]
for old, new in marks:
    if old not in boot:
        print(f"!! marker not found, instrumentation incomplete: {old[:50]}")
    boot = boot.replace(old, new, 1)
open(boot_path, "w", encoding="utf-8").write(boot)

# 3. Report at the end, whether or not the module got anywhere.
report = """<script>
setTimeout(function(){
  var el = document.createElement('pre');
  el.id = 'probe';
  el.style.cssText = 'position:fixed;bottom:0;left:0;right:0;background:#000;color:#0f0;font-size:10px;z-index:99;margin:0;padding:4px';
  var out = (window.__log || []).join(String.fromCharCode(10));
  if (!out) out = '(no log entries - the module never started)';
  out += String.fromCharCode(10) + 'signin gate present: ' + !!document.getElementById('signin');
  out += String.fromCharCode(10) + 'stats text: ' + (document.getElementById('stats')||{}).textContent;
  el.textContent = out;
  document.body.appendChild(el);
}, 25000);
</script>"""
html = html.replace("</body>", report + "</body>")
open(os.path.join(TMP, "index.html"), "w", encoding="utf-8").write(html)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


os.chdir(TMP)
srv = socketserver.TCPServer(("127.0.0.1", PORT), Quiet)
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"serving {TMP} on http://127.0.0.1:{PORT}")

dom = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                      "--virtual-time-budget=60000", "--dump-dom",
                      f"http://127.0.0.1:{PORT}/"],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=300)
srv.shutdown()

m = re.search(r'<pre id="probe"[^>]*>(.*?)</pre>', dom.stdout or "", re.S)
print()
print("=== browser report ===")
print(m.group(1).strip() if m else "(no probe output - the page did not finish loading)")
