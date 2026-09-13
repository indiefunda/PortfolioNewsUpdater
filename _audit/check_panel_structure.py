#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Structural check of the tabbed panel: every card sits inside exactly one
pane, panes are balanced, and no id got duplicated by the refactor."""
import importlib.util
import re
import sys
from html.parser import HTMLParser

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location(
    "cm", r"F:\MyRepository\PortfolioNewsUpdater\cloud_manager.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
html = m.HTML

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Check(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.ids = {}
        self.cards = []          # (depth, pane_id or None)
        self.pane_depth = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID:
            self.stack.append(tag)
        if "id" in a:
            self.ids[a["id"]] = self.ids.get(a["id"], 0) + 1
        if "class" in a and "tabpane" in a["class"]:
            self.pane_depth[a["id"]] = len(self.stack)
        if "class" in a and a["class"] == "card":
            # which pane (if any) is this card inside?
            cur = None
            for pid, depth in self.pane_depth.items():
                if len(self.stack) >= depth:
                    cur = pid
            self.cards.append(cur)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"stray </{tag}>")
            return
        # pop until we find the match (tolerate implicit closes)
        while self.stack:
            top = self.stack.pop()
            if top == tag:
                return
        self.errors.append(f"unmatched </{tag}>")


p = Check()
p.feed(html)
print("unclosed tags at EOF:", p.stack)
print("parse errors:", p.errors[:5] if p.errors else "none")
dupes = {k: v for k, v in p.ids.items() if v > 1}
print("duplicate ids:", dupes if dupes else "none")
print()
print("panes found:", sorted(p.pane_depth))
print()
from collections import Counter
c = Counter(p.cards)
print("cards per pane:")
for pane, n in c.items():
    print(f"  {pane}: {n} card(s)")
orphan = c.get(None, 0)
print()
ok = (not p.stack and not p.errors and not dupes and orphan == 0)
print("VERDICT:", "structure OK ✅" if ok else f"PROBLEM ❌ ({orphan} card(s) outside any pane)")
sys.exit(0 if ok else 1)
