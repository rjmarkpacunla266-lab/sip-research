"""routes/pages.py — Page routes: home, landing, news, donate, health"""
from datetime import datetime
from flask import Blueprint, render_template, session, redirect, url_for, jsonify
from core import login_required

pages_bp = Blueprint("pages", __name__)

# ── WHAT'S NEW ──────────────────────────────────────────────────────
# Add a new update at the TOP of this list. The newest one is shown on the landing page strip and on /news.
# Fields: date (YYYY-MM-DD), tag, title, summary (optional), points (list), link / link_text (optional)
NEWS = [
    {"date": "2026-10-20", "tag": "Guide", "title": "New Guide: how to use every Sturch tool",
     "summary": "A step-by-step tutorial for the whole site, in one page.",
     "points": ["Search Papers: Simple mode (like Google Scholar) and Advanced mode, filters, tags and result buttons.",
                "The paper reader: find words inside a paper and copy citations in APA, MLA, Chicago or Harvard.",
                "Answer Finder: question types, the \"I read this as\" line, quotes with context, and Continue searching.",
                "Source Tracer (Quick Trace and full trace) and Topic Finder (topic size, narrowing trail, papers per year, methods and gap types).",
                "Your library: Saved papers, Collections, History and Export, plus good research habits and a short FAQ."],
     "link": "/guide", "link_text": "Open the Guide"},
    {"date": "2026-10-20", "tag": "Answer Finder", "title": "Answer Finder: keep searching for more answers",
     "summary": "Not happy with the answers? Let Sturch read deeper.",
     "points": ["A new Continue searching button reads for up to 1 more minute, going past the papers already read.",
                "It looks at the next pages of results and reads the full text of new open-access papers.",
                "New answers appear below the old ones, grouped by round, and the best answer badge moves if a better one is found.",
                "You can press it again and again, or stop a round at any time."],
     "link": "/answer", "link_text": "Open Answer Finder"},
    {"date": "2026-10-19", "tag": "New", "title": "Paper counter: how many papers can Sturch search?",
     "summary": "A live count from each database, shown on the home page.",
     "points": ["Total papers per database: OpenAlex, Crossref, PubMed, Europe PMC and arXiv.",
                "How many of them are open access (free to read) and how many are not.",
                "Because one paper can be in several databases, we show a range: at least this many different papers, and up to this many records."],
     "link": "/#numbers", "link_text": "See the numbers"},
    {"date": "2026-10-19", "tag": "Search", "title": "Search Papers: a simpler Simple mode, plus Advanced mode",
     "summary": "Search now opens in a clean, Google Scholar style view. Need every option? Switch on Advanced mode.",
     "points": ["Simple mode is the new default: one search box, and just three filters (date, sort and free to read).",
                "Results show the title, authors, a short snippet, how many times it was cited, and Cite, Save and Read.",
                "Advanced mode is the full search you know, with tags, all sources, title or abstract only and every filter.",
                "Use the Advanced mode switch in the top corner. Sturch remembers your choice, and your results stay when you switch."],
     "link": "/search", "link_text": "Open Search"},
    {"date": "2026-10-19", "tag": "Answer Finder", "title": "Answer Finder: more questions, quotes and citations",
     "summary": "Ask more kinds of questions and use the answers in your paper.",
     "points": ["New question types: Benefits of, Risks of, Examples of, How to, and Is … safe/effective.",
                "Type a whole question. If it fits no pattern, Sturch shows the closest sentences and says so.",
                "See how your question was read, and switch to another reading with one tap.",
                "Show the sentences around an answer, copy it with an APA citation, or save its paper to your library."],
     "link": "/answer", "link_text": "Open Answer Finder"},
    {"date": "2026-10-19", "tag": "Topic Finder", "title": "Topic Finder: topic size, narrowing trail and local check",
     "summary": "Pick a topic that is not too big and not too small.",
     "points": ["Topic size meter: shows how many papers contain all your words, from too narrow to too broad.",
                "Narrowing trail: every sub-topic you tap adds a step with its paper count, and you can step back at any time.",
                "Local check: see how many papers on your topic mention the Philippines or have Philippine authors.",
                "Faster results: papers and sub-topics appear first, and gaps from the full papers join a few seconds later.",
                "Save any paper (or the paper behind a research gap) to your library with one tap.",
                "Copy or download a topic brief with your sub-topics, gaps and APA references.",
                "Papers-per-year bars: see when the topic grew, and tap a year to look at just that year.",
                "Methods seen: surveys, experiments, simulations and more, with a tap to show only those papers.",
                "Gap types: filter research gaps by unclear, understudied or needs more research."],
     "link": "/topic-generator", "link_text": "Open Topic Finder"},
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


@pages_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Sturch", "version": "3.0-blueprints"})


