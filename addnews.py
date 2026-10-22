#!/usr/bin/env python3
"""addnews.py: add, list or remove "What's new" entries in news.json, from any terminal. No AI, no code editing.

  python addnews.py            add an update (it asks you step by step)
  python addnews.py --list     show the updates with their numbers
  python addnews.py --remove 3 remove update number 3 (a backup is kept in news.json.bak)

Then publish it:  git add news.json && git commit -m "news" && git push origin main
"""
import json, os, sys, shutil
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(HERE, "news.json")
TAGS = ["New", "Search", "Answer Finder", "Source Tracer", "Topic Finder", "Guide", "Accounts", "Fix", "Design"]
LINKS = {"search": "/search", "answer": "/answer", "tracer": "/source-tracer", "topic": "/topic-generator",
         "guide": "/guide", "saved": "/saved", "news": "/news", "donate": "/donate"}


def load():
    if not os.path.exists(FILE):
        return []
    with open(FILE, encoding="utf-8") as f:
        return json.load(f)


def save(items):
    if os.path.exists(FILE):
        shutil.copyfile(FILE, FILE + ".bak")
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, FILE)          # never leaves a half-written file


def ask(prompt, default=""):
    s = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return s or default


def show(items):
    if not items:
        print("No updates yet.")
    for i, n in enumerate(items, 1):
        print(f"{i:>2}. {n.get('date')}  [{n.get('tag','')}]  {n.get('title')}")


def add():
    items = load()
    print("Add an update. Press Enter to accept [defaults]. Ctrl+C to cancel.\n")
    while True:
        d = ask("Date (YYYY-MM-DD)", date.today().isoformat())
        try:
            datetime.strptime(d, "%Y-%m-%d"); break
        except ValueError:
            print("  Use the format 2026-10-22")
    print("Tags: " + ", ".join(f"{i}={t}" for i, t in enumerate(TAGS, 1)) + "  (or type your own)")
    t = ask("Tag", "New")
    tag = TAGS[int(t) - 1] if t.isdigit() and 1 <= int(t) <= len(TAGS) else t
    title = ""
    while not title:
        title = ask("Title")
    summary = ask("One-line summary (optional)")
    print("Bullet points, one per line. Press Enter on an empty line to finish.")
    points = []
    while True:
        p = input(f"  • ").strip()
        if not p:
            break
        points.append(p)
    print("Link: " + ", ".join(f"{k}={v}" for k, v in LINKS.items()) + "  (or type a path like /answer, or leave empty)")
    l = ask("Link")
    link = LINKS.get(l.lower(), l)
    if link and not (link.startswith("/") or link.startswith("http")):
        print("  Links should start with / or http. Ignoring it."); link = ""
    link_text = ask("Link text", "Open") if link else ""
    entry = {"date": d, "tag": tag, "title": title}
    if summary: entry["summary"] = summary
    if points: entry["points"] = points
    if link: entry["link"] = link; entry["link_text"] = link_text
    print("\nPreview:\n" + json.dumps(entry, indent=2, ensure_ascii=False))
    if ask("\nSave it? (y/n)", "y").lower() != "y":
        print("Cancelled."); return
    items.insert(0, entry)
    save(items)
    print(f"Saved. {len(items)} updates in news.json.\nNext: git add news.json && git commit -m \"news\" && git push origin main")


def remove(n):
    items = load()
    if not 1 <= n <= len(items):
        print("No update with that number. Use --list."); return
    print("Remove:", items[n - 1].get("title"))
    if ask("Sure? (y/n)", "n").lower() == "y":
        items.pop(n - 1); save(items); print("Removed. (backup: news.json.bak)")


if __name__ == "__main__":
    try:
        a = sys.argv[1:]
        if a[:1] == ["--list"]: show(load())
        elif a[:1] == ["--remove"] and len(a) > 1 and a[1].isdigit(): remove(int(a[1]))
        elif not a: add()
        else: print(__doc__)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
