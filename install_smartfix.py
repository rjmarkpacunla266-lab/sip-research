#!/usr/bin/env python3
"""install_smartfix.py - run from the project folder (~/W):  python3 install_smartfix.py
Makes the smart badges honest. Smart search used to run BEFORE the "papers with only some of your words"
step, so a paper the normal search would also have shown (marked Missing: ...) could be labelled
"Smart pick". Now smart search runs after that step: "Smart pick" = the normal search would NOT have shown
it, "Smart boost" = it would have, and smart search moved it up. Needs install_smart2 and install_smartbadge.
Safe to run twice."""
import os
SMART = "    if page == 1 and f.get(\"smart\"):\n        results = _add_smart(results, query, f, sq)\n"
POST = "    if post:                              # operators (\"phrase\", -word, author:) are checked before abstracts are hidden\n"
p = next((c for c in ("search.py", os.path.join("routes", "search.py")) if os.path.isfile(c)), None)
if not p:
    print("search.py not found. Run this from the project folder (~/W)."); raise SystemExit(1)
t = open(p, encoding="utf-8").read()
moved = SMART + POST
if moved in t:
    print("Updated: nothing (already done?)")
elif t.count(SMART) == 1 and t.count(POST) == 1:
    t = t.replace(SMART, "", 1).replace(POST, SMART + POST, 1)
    open(p + ".new", "w", encoding="utf-8", newline="").write(t); os.replace(p + ".new", p)
    print("Updated:", p)
else:
    print("warning: left alone, the lines were not found as expected. Run install_smart2.py first.")
