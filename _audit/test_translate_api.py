#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test /api/translate with a properly UTF-8 encoded body (PowerShell mangles it)."""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
text = "美国财政部宣布对伊朗相关实体实施新制裁"
body = json.dumps({"texts": [text]}).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8001/api/translate", data=body,
    headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print("  sent:", text)
    print("  got :", data.get("translations"))
    ok = bool(data.get("translations")) and "?" not in (data["translations"][0] or "")
    print("  [%s] UTF-8 round-trip clean" % ("PASS" if ok else "FAIL"))
except Exception as exc:
    print("  FAILED:", exc)
    sys.exit(1)
