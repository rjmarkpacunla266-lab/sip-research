"""routes/pages.py — Page routes: home, landing, news, donate, health"""
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, jsonify
from core import login_required

pages_bp = Blueprint("pages", __name__)

# ── WHAT'S NEW ──────────────────────────────────────────────────────
# Add a new update at the TOP of this list. The newest one is shown on the landing page strip and on /news.
# Fields: date (YYYY-MM-DD), tag, title, summary (optional), points (list), link / link_text (optional)
NEWS = [
    {"date": "2026-10-18", "tag": "Source Tracer", "title": "Quick Trace added to Source Tracer",
     "summary": "Find where a quote came from, faster and more often.",
     "points": ["New Quick Trace button: a fast first answer from open-access papers, clearly labelled as less reliable.",
                "Trace Source now shows the quick result first, then keeps searching for up to 2 minutes if it is not convinced.",
                "Open-access papers are read in full text, so quotes copied from the body of a paper (not only the abstract) can be found.",
                "Better matching for long quotes."],
     "link": "/source-tracer", "link_text": "Open Source Tracer"},
    {"date": "2026-10-18", "tag": "Topic Finder", "title": "Smarter filters in the Topic Finder",
     "summary": "Less scrolling, more useful papers.",
     "points": ["Pick your level (High School, College, Graduate) and the filters are set for you.",
                "New Paper type filter: reviews and overviews, or original studies.",
                "One-tap recent years (last 3, 5 or 10) and a new Recommended sort that balances recent and well-cited papers.",
                "Narrowing a filter now hides papers instantly, with counts next to each filter.",
                "Broad subjects like \"biology\" get a hint to try something specific, like \"biofuel\".",
                "No dead ends: when a search finds too little, buttons show which filter to relax.",
                "Cleaner sub-topics and research gaps."],
     "link": "/topic-generator", "link_text": "Open Topic Finder"},
    {"date": "2026-10-17", "tag": "Accounts", "title": "Sign up with just a username, or try Sturch as a guest",
     "points": ["No email needed. Choose a username and password.",
                "You get a recovery code to reset your password. Keep it somewhere safe.",
                "Guest mode lets you try Sturch right away. Guest searches and saved papers are kept for 30 days, and you can turn a guest into a full account at any time."],
     "link": "/signup", "link_text": "Create an account"},
    {"date": "2026-10-15", "tag": "Answer Finder", "title": "More accurate answers in the Answer Finder",
     "points": ["Stricter sentence checks, so fewer off-topic or half-finished sentences appear as answers."],
     "link": "/answer", "link_text": "Open Answer Finder"},
]

def _news():
    out = []
    for n in NEWS:
        d = dict(n)
        try:
            d["date_text"] = datetime.strptime(n["date"], "%Y-%m-%d").strftime("%b %d, %Y").replace(" 0", " ")
        except ValueError:
            d["date_text"] = n["date"]
        out.append(d)
    return out

@pages_bp.route("/")
def home():
    if "user_id" in session:
        return render_template("home.html")
    news = _news()
    return render_template("landing.html", latest=news[0] if news else None)

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


@pages_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Sturch", "version": "3.0-blueprints"})


