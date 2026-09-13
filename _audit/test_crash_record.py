#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test the top-level crash handler: classification and recorded context.

Two things were wrong with it:
  1. It reported EVERY unhandled exception as "error", including a broken
     output pipe - which just means the window was closed or the output was
     piped into head/tail. Painting that red trains you to ignore the badge.
  2. It wrote only timestamp/status/error, so a failed run showed in the panel
     as 0 tickers / 0 new / 0 sent / no duration - reading like "it ran and
     found nothing" instead of "it died before counting".
"""
import errno
import importlib.util
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", r"F:\MyRepository\PortfolioNewsUpdater\news_updater.py")
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0


def check(label, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))


print("=" * 74)
print("TEST 1 - what counts as a broken pipe")
print("=" * 74)
check("BrokenPipeError", nu._is_broken_pipe(BrokenPipeError(32, "Broken pipe")) is True)
check("OSError(EPIPE)", nu._is_broken_pipe(OSError(errno.EPIPE, "Broken pipe")) is True)
check("ValueError is NOT", nu._is_broken_pipe(ValueError("boom")) is False)
check("KeyError is NOT", nu._is_broken_pipe(KeyError("id")) is False)
check("OSError(ENOENT) is NOT", nu._is_broken_pipe(OSError(errno.ENOENT, "nope")) is False)

print()
print("=" * 74)
print("TEST 2 - _record_crash classification and context")
print("=" * 74)

captured = []


def fake_append(rec):
    captured.append(rec)


nu.append_run_record = fake_append

# Simulate a run that got as far as counting tickers when the pipe broke.
start = datetime.now(nu.EASTERN) - timedelta(seconds=42)
nu._RUN_START = start
nu._RUN_RECORD = {
    "timestamp": start.strftime("%Y-%m-%d %H:%M:%S %Z"),
    "status": "ran",
    "tickers_checked": 6,
    "new_items": 11,
    "sent_items": 3,
    "duration_sec": None,
    "error": None,
}
nu._record_crash(BrokenPipeError(32, "Broken pipe"))
rec = captured[-1]
check("status is 'interrupted'", rec["status"] == "interrupted", rec["status"])
check("error explains the cause", "disconnected" in rec["error"], "")
check("error does NOT say 'unhandled'", "unhandled" not in rec["error"])
check("keeps tickers_checked", rec["tickers_checked"] == 6, str(rec["tickers_checked"]))
check("keeps new_items", rec["new_items"] == 11, str(rec["new_items"]))
check("keeps sent_items", rec["sent_items"] == 3, str(rec["sent_items"]))
check("computes duration from the run start",
      rec["duration_sec"] is not None and 40 <= rec["duration_sec"] <= 60,
      str(rec["duration_sec"]))
check("keeps the run's own timestamp", rec["timestamp"] == nu._RUN_RECORD["timestamp"])

# A real bug must still be red.
nu._RUN_RECORD = dict(nu._RUN_RECORD, tickers_checked=2)
nu._record_crash(TypeError("tuple indices must be integers or slices, not str"))
rec = captured[-1]
check("TypeError -> 'error'", rec["status"] == "error", rec["status"])
check("TypeError keeps 'unhandled' prefix", rec["error"].startswith("unhandled: TypeError"), "")
check("TypeError keeps context", rec["tickers_checked"] == 2, str(rec["tickers_checked"]))

# Ctrl-C is also a deliberate stop, not a failure.
nu._RUN_RECORD = {"tickers_checked": 4, "new_items": 0, "sent_items": 0,
                  "duration_sec": None, "error": None}
nu._record_crash(KeyboardInterrupt())
rec = captured[-1]
check("KeyboardInterrupt -> 'interrupted'", rec["status"] == "interrupted", rec["status"])
check("Ctrl-C message is clear", "Ctrl-C" in rec["error"], "")

print()
print("=" * 74)
print("TEST 3 - crash BEFORE main() built a record (CLI modes, imports)")
print("=" * 74)
nu._RUN_RECORD = None
nu._RUN_START = None
try:
    nu._record_crash(ValueError("very early failure"))
    rec = captured[-1]
    check("writes a row anyway", rec["status"] == "error", rec["status"])
    check("has a timestamp", bool(rec.get("timestamp")), "")
    check("counters default to 0, not missing",
          rec["tickers_checked"] == 0 and rec["new_items"] == 0, "")
    check("duration is None, not a crash", rec["duration_sec"] is None, "")
except Exception as exc:
    check("does not raise", False, repr(exc))

print()
print(f"RESULT: {'ALL TESTS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
