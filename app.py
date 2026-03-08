"""
╔══════════════════════════════════════════════════════════════════╗
║           Sturch — Flask Backend  app.py                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  WHAT THIS FILE DOES:                                            ║
║  This is the main server file. It handles:                       ║
║  - User signup and login                                         ║
║  - Search requests (calls OpenAlex API)                          ║
║  - Search counter (10 free, then paywall)                        ║
║  - IP rate limiting (max 3 accounts per IP per day)              ║
║  - Serving the website to users                                  ║
║                                                                  ║
║  HOW TO RUN:                                                      ║
║  python app.py                                                   ║
║  Then open: localhost:5000                                       ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ─── IMPORTS ────────────────────────────────────────────────────────
import os
import hashlib
import secrets
import requests
from datetime import datetime, timedelta
from functools import wraps


from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for
)
from flask_cors import CORS
from dotenv import load_dotenv

# Load secret keys from .env file
load_dotenv()

# ─── APP SETUP ──────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "sturch-secret-key")
CORS(app)

# ─── SUPABASE SETTINGS ──────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY")

def sb_headers():
    """Headers for Supabase API requests."""
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }

def sb_get(table, filters=""):
    """GET request to Supabase table."""
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.get(url, headers=sb_headers())
    return resp.json() if resp.ok else []

def sb_post(table, data):
    """INSERT into Supabase table."""
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=sb_headers(), json=data)
    return resp.json() if resp.ok else None

def sb_patch(table, filters, data):
    """UPDATE Supabase table."""
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.patch(url, headers=sb_headers(), json=data)
    return resp.ok

# ─── SETTINGS ───────────────────────────────────────────────────────
def get_int_env(name, default):
    value = os.getenv(name)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

FREE_SEARCHES    = get_int_env("FREE_SEARCHES", 10)
PAID_SEARCHES    = get_int_env("PAID_SEARCHES", 20)
MAX_ACCOUNTS_PER_IP = get_int_env("MAX_ACCOUNTS_PER_IP", 3)
OPENALEX_URL     = "https://api.openalex.org/works"

# ─── IP RATE LIMITING ────────────────────────────────────────────────
# Now stored in Supabase so it survives redeploys.
# Requires a table: ip_signups (id int8 pk, ip text, created_at timestamp)

def get_client_ip():
    """Get real IP even behind proxy/Railway."""
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

def is_ip_allowed_to_signup(ip):
    """Check if IP has made less than MAX_ACCOUNTS_PER_IP signups today."""
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    result = sb_get("ip_signups", f"ip=eq.{ip}&created_at=gte.{today_start}")
    return len(result) < MAX_ACCOUNTS_PER_IP

def record_ip_signup(ip):
    """Record a signup from this IP into Supabase."""
    sb_post("ip_signups", {
        "ip":         ip,
        "created_at": datetime.now().isoformat()
    })

# ─── LOGIN REQUIRED ─────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ─── PASSWORD FUNCTIONS ─────────────────────────────────────────────
def hash_password(password):
    """Hash a password with a random salt."""
    salt = secrets.token_hex(16)
    h    = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{h}"

def check_password(password, hashed):
    """Check if password matches stored hash."""
    try:
        salt, h = hashed.split(":")
        return hashlib.sha256((password + salt).encode()).hexdigest() == h
    except Exception:
        return False

# ─── USER HELPERS ────────────────────────────────────────────────────
def get_user(user_id):
    result = sb_get("users", f"id=eq.{user_id}&select=*")
    return result[0] if result else None

def get_user_by_email(email):
    result = sb_get("users", f"email=eq.{email}&select=*")
    return result[0] if result else None

def can_search(user):
    total = FREE_SEARCHES + (user.get('paid_searches', 0))
    return user.get('search_count', 0) < total

def searches_remaining(user):
    total = FREE_SEARCHES + (user.get('paid_searches', 0))
    return max(0, total - user.get('search_count', 0))

# ─── OPENALEX HELPERS ────────────────────────────────────────────────
def reconstruct_abstract(abstract_index):
    if not abstract_index:
        return ""
    words = []
    for word, positions in abstract_index.items():
        for pos in positions:
            words.append((pos, word))
    words.sort(key=lambda x: x[0])
    return " ".join(w for _, w in words)

def format_paper(paper):
    loc    = (paper.get("primary_location") or {})
    source = (loc.get("source") or {})
    oa     = (paper.get("open_access") or {})
    biblio = (paper.get("biblio") or {})

    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in paper.get("authorships", [])
        if (a.get("author") or {}).get("display_name")
    ]

    concepts = [
        c.get("display_name", "")
        for c in sorted(
            paper.get("concepts") or [],
            key=lambda x: x.get("score", 0), reverse=True
        )[:5]
        if c.get("display_name")
    ]

    abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))
    year     = paper.get("publication_year", "n.d.")
    title    = paper.get("title", "")
    journal  = source.get("display_name", "")
    doi      = paper.get("doi", "")

    # Build APA authors
    apa_authors = []
    for a in paper.get("authorships", [])[:3]:
        name = (a.get("author") or {}).get("display_name", "")
        if name:
            parts = name.split()
            if len(parts) >= 2:
                apa_authors.append(f"{parts[-1]}, {parts[0][0]}.")
            else:
                apa_authors.append(name)
    if len(paper.get("authorships", [])) > 3:
        apa_authors.append("et al.")

    # Build volume, issue, pages
    volume     = biblio.get("volume", "")
    issue      = biblio.get("issue", "")
    first_page = biblio.get("first_page", "")
    last_page  = biblio.get("last_page", "")
    pages      = f"{first_page}\u2013{last_page}" if first_page and last_page else ""

    vol_issue = ""
    if volume and issue:
        vol_issue = f", {volume}({issue})"
    elif volume:
        vol_issue = f", {volume}"

    # Track missing APA fields
    missing = []
    if not volume:  missing.append("volume")
    if not issue:   missing.append("issue")
    if not pages:   missing.append("page range")
    if not doi:     missing.append("DOI")

    pages_part = f", {pages}" if pages else ""
    doi_part   = f" {doi}" if doi else ""
    apa = f"{', '.join(apa_authors) or 'Unknown'} ({year}). {title}. {journal}{vol_issue}{pages_part}.{doi_part}"

    return {
        "title":         title,
        "authors":       authors,
        "year":          year,
        "journal":       journal,
        "abstract":      abstract,
        "citations":     paper.get("cited_by_count", 0),
        "is_oa":         oa.get("is_oa", False),
        "oa_url":        oa.get("oa_url", ""),
        "doi":           doi,
        "concepts":      concepts,
        "openalex_id":   paper.get("id", ""),
        "volume":        volume,
        "issue":         issue,
        "pages":         pages,
        "apa_reference": apa,
        "apa_missing":   missing,
        "data_source":   "OpenAlex (openalex.org)",
        "ref_warning":   "Auto-generated — verify before academic use",
    }

# ─── ROUTES ──────────────────────────────────────────────────────────

@app.route('/')
def home():
    if 'user_id' in session:
        return render_template('index.html')
    return redirect(url_for('login'))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if 'user_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        data     = request.get_json() or request.form
        email    = (data.get('email') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"error": "Email and password required"}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        if '@' not in email:
            return jsonify({"error": "Enter a valid email address"}), 400

        # ─── IP RATE LIMIT CHECK ────────────────────────────────────
        ip = get_client_ip()
        if not is_ip_allowed_to_signup(ip):
            return jsonify({
                "error": f"Too many accounts created from your device today. Maximum is {MAX_ACCOUNTS_PER_IP} per day."
            }), 429

        if get_user_by_email(email):
            return jsonify({"error": "Email already registered. Please login."}), 400

        hashed = hash_password(password)
        result = sb_post("users", {
            "email":         email,
            "password_hash": hashed,
            "search_count":  0,
            "is_paid":       False,
            "paid_searches": 0,
            "created_at":    datetime.now().isoformat(),
        })

        if not result:
            return jsonify({"error": "Could not create account. Try again."}), 500

        # Record this IP signup
        record_ip_signup(ip)

        new_user = result[0] if isinstance(result, list) else result
        session['user_id']    = new_user['id']
        session['user_email'] = new_user['email']

        return jsonify({"success": True, "redirect": "/"})

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        data     = request.get_json() or request.form
        email    = (data.get('email') or '').strip().lower()
        password = (data.get('password') or '').strip()

        if not email or not password:
            return jsonify({"error": "Email and password required"}), 400

        user = get_user_by_email(email)
        if not user or not check_password(password, user['password_hash']):
            return jsonify({"error": "Invalid email or password"}), 401

        session['user_id']    = user['id']
        session['user_email'] = user['email']

        return jsonify({"success": True, "redirect": "/"})

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/me')
@login_required
def get_me():
    user = get_user(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "email":              user['email'],
        "search_count":       user['search_count'],
        "searches_remaining": searches_remaining(user),
        "total_allowed":      FREE_SEARCHES + user.get('paid_searches', 0),
        "is_paid":            user.get('is_paid', False),
        "can_search":         can_search(user),
    })


@app.route('/api/search')
@login_required
def search():
    query     = request.args.get('q', '').strip()
    page      = int(request.args.get('page', 1))
    year_from = request.args.get('year_from', '')
    year_to   = request.args.get('year_to', '')

    if not query:
        return jsonify({"error": "Please enter a search query"}), 400

    user = get_user(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    if not can_search(user):
        return jsonify({
            "error":   "limit_reached",
            "message": "You have used all your free searches. Pay $1 for 20 more!",
        }), 403

    params = {
        "search":   query,
        "per-page": 25,
        "page":     page,
        "sort":     "cited_by_count:desc",
    }

    if year_from and year_to:
        params["filter"] = f"publication_year:{year_from}-{year_to}"
    elif year_from:
        params["filter"] = f"publication_year:{year_from}-"

    try:
        resp   = requests.get(OPENALEX_URL, params=params, timeout=15)
        data   = resp.json()
        papers = data.get("results", [])
        total  = data.get("meta", {}).get("count", 0)
    except Exception as e:
        return jsonify({"error": f"Search failed: {e}"}), 500

    formatted = [format_paper(p) for p in papers]

    new_count = user['search_count'] + 1
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_count})

    sb_post("search_logs", {
        "user_id":    user['id'],
        "query":      query,
        "results":    len(formatted),
        "searched_at": datetime.now().isoformat(),
    })

    total_allowed = FREE_SEARCHES + user.get('paid_searches', 0)

    return jsonify({
        "results":            formatted,
        "total":              total,
        "query":              query,
        "searches_remaining": max(0, total_allowed - new_count),
        "data_source":        "OpenAlex (openalex.org)",
        "ref_disclaimer":     "References auto-generated — verify before academic use",
    })


@app.route('/upgrade')
@login_required
def upgrade():
    user = get_user(session['user_id'])
    return render_template('upgrade.html', user=user)


@app.route('/health')
def health():
    return jsonify({"status": "ok", "app": "Sturch", "version": "1.0-beta"})


# ─── RUN ────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
