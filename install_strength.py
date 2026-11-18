#!/usr/bin/env python3
"""install_strength.py - run from the project folder (~/W):  python3 install_strength.py
Answer Finder: every answer now shows how well it matches the question (Strong / Partial / Weak match), and when no answer
is strong the page says "No strong matching evidence found" instead of presenting weak sentences as answers.
The strength comes from the same rule score the Answer Finder already uses. Nothing is written by AI; the sentences are
still exact quotes from real papers. Safe to run twice."""
import os, sys
SPEC = {
 "answer_tool.py": [
  ("def _rank(c):\n",
   'def _strength(c):\n    """strong / partial / weak, from the sentence score (the same score that decides which sentences are shown)."""\n'
   '    s = c.get("score") or 0\n    return "strong" if s >= BEST_SCORE else ("partial" if s >= BEST_SCORE - 1 else "weak")\n\n\ndef _rank(c):\n'),
  ('            "best_index": best,\n',
   '            "best_index": best,\n            "strength": [_strength(c) for c in top],\n            "weak_only": bool(top) and not any(_strength(c) == "strong" for c in top),\n'),
 ],
 "answer.html": [
  (".best-b{",
   ".st-b{margin-left:6px;font-size:.66rem;padding:1px 8px;border-radius:20px;border:1px solid var(--border);font-weight:600}\n"
   ".st-b.strong{color:var(--green);background:var(--green-bg);border-color:transparent}\n"
   ".st-b.partial{color:var(--orange);background:var(--orange-bg);border-color:transparent}\n"
   ".st-b.weak{color:var(--muted);background:var(--surf2)}\n"
   ".weak-note{margin:0 0 12px;padding:11px 14px;border:1px solid var(--orange);background:var(--orange-bg);color:var(--text);border-radius:12px;font-size:.86rem;line-height:1.5}\n"
   ".best-b{"),
  ("${data.best_index === i ? ' <span class=\"best-b\">★ Best answer</span>' : ''}</div>",
   "${data.best_index === i ? ' <span class=\"best-b\">★ Best answer</span>' : ''}${(data.strength || [])[i] ? ' <span class=\"st-b ' + data.strength[i] + '\" title=\"' + ({strong: 'Matches the topic and the wording of your question', partial: 'Related to your question but may not answer it directly', weak: 'Mentions the topic but may not answer the question'}[data.strength[i]]) + '\">' + ({strong: 'Strong match', partial: 'Partial match', weak: 'Weak match'}[data.strength[i]]) + '</span>' : ''}</div>"),
  ("      ${fUsedLine(data)}\n      ${cards}\n",
   "      ${fUsedLine(data)}\n      ${data.weak_only ? '<div class=\"weak-note\"><b>No strong matching evidence found.</b> These are the closest sentences from real papers. A sentence can be similar to your question without answering it, so open the source before you rely on it.</div>' : ''}\n      ${cards}\n"),
 ],
}
def find(name):
    for c in (name, os.path.join("routes", name), os.path.join("templates", name)):
        if os.path.isfile(c): return c
def read(p): return open(p, encoding="utf-8").read()
def write(p, t): open(p + ".new", "w", encoding="utf-8", newline="").write(t); os.replace(p + ".new", p)
def done(t, old, new): return new in t or (new.endswith(old) and new[:-len(old)] in t)
ok, warn = [], []
for name, pairs in SPEC.items():
    p = find(name)
    if not p: warn.append(name + ": not found, left alone"); continue
    t, n, bad = read(p), 0, []
    for old, new in pairs:
        if done(t, old, new): continue
        if t.count(old) == 1 or (name.endswith(".html") and t.count(old) > 1):
            t = t.replace(old, new, 1); n += 1
        else: bad.append(old[:60].replace("\n", " "))
    if bad: warn.append(name + ": left alone, did not match: " + " | ".join(bad)); continue
    if n: write(p, t); ok.append(p)
print("Updated:", ", ".join(ok) or "nothing (already done?)")
for w in warn: print("  warning:", w)
