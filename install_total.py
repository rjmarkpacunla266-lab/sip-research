#!/usr/bin/env python3
"""install_total.py - run from the project folder (~/W):  python3 install_total.py
Fixes the results count: it could say "1 results" above 10 papers, because it used OpenAlex's strict
count. The count now never goes below the number of papers found. Safe to run twice."""
import os
OLD = '"total": (total or len(results)) if not relaxed else len(results),'
NEW = '"total": max(total or 0, len(results)) if not relaxed else len(results),'
p = next((c for c in ("search.py", os.path.join("routes", "search.py")) if os.path.isfile(c)), None)
if not p:
    print("search.py not found. Run this from the project folder (~/W)."); raise SystemExit(1)
t = open(p, encoding="utf-8").read()
if NEW in t:
    print("Updated: nothing (already done?)")
elif t.count(OLD) == 1:
    open(p + ".new", "w", encoding="utf-8", newline="").write(t.replace(OLD, NEW)); os.replace(p + ".new", p)
    print("Updated:", p)
else:
    print("warning: the count line was not found (" + str(t.count(OLD)) + " matches), left alone.")
