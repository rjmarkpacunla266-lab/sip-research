#!/usr/bin/env python3
"""install_smarton.py - run from the project folder (~/W):  python3 install_smarton.py
Makes smart search available WITHOUT any Render setting: SMART_SEARCH now defaults to on and the daily
limit defaults to 80 searches (safe without an OpenAlex key). Each person still has to tick the Smart
search switch on the page. If you ever add the variable SMART_SEARCH=0 in Render, it turns smart search off.
Needs install_smart2 first. Safe to run twice."""
import os
PAIRS = [
 ('SMART_ON = os.environ.get("SMART_SEARCH", "") == "1"', 'SMART_ON = os.environ.get("SMART_SEARCH", "1") == "1"'),
 ('SMART_DAILY_LIMIT = int(os.environ.get("SMART_DAILY_LIMIT", "300"))', 'SMART_DAILY_LIMIT = int(os.environ.get("SMART_DAILY_LIMIT", "80"))'),
 ('except ValueError:\n    SMART_DAILY_LIMIT = 300\n', 'except ValueError:\n    SMART_DAILY_LIMIT = 80\n'),
]
p = next((c for c in ("search.py", os.path.join("routes", "search.py")) if os.path.isfile(c)), None)
if not p:
    print("search.py not found. Run this from the project folder (~/W)."); raise SystemExit(1)
t = open(p, encoding="utf-8").read(); n = 0; bad = []
for old, new in PAIRS:
    if new in t: continue
    if t.count(old) == 1: t = t.replace(old, new); n += 1
    else: bad.append(old[:50])
if bad:
    print("warning: left alone, did not match:", " | ".join(bad), "\nRun install_smart2.py first."); raise SystemExit(1)
if n:
    open(p + ".new", "w", encoding="utf-8", newline="").write(t); os.replace(p + ".new", p)
print("Updated:", p if n else "nothing (already done?)")
