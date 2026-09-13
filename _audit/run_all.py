#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the WHOLE audit suite with one command and one verdict.

Usage:
    python _audit/run_all.py          # offline checks (fast, no network/AI)
    python _audit/run_all.py --net    # also run the AI/network-dependent tests
    python _audit/run_all.py --quiet  # one line per check

The point is that a fast-moving repo needs one gate that cannot drift. Three
things this deliberately does:

  * Checks the exit code AND scans the output for failure markers. Two suites in
    this repo printed "RESULT: N FAILURE(S)" while still exiting 0, so trusting
    the return code alone would have reported a green run over a real failure.
  * Treats a crash (no verdict at all) as a failure rather than a pass.
  * Names every check, so a suite that stops being collected is visible instead
    of silently shrinking the suite.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

# (kind, filename, extra args). kind "py" -> run with Python, "node" -> Node.
OFFLINE = [
    ("py", "test_gates.py", []),
    ("py", "test_banks.py", []),
    ("py", "test_push_gates.py", []),
    ("py", "test_orphans.py", []),
    ("py", "test_event_gate.py", []),
    ("py", "test_delete.py", []),
    ("py", "test_market_noise.py", []),
    ("py", "test_no_write_credits.py", []),
    ("py", "test_junk_domains.py", []),
    ("py", "test_junk_fetch_filter.py", []),
    ("py", "test_crash_record.py", []),
    ("py", "test_schedule.py", []),
    ("py", "schedule_sim.py", []),
    ("py", "check_panel_js.py", []),
    ("py", "check_panel_ids.py", []),
    ("py", "check_panel_structure.py", []),
    ("py", "check_batch_quotes.py", []),
    ("py", "show_pane_order.py", []),
    ("py", "verify_docs.py", []),
    ("node", "test_tabs.js", []),
]
# Needs live API keys / network, so it is opt-in.
NETWORK = [
    ("py", "test_translate_api.py", []),
    ("py", "test_translate_batch.py", []),
]

# Anything matching one of these in the output means failure, even with exit 0.
# Kept narrow on purpose: a pattern that matches prose in a test's own header
# (e.g. the words "false positives" in an explanatory title) reports a failure
# for a suite that actually passed, and a check that cries wolf gets ignored.
FAIL_PATTERNS = [
    re.compile(r"RESULT:\s*FAIL", re.I),
    re.compile(r"RESULT:\s*\d+\s*FAILURE", re.I),
    re.compile(r"VERDICT:\s*(BROKEN|NOT CLEAN|FAIL)", re.I),
    re.compile(r"\d+\s+FAILURE\(S\)", re.I),
    re.compile(r"drop-misses=[1-9]"),
    re.compile(r"false-positives=[1-9]"),
    re.compile(r"\*\*\*"),                      # marker used for real problems
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^\s*FAIL\b", re.M),
]
# Phrases proving the run reached a verdict rather than dying early.
OK_PATTERNS = [
    re.compile(r"ALL TESTS PASSED|ALL PASSED|ALL CHECKS PASSED", re.I),
    re.compile(r"RESULT:\s*PASS", re.I),
    re.compile(r"VERDICT:.*\bOK\b", re.I),
    re.compile(r"checks pass"),
    re.compile(r"^\s*\d+/\d+ ", re.M),
]
# A suite that needs a private fixture (the real news DB / discovered lookup,
# neither of which is committed) says SKIPPED and exits 0. That is not a
# failure, but it must not be reported as a pass either.
SKIP_PATTERNS = [re.compile(r"^\s*SKIPPED\b", re.M)]


def run_one(kind, name, extra):
    path = HERE / name
    if not path.exists():
        return "MISSING", "file not found", 127
    cmd = ([PY, str(path)] if kind == "py" else ["node", str(path)]) + list(extra)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                              cwd=str(ROOT), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "exceeded 600s", 124
    out = (proc.stdout or "") + (proc.stderr or "")
    code = proc.returncode

    if code != 0:
        return "FAIL", f"exit {code}", code
    for pat in FAIL_PATTERNS:
        m = pat.search(out)
        if m:
            line = next((l.strip() for l in out.splitlines() if pat.search(l)), m.group(0))
            return "FAIL", f"exit 0 but output says: {line[:70]}", 1
    if any(p.search(out) for p in SKIP_PATTERNS):
        return "SKIP", "needs a local private fixture", 0
    if not any(p.search(out) for p in OK_PATTERNS) and "informational" not in out:
        # No recognisable verdict - do not silently call that a pass.
        tail = (out.strip().splitlines() or ["(no output)"])[-1][:70]
        return "WARN", f"no verdict found; last line: {tail}", 0
    return "PASS", next((l.strip() for l in out.splitlines()
                         if any(p.search(l) for p in OK_PATTERNS)), "")[:70], 0


def main():
    quiet = "--quiet" in sys.argv
    checks = list(OFFLINE) + (list(NETWORK) if "--net" in sys.argv else [])
    print("=" * 78)
    print(f"PortfolioNewsUpdater audit suite - {len(checks)} checks"
          + ("" if "--net" in sys.argv else "  (offline; add --net for AI tests)"))
    print("=" * 78)
    results = []
    for kind, name, extra in checks:
        status, detail, code = run_one(kind, name, extra)
        results.append((status, name, detail))
        if quiet:
            print(f"  {status:7} {name}")
        else:
            print(f"  {status:7} {name:28} {detail}")
    bad = [r for r in results if r[0] in ("FAIL", "TIMEOUT", "MISSING")]
    warn = [r for r in results if r[0] == "WARN"]
    skip = [r for r in results if r[0] == "SKIP"]
    print("=" * 78)
    print(f"{len(results) - len(bad) - len(warn) - len(skip)} pass, {len(bad)} fail, "
          f"{len(warn)} warn, {len(skip)} skipped")
    for status, name, detail in bad:
        print(f"  {status}: {name} - {detail}")
    for status, name, detail in warn:
        print(f"  WARN: {name} - {detail}")
    for status, name, detail in skip:
        print(f"  SKIP: {name} - {detail}")
    print()
    print("VERDICT: " + ("ALL CHECKS PASSED" if not bad and not warn else "NOT CLEAN"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
