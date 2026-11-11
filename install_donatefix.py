#!/usr/bin/env python3
"""install_donatefix.py - run from ~/W:  python3 install_donatefix.py
Removes the "Covering data and service costs" line from the donate page's "Where it goes" list."""
import os, re
p = os.path.join("templates", "donate.html")
if not os.path.isfile(p):
    raise SystemExit("templates/donate.html not found - run this from ~/W")
t = open(p, encoding="utf-8").read()
new = re.sub(r'[ \t]*<div class="wr"><i>[^<]*</i><span>Covering data and service costs</span></div>\n?', "", t)
if new == t:
    print("Nothing to change (already removed?)")
else:
    open(p, "w", encoding="utf-8", newline="").write(new)
    print("Removed the 'Covering data and service costs' line from templates/donate.html")
