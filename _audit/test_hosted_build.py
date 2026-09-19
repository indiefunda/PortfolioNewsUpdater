#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the hosted build's Firebase wiring WITHOUT a Firebase project.

Two things are checkable offline and are exactly the things most likely to be
wrong: whether the pinned CDN version actually resolves and exports what the
bootstrap expects, and whether the generated index.html parses as JavaScript.

The Firestore queries themselves cannot be tested here - they need the user's
project - and this script says so rather than implying otherwise.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT = os.path.join(ROOT, "web", "firebase-boot.js")

fail = 0


def check(label, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


boot_src = open(BOOT, encoding="utf-8").read()

print("=" * 74)
print("TEST 1 - module syntax and specifier rules")
print("=" * 74)
ver = re.search(r'FIREBASE_VERSION\s*=\s*"([^"]+)"', boot_src).group(1)
print(f"  pinned Firebase version: {ver}")

# A static import specifier must be a string literal; a template literal there
# is a SyntaxError that only shows up in a browser.
static_imports = re.findall(r'^import\s+.*?from\s+(.+?);', boot_src, re.M)
bad = [s for s in static_imports if not s.strip().startswith('"')]
check("static imports use string literals", not bad, str(bad))

print()
print("=" * 74)
print("TEST 2 - the pinned CDN really resolves and exports what we use")
print("=" * 74)
probe = f"""<!DOCTYPE html><html><body><pre id="p">…</pre><script type="module">
const CDN = "https://www.gstatic.com/firebasejs/{ver}";
const out = [];
try {{
  const app = await import(`${{CDN}}/firebase-app.js`);
  out.push('firebase-app: ' + (typeof app.initializeApp === 'function' ? 'OK' : 'MISSING initializeApp'));
  const auth = await import(`${{CDN}}/firebase-auth.js`);
  const need = ['getAuth','GoogleAuthProvider','signInWithPopup','signOut','onAuthStateChanged'];
  out.push('firebase-auth: ' + need.filter(n => typeof auth[n] === 'function').length + '/' + need.length + ' exports');
  const fs = await import(`${{CDN}}/firebase-firestore.js`);
  const need2 = ['getFirestore','collection','getDocs','doc','getDoc'];
  out.push('firebase-firestore: ' + need2.filter(n => typeof fs[n] === 'function').length + '/' + need2.length + ' exports');
}} catch (e) {{
  out.push('ERROR: ' + e.message);
}}
document.getElementById('p').textContent = out.join(String.fromCharCode(10));
</script></body></html>"""

tmp = os.path.join(tempfile.gettempdir(), "gl_fb_probe.html")
open(tmp, "w", encoding="utf-8").write(probe)
dom = subprocess.run([EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
                      "--virtual-time-budget=15000", "--dump-dom",
                      "file:///" + tmp.replace("\\", "/")],
                     capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=180)
m = re.search(r'<pre id="p">(.*?)</pre>', dom.stdout or "", re.S)
body = (m.group(1) if m else "").strip()
print("  " + body.replace("\n", "\n  "))

check("firebase-app.js resolved", "firebase-app: OK" in body)
check("firebase-auth.js exports all 5 used symbols", "firebase-auth: 5/5" in body)
check("firebase-firestore.js exports all 5 used symbols",
      "firebase-firestore: 5/5" in body)

print()
print("=" * 74)
print("TEST 3 - hosted export shape")
print("=" * 74)
out = os.path.join(tempfile.gettempdir(), "gl_hosted")
cfg = os.path.join(tempfile.gettempdir(), "gl_fbcfg.json")
json.dump({"apiKey": "AIzaTEST", "projectId": "test-project",
           "appId": "1:1:web:1", "authDomain": "test.firebaseapp.com"},
          open(cfg, "w"))

r = subprocess.run([sys.executable, os.path.join(ROOT, "news_web.py"),
                    f"--out={out}", "--db=" + os.path.join(ROOT, "_audit", "news.db"),
                    "--hosted", "--firebase-config=" + cfg],
                   capture_output=True, text=True, timeout=180)
print("  " + (r.stdout or r.stderr).strip().replace("\n", "\n  "))
check("firebase-boot.js copied", os.path.exists(os.path.join(out, "firebase-boot.js")))
check("news.json NOT written (data lives in Firestore)",
      not os.path.exists(os.path.join(out, "news.json")))
html = open(os.path.join(out, "index.html"), encoding="utf-8").read()
check("sets __GL_HOSTED__ before the page script",
      html.index("__GL_HOSTED__") < html.index("async function loadData"))
check("embeds the firebase config", "test-project" in html)

# Without a config the hosted build must refuse rather than emit a broken page.
r2 = subprocess.run([sys.executable, os.path.join(ROOT, "news_web.py"),
                     f"--out={out}", "--db=" + os.path.join(ROOT, "_audit", "news.db"),
                     "--hosted"],
                    capture_output=True, text=True, timeout=180)
check("refuses --hosted without a config", "needs --firebase-config" in (r2.stdout + r2.stderr))

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print("NOTE: the Firestore reads themselves are NOT verified here - they need a")
print("      real Firebase project. See SETUP-WEB.md for the setup and the")
print("      end-to-end check to run once it exists.")
sys.exit(1 if fail else 0)
