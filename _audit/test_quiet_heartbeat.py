#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify the quiet-run heartbeat.

The heartbeat is the only thing that distinguishes "a quiet news day" from "the
pipeline is dead" - both are silence on the phone. These checks pin the two
properties that matter:

  * it must fire on a real quiet STRETCH and stay quiet otherwise (no message
    after every single empty run, no message right after a delivery);
  * it must never claim a delivery that did not happen - a run recorded as
    error/interrupted ends the streak instead of being counted as "quiet", and
    the heartbeat itself must not mark any news row as pushed.
"""
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "nu", os.path.join(ROOT, "news_updater.py"))
nu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nu)

fail = 0


def check(name, ok, detail=""):
    global fail
    if not ok:
        fail += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}"
          + (f"   [{detail}]" if detail and not ok else ""))


def rec(status="ran", sent=0, stamp="2026-10-21 09:15:10 EDT", new=0,
        heartbeat_sent=False):
    r = {"timestamp": stamp, "status": status, "sent_items": sent,
         "new_items": new, "tickers_checked": 6}
    if heartbeat_sent:
        r["heartbeat_sent"] = True
    return r


print("quiet_run_streak / heartbeat_due")
print("-" * 72)
# Newest first: two quiet runs, then one that delivered 3 items.
hist = [rec(), rec(stamp="2026-10-20 17:00:11 EDT"),
        rec(sent=3, stamp="2026-10-20 09:15:11 EDT"), rec()]
check("counts 2 trailing quiet runs + this one = 3",
      nu.quiet_run_streak(hist, "ran", 0) == 3,
      str(nu.quiet_run_streak(hist, "ran", 0)))
check("a delivery this run ends the streak", nu.quiet_run_streak(hist, "ran", 2) == 0)
check("an error run is NOT counted as quiet",
      nu.quiet_run_streak([rec(status="error")] + hist, "ran", 0) == 1)
check("this run erroring means no streak",
      nu.quiet_run_streak(hist, "error", 0) == 0)
check("empty history -> streak 1", nu.quiet_run_streak([], "ran", 0) == 1)

check("not due on the very first quiet run", nu.heartbeat_due([]) is False)
check("due on the 2nd quiet run", nu.heartbeat_due([rec()]) is True)
check("fires mid-stretch too (deployed during a quiet week)",
      nu.heartbeat_due([rec(), rec()]) is True)
check("no heartbeat again 1 run after one was sent",
      nu.heartbeat_due([rec(), rec(heartbeat_sent=True), rec(sent=3)]) is False)
check("reminds after `every` more quiet runs",
      nu.heartbeat_due([rec(), rec(), rec(), rec(),
                        rec(heartbeat_sent=True), rec(sent=3)]) is True)
check("every=1 means every quiet run after the first",
      nu.heartbeat_due([rec(), rec(heartbeat_sent=True), rec(sent=3)],
                       every=1) is True)
check("after=0 disables", nu.heartbeat_due([rec()] * 9, after=0) is False)
check("a delivery this run ends the streak",
      nu.heartbeat_due([rec()], sent=2) is False)
check("a previous delivery ends the streak",
      nu.heartbeat_due([rec(sent=1), rec()]) is False)
check("an error run is not a quiet run",
      nu.heartbeat_due([rec(status="error"), rec()]) is False)

print()
print("message")
print("-" * 72)
msg = nu.build_quiet_heartbeat(
    rec(new=6), 2, [rec(sent=3, stamp="2026-10-20 09:15:11 EDT")], "pre-open")
check("names the streak", "2 quiet runs in a row" in msg)
check("shows the run time in ET", "21 Oct 09:15 ET" in msg)
check("shows the candidate count", "6 new candidate(s)" in msg)
check("shows the last delivery", "20 Oct 09:15 ET" in msg and "3 item(s)" in msg)
check("says nothing is broken", "Nothing to fix" in msg)
check("no last delivery does not crash",
      "No delivery recorded yet" in nu.build_quiet_heartbeat(rec(), 2, [], ""))
check("bad timestamp is tolerated",
      nu.build_quiet_heartbeat({"timestamp": "garbage"}, 2, [], "")[:4] != "")

print()
print("sending")
print("-" * 72)
sent = []


def fake_send(token, chat_id, message, parse_mode=None):
    sent.append((token, chat_id, message, parse_mode))
    return True, "plain"


def boom(*a, **k):
    raise AssertionError("heartbeat must not touch the push state")


real_send = nu.send_telegram
real_hist = nu.load_run_history
real_mark = nu.mark_pushed
real_rec = nu.record_pushed_stories
nu.send_telegram = fake_send
nu.mark_pushed = boom
nu.record_pushed_stories = boom
try:
    # One quiet run behind this one -> this run is the 2nd -> heartbeat due.
    nu.load_run_history = lambda: [rec(), rec(sent=4)]
    r = rec()
    ok = nu.maybe_send_quiet_heartbeat(
        r, {}, "tok", "123", label="pre-open")
    check("sends on the 2nd quiet run", ok is True and len(sent) == 1)
    check("sent as plain text (nothing to escape)",
          sent and sent[-1][3] == "")
    check("records heartbeat_sent on the run", r.get("heartbeat_sent") is True)

    sent.clear()
    nu.load_run_history = lambda: [rec(sent=4)]
    check("does not send right after a delivery",
          nu.maybe_send_quiet_heartbeat(rec(), {}, "tok", "123",
                                        label="pre-open") is False
          and not sent)

    sent.clear()
    nu.load_run_history = lambda: [rec(), rec(sent=4)]
    check("manual runs never get it",
          nu.maybe_send_quiet_heartbeat(rec(), {}, "tok", "123",
                                        label="manual") is False and not sent)
    check("disabled by config 0",
          nu.maybe_send_quiet_heartbeat(
              rec(), {"heartbeat_after_quiet_runs": 0}, "tok", "123",
              label="pre-open") is False and not sent)
    check("dry run does not send",
          nu.maybe_send_quiet_heartbeat(rec(), {}, "tok", "123",
                                        label="pre-open", dry_run=True) is False
          and not sent)
    check("no Telegram configured -> no crash, no send",
          nu.maybe_send_quiet_heartbeat(rec(), {}, "", "", label="pre-open") is False
          and not sent)
    check("config knob 3 delays it until the 3rd quiet run",
          nu.maybe_send_quiet_heartbeat(
              rec(), {"heartbeat_after_quiet_runs": 3}, "tok", "123",
              label="pre-open") is False
          and nu.maybe_send_quiet_heartbeat(
              rec(), {"heartbeat_after_quiet_runs": 2}, "tok", "123",
              label="pre-open", dry_run=False) is True)
finally:
    nu.send_telegram = real_send
    nu.load_run_history = real_hist
    nu.mark_pushed = real_mark
    nu.record_pushed_stories = real_rec

print()
print("=" * 72)
print(f"RESULT: {'ALL CHECKS PASSED' if fail == 0 else str(fail) + ' FAILURE(S)'}")
sys.exit(1 if fail else 0)
