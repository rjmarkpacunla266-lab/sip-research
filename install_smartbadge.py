#!/usr/bin/env python3
"""install_smartbadge.py - run from the project folder (~/W):  python3 install_smartbadge.py
Puts a small badge on every result that smart search touched:
   "✨ Smart pick"   = found only by smart search (by meaning)
   "✨ Smart boost"  = found by the normal search too, and moved up by smart search
Results smart search did not touch show no badge. Needs install_smart2 first. Safe to run twice."""
import os

def find(name):
    for c in (name, os.path.join("routes", name), os.path.join("templates", name)):
        if os.path.isfile(c): return c
SPEC = {
 "search.py": [('            p["via"] = "similar meaning (OpenAlex AI search)"\n',
                '            p["via"] = "similar meaning (OpenAlex AI search)"\n            p["smart_only"] = True\n')],
 "index.html": [
  ("function oaBadgeHtml(item) {",
   "function smartBadgeHtml(item) {\n  if (!item.smart) return '';\n"
   "  return item.smart_only ? '<span class=\"sm-b\" title=\"Found only by smart search, by meaning\">✨ Smart pick</span>'\n"
   "                         : '<span class=\"sm-b\" title=\"Moved up by smart search\">✨ Smart boost</span>';\n}\n"
   "function oaBadgeHtml(item) {"),
  ("${oaBadgeHtml(item)}${adv ? sourceBadge(item.data_source) : ''}", "${oaBadgeHtml(item)}${smartBadgeHtml(item)}${adv ? sourceBadge(item.data_source) : ''}"),
  (".pw-b{", ".sm-b{font-size:.74rem;font-weight:600;padding:2px 10px;border-radius:999px;background:var(--surf2);border:1px solid var(--border);color:var(--accent,#2f5bea);white-space:nowrap}\n.pw-b{"),
 ]}
def read(p): return open(p, encoding="utf-8").read()
ok, warn = [], []
for name, pairs in SPEC.items():
    p = find(name)
    if not p: warn.append(name + ": not found"); continue
    t = read(p); n = 0; bad = []
    for old, new in pairs:
        if new in t or (new.endswith(old) and new[:-len(old)] in t): continue
        if t.count(old) >= 1 and (name == "index.html" or t.count(old) == 1):
            t = t.replace(old, new, 1); n += 1
        else: bad.append(old[:50])
    if bad: warn.append(name + ": left alone, did not match: " + " | ".join(bad)); continue
    if n:
        open(p + ".new", "w", encoding="utf-8", newline="").write(t); os.replace(p + ".new", p); ok.append(p)
print("Updated:", ", ".join(ok) or "nothing (already done?)")
for w in warn: print("  warning:", w)
