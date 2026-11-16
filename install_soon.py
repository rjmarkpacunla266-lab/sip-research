#!/usr/bin/env python3
"""install_soon.py - run from the project folder (~/W):  python3 install_soon.py
Adds 4 greyed-out "Coming soon" squares under your tool squares on the home page (templates/home.html).
Safe to run twice. To name a tool later, change its "Coming soon" heading and text in home.html."""
import os, sys
p = next((c for c in ("home.html", os.path.join("templates", "home.html")) if os.path.isfile(c)), None)
if not p:
    print("home.html not found. Run this from the project folder (~/W)."); sys.exit(1)
t = open(p, encoding="utf-8").read()
if 'class="tool soon"' in t:
    print("Already added. Nothing changed."); sys.exit(0)
CSS_OLD = ".tool:hover .open{background:var(--accent-h)}\n"
CSS_NEW = CSS_OLD + (".tool.soon{opacity:.62;border-style:dashed;box-shadow:none;cursor:default}\n"
  ".tool.soon:hover{transform:none;border-color:var(--border)}\n"
  ".tool.soon .ti{background:var(--surf2);color:var(--muted)}\n"
  ".tool.soon:hover .open,.tool.soon .open{background:var(--surf2);color:var(--muted);border:1px solid var(--border)}\n")
HTML_OLD = '<h3>Topic Ideas</h3><p>Narrow down a research topic.</p><span class="open">Open</span></a>\n'
ICON = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h12M6 21h12"/><path d="M7 3c0 5 5 6 5 9s-5 4-5 9M17 3c0 5-5 6-5 9s5 4 5 9"/></svg>'
TILE = ('      <div class="tool soon" aria-disabled="true">\n        <div class="ti">' + ICON + '</div>\n'
        '        <h3>New tool</h3><p>Coming soon.</p><span class="open">Coming soon</span></div>\n')
HTML_NEW = HTML_OLD + TILE * 4
if t.count(CSS_OLD) != 1 or t.count(HTML_OLD) != 1:
    print("The tool squares in home.html do not look the way I expected. Nothing changed."); sys.exit(1)
t = t.replace(CSS_OLD, CSS_NEW, 1).replace(HTML_OLD, HTML_NEW, 1)
open(p + ".new", "w", encoding="utf-8").write(t); os.replace(p + ".new", p)
print("Added 4 Coming soon squares to", p)
