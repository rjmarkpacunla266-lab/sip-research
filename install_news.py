#!/usr/bin/env python3
"""install_news.py - run from the project folder (~/W):  python3 install_news.py
Adds the recent updates to news.json (newest first). Skips any update whose title is already there, so it is safe to run twice.
Edit the dates in news.json afterwards if you want them to match the real release days."""
import json, os, sys
NEW = [
 {"date": "2026-11-16", "tag": "New", "title": "Source types and the APA 7 guide",
  "summary": "Every paper now shows what kind of source it is, and APA follows the APA 7 reference guide.",
  "points": ["Each result is labelled: journal article, book, book chapter, preprint, conference paper, report, thesis, dataset or web page.",
             "The Cite window says whether the link opens as a PDF or a web page.",
             "APA now uses sentence-case titles, an italic journal name and volume, en-dash page ranges and DOI links. Italics stay when you paste into Word.",
             "Notes under the reference tell you what to check, such as proper nouns in the title.",
             "Crossref results now bring volume, issue and page numbers, so fewer references say \"not found\".",
             "Other source types are labelled but still use the old format until their guide is added."],
  "link": "/search", "link_text": "Try the Cite window"},
 {"date": "2026-11-16", "tag": "New", "title": "Choose how authors are shown in citations",
  "summary": "A new Authors option in the Cite window and on the Citations page.",
  "points": ["Automatic follows the rules of each style.",
             "Show all authors lists everyone.",
             "Shorten with et al. keeps only the first author followed by et al. when there are three or more.",
             "Your choice is remembered on this device."],
  "link": "/citations", "link_text": "Open Citations"},
 {"date": "2026-11-15", "tag": "New", "title": "Smart search is on by default",
  "summary": "Smart search finds papers by meaning, not only by the exact words you typed.",
  "points": ["Papers it adds are marked ✨ Smart pick; papers it moved up are marked ✨ Smart boost.",
             "A pick only appears if the normal search would not have shown it.",
             "Every result is still a real paper with a real source. Sturch does not write answers with AI.",
             "You can switch it off with the Smart search option when you search."],
  "link": "/search", "link_text": "Try a search"},
 {"date": "2026-11-15", "tag": "Fix", "title": "Result counts are correct",
  "summary": "The results header no longer says \"1 results\" when more papers are shown.",
  "points": [], "link": "", "link_text": ""},
]
path = "news.json"
try:
    items = json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else []
    if not isinstance(items, list): raise ValueError("news.json is not a list")
except Exception as e:
    print("news.json could not be read (%s). Nothing changed." % e); sys.exit(1)
have = {str(n.get("title")) for n in items if isinstance(n, dict)}
add = [n for n in NEW if n["title"] not in have]
if not add:
    print("Already there. Nothing to add."); sys.exit(0)
items = add + items
tmp = path + ".new"
json.dump(items, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
os.replace(tmp, path)
print("Added %d update(s) to news.json:" % len(add))
for n in add: print("  -", n["title"])
