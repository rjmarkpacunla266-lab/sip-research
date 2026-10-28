"""routes/pages.py — Page routes: home, landing, news, donate, health"""
import json
import os
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, jsonify
from core import login_required

pages_bp = Blueprint("pages", __name__)

# ── WHAT'S NEW ──────────────────────────────────────────────────────
# The updates live in news.json (project root), newest first. Add one from a terminal with:
#     python addnews.py
# (no code editing needed; commit news.json and push). Fields: date, tag, title, summary, points, link, link_text.
NEWS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news.json")
_news_cache = {"mtime": None, "items": []}

def _load_news():
    """Read news.json; re-read only when the file changes. A broken or missing file never breaks the site."""
    try:
        m = os.path.getmtime(NEWS_FILE)
        if m != _news_cache["mtime"]:
            with open(NEWS_FILE, encoding="utf-8") as f:
                items = json.load(f)
            items = [n for n in items if isinstance(n, dict) and n.get("title") and n.get("date")]
            items.sort(key=lambda n: str(n["date"]), reverse=True)   # newest first (stable: same-day order is kept)
            _news_cache.update(mtime=m, items=items)
    except Exception:
        pass            # keep the last good list
    return _news_cache["items"]

def _news():
    out = []
    for n in _load_news():
        d = dict(n)
        try:
            d["date_text"] = datetime.strptime(n["date"], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            d["date_text"] = n["date"]
        out.append(d)
    return out

@pages_bp.route("/")
def home():
    news = _news()
    if "user_id" in session:
        return render_template("home.html", latest=news[0] if news else None)
    return render_template("landing.html", latest=news[0] if news else None)

@pages_bp.route("/api/paper-count")
def paper_count():
    """How many papers Sturch can search, counted from each database (cached for hours, see papercount.py). Public."""
    try:
        import papercount
        data = papercount.get_counts()
    except Exception:
        data = None
    if not data:
        return jsonify({"error": "The paper count is not available right now."}), 503
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp

@pages_bp.route("/guide")
def guide():
    return render_template("guide.html")

@pages_bp.route("/news")
def news():
    return render_template("news.html", updates=_news())

@pages_bp.route("/search")
@login_required
def search_page():
    return render_template("index.html")

@pages_bp.route("/saved")
@login_required
def saved_page():
    return render_template("saved.html")

@pages_bp.route("/history")
@login_required
def history_page():
    return render_template("history.html")

@pages_bp.route("/collections")
@login_required
def collections_page():
    return render_template("collections.html")

@pages_bp.route("/citations")
@login_required
def citations_page():
    return render_template("citations.html")

@pages_bp.route("/donate")
def donate():
    return render_template("donate.html")


@pages_bp.route("/shop")
def shop():
    return render_template("shop.html")


@pages_bp.route("/policy")
def policy():
    return render_template("policy.html")


@pages_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Sturch", "version": "3.0-blueprints"})


