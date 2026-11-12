#!/usr/bin/env python3
"""install_brand.py - run from the project folder (~/W):  python3 install_brand.py
New wording for the "no AI" promise, true whether or not smart search is turned on:
   "Real papers, real sources, no AI hallucinations."
AI never writes your answers, so nothing is made up; every result is a real paper you can open.
It changes the lines the SEO installer added, then LISTS every other line in your pages that still
says "no AI" so you can decide. Safe to run twice. Run it again if you re-run install_seo4."""
import glob, os, re

FAQ_OLD = "No. Sturch uses fixed rules to search papers, match sentences and build citations. Real papers, real sources, no AI."
FAQ_NEW = ("Sturch never uses AI to write answers, so nothing is made up. It finds real papers with fixed rules, "
           "and may use OpenAlex's meaning-based search to help find and sort them. Every result is a real paper "
           "you can open, and quoted sentences come from the paper's own text. Real papers, real sources, no AI hallucinations.")
PAIRS = [
    (FAQ_OLD, FAQ_NEW),
    ("Free, no AI, no made-up answers.", "Free, real papers only, no AI hallucinations."),
    ("Real papers, real sources, no AI.", "Real papers, real sources, no AI hallucinations."),
    ("no AI research tool,", "no AI hallucinations research tool,"),
]

def read(p): return open(p, encoding="utf-8").read()
def write(p, t): open(p, "w", encoding="utf-8", newline="").write(t)

files = [p for p in glob.glob("templates/*.html") + glob.glob("*.html")]
if not files:
    print("No templates found. Run this from the project folder."); raise SystemExit(1)
changed, left = [], []
for p in sorted(set(files)):
    t = t0 = read(p)
    for old, new in PAIRS:
        if new in t and old not in t:
            continue
        t = t.replace(old, new)
    if t != t0:
        write(p, t); changed.append(p)
    for n, line in enumerate(t.splitlines(), 1):
        if re.search(r"\bno ai\b|without ai|ai[- ]free|not use ai|doesn.t use ai", line, re.I) and "hallucinations" not in line.lower():
            left.append("%s:%d: %s" % (p, n, line.strip()[:110]))
print("Updated:", ", ".join(changed) or "nothing (already done?)")
if left:
    print("\nThese lines still mention AI. Check each one and tell me if you want it changed:")
    for l in left: print("  " + l)
else:
    print("No other 'no AI' lines found.")
