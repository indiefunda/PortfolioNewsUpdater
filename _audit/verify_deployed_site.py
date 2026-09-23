#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the deployed site: it serves correctly, AND the rules really deny
anonymous reads. The second check is the important one - an auth-gated page over
an open database is the exact failure mode this whole design avoids."""
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT = "keen-wavelet-275120"
SITE = f"https://{PROJECT}.web.app"
DOCS = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"

fail = 0


def check(label, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


def get(url, raw=False):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read()
            return r.status, (body if raw else body.decode("utf-8", "replace")), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return None, str(e), {}


print("=" * 74)
print("TEST 1 - the site serves")
print("=" * 74)
status, html, headers = get(SITE + "/")
check("index.html returns 200", status == 200, f"HTTP {status}")
check("is HTML", "text/html" in headers.get("Content-Type", ""),
      headers.get("Content-Type", ""))
check("carries the Firebase config", PROJECT in html and "apiKey" in html)
check("sets __GL_HOSTED__", "__GL_HOSTED__" in html)
check("loads firebase-boot.js", "firebase-boot.js" in html)
# The precise test: with no embedded data the placeholder resolves to null.
# (Searching for a column name like "first_seen" gives a false positive - the
# row renderer legitimately mentions it in the JavaScript.)
check("no data embedded (EMBEDDED resolves to null)", "var EMBEDDED = null;" in html)
check("page is small, consistent with no embedded archive",
      len(html) < 100_000, f"{len(html)} bytes")

status, js, headers = get(SITE + "/firebase-boot.js")
check("firebase-boot.js returns 200", status == 200, f"HTTP {status}")
check("served as JavaScript", "javascript" in headers.get("Content-Type", ""),
      headers.get("Content-Type", ""))

print()
print("=" * 74)
print("TEST 2 - anonymous access to the data is REFUSED")
print("=" * 74)
# No Authorization header at all: this is what a stranger with the URL gets.
status, body, _ = get(DOCS + "/news?pageSize=1")
denied = status in (401, 403)
check("news collection without auth is denied", denied, f"HTTP {status}")
if not denied:
    print("      *** THE DATABASE IS PUBLICLY READABLE ***")
    print("      " + body[:300])

status, body, _ = get(DOCS + "/meta/status")
check("meta doc without auth is denied", status in (401, 403), f"HTTP {status}")

print()
print("=" * 74)
print("TEST 3 - security headers from firebase.json")
print("=" * 74)
for h in ("X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options"):
    check(f"{h} present", h in headers, headers.get(h, ""))

print()
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
print()
print("NOT verified here: the Google sign-in round trip and the authenticated")
print("read, which need a real browser session. Open the site and sign in.")
print()
print("WARNING for anyone extending this: do NOT test the sign-in gate with")
print("--virtual-time-budget. Firebase Auth resolves its initial state through")
print("IndexedDB and timers, and virtual time breaks that, so onAuthStateChanged")
print("never fires and the page looks permanently stuck on 'Loading...'. That is")
print("a false negative - the page is fine. Use _audit/cdp_probe.js instead,")
print("which drives a real clock over the DevTools protocol and confirms both")
print("the gate and a screenshot.")
sys.exit(1 if fail else 0)
