"""
╔══════════════════════════════════════════════════════════════════╗
║           Sturch — Flask Backend  app.py  v2.0               ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  WHAT THIS FILE DOES:                                            ║
║  This is the main server file. It handles:                       ║
║  - User signup and login                                         ║
║  - Search requests (calls MULTIPLE APIs in parallel)             ║
║      • OpenAlex  — 250M+ academic works                         ║
║      • Semantic Scholar — 200M+ papers, great for CS/AI         ║
║      • arXiv — cutting-edge CS, Math, Physics (all open access) ║
║      • PubMed — gold standard for medicine and biology          ║
║  - Deduplication of results across sources (by DOI + title)      ║
║  - Search counter (10 free, then paywall)                        ║
║  - IP rate limiting (max 3 accounts per IP per day)              ║
║  - Serving the website to users                                  ║
║                                                                  ║
║  HOW TO RUN:                                                     ║
║  python app.py                                                   ║
║  Then open: localhost:5000                                       ║
║                                                                  ║
║  NEW IN v2.0:                                                    ║
║  - 4 APIs searched simultaneously using ThreadPoolExecutor       ║
║  - Results merged and deduplicated before returning to frontend  ║
║  - Each result carries a data_source label for the frontend      ║
║  - No new API keys needed — all sources are free and open        ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ─── IMPORTS ────────────────────────────────────────────────────────
import os
import hashlib
import secrets
import requests
import xml.etree.ElementTree as ET                # arXiv returns Atom/XML
from bs4 import BeautifulSoup                     # parse full paper HTML/XML
from datetime import datetime, timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed  # parallel API calls

from flask import (
    Flask, render_template, request,
    jsonify, session, redirect, url_for
)
from flask_cors import CORS
from dotenv import load_dotenv

# Load secret keys from .env file
load_dotenv()

# ─── EMAIL CONFIG (Resend) ───────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
GMAIL_USER     = os.getenv("GMAIL_USER", "pacunlarjmark@gmail.com")  # used as sender name
APP_URL        = os.getenv("APP_URL", "http://localhost:8080")

# ─── PAYMONGO CONFIG ─────────────────────────────────────────────────
PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "")
PAYMONGO_PUBLIC_KEY = os.getenv("PAYMONGO_PUBLIC_KEY", "")


# ─── EMAIL NORMALIZATION ─────────────────────────────────────────────
def normalize_email(email):
    """
    Normalize Gmail addresses to prevent duplicate account tricks.
    - Removes dots from username (u.s.e.r = user)
    - Removes +tags (user+spam = user)
    - Lowercases everything
    Example: U.Ser+test@Gmail.com → user@gmail.com
    """
    email = email.strip().lower()
    if '@' not in email:
        return email
    local, domain = email.split('@', 1)
    # Remove +tag
    local = local.split('+')[0]
    # Remove dots only for Gmail
    if domain in ('gmail.com', 'googlemail.com'):
        local = local.replace('.', '')
    return f"{local}@{domain}"


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

def sb_delete(table, filters):
    """DELETE from Supabase table."""
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.delete(url, headers=sb_headers())
    return resp.ok


# ─── SETTINGS ───────────────────────────────────────────────────────
def get_int_env(name, default):
    value = os.getenv(name)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

FREE_POINTS         = get_int_env("FREE_POINTS", 100)    # starting points for every user
PAID_POINTS         = get_int_env("PAID_POINTS", 200)    # bonus points per $1 purchase
SEARCH_COST         = 10                                  # points per full search
LOAD_MORE_COST      = 5                                   # points to load next 100 papers
MAX_ACCOUNTS_PER_IP = get_int_env("MAX_ACCOUNTS_PER_IP", 3)
OPENALEX_URL        = "https://api.openalex.org/works"

# How many results to request from each source per search
# Total max results = RESULTS_PER_SOURCE * 4 sources (before dedup)
RESULTS_PER_SOURCE = 100


# ─── IP RATE LIMITING ────────────────────────────────────────────────
# Stored in Supabase so it survives redeploys.
# Requires table: ip_signups (id int8 pk, ip text, created_at timestamp)

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

def total_points(user):
    """Total points a user has (free + purchased)."""
    return FREE_POINTS + user.get('paid_searches', 0)  # paid_searches now stores bonus points

def points_used(user):
    """Points spent so far (search_count now tracks points used)."""
    return user.get('search_count', 0)

def points_remaining(user):
    return max(0, total_points(user) - points_used(user))

def can_search(user):
    return points_remaining(user) >= SEARCH_COST

def can_load_more(user):
    return points_remaining(user) >= LOAD_MORE_COST


# ─── APA REFERENCE BUILDER (shared by all sources) ──────────────────
def _build_apa(authors, year, title, journal, volume, issue, pages, doi):
    """
    Build an APA reference string from parts.
    Used by all 4 source formatters so the format is consistent.
    ⚠️  Auto-generated — always verify before academic submission.
    """
    apa_authors = []
    for name in (authors or [])[:3]:
        parts = name.strip().split()
        if len(parts) >= 2:
            apa_authors.append(f"{parts[-1]}, {parts[0][0]}.")
        elif name.strip():
            apa_authors.append(name.strip())
    if len(authors or []) > 3:
        apa_authors.append("et al.")

    vol_issue = ""
    if volume and issue:
        vol_issue = f", {volume}({issue})"
    elif volume:
        vol_issue = f", {volume}"

    pages_part = f", {pages}" if pages else ""

    # Normalize DOI to full URL if it isn't already
    if doi and not doi.startswith("http"):
        doi_part = f" https://doi.org/{doi}"
    elif doi:
        doi_part = f" {doi}"
    else:
        doi_part = ""

    return (
        f"{', '.join(apa_authors) or 'Unknown'} "
        f"({year or 'n.d.'}). "
        f"{title}. "
        f"{journal}{vol_issue}{pages_part}.{doi_part}"
    )


def _build_mla(authors, year, title, journal, volume, issue, pages, doi):
    """
    Build MLA 9th edition reference.
    Format: Last, First, and First Last. "Title." Journal, vol. X, no. X, Year, pp. XX-XX.
    ⚠️  Auto-generated — always verify before academic submission.
    """
    mla_authors = []
    for i, name in enumerate((authors or [])[:3]):
        parts = name.strip().split()
        if len(parts) >= 2:
            if i == 0:
                mla_authors.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            else:
                mla_authors.append(name.strip())
        elif name.strip():
            mla_authors.append(name.strip())

    if not mla_authors:
        author_str = "Unknown"
    elif len(mla_authors) == 1:
        author_str = mla_authors[0]
        if len(authors or []) > 1:
            author_str += ", et al"
    else:
        author_str = ", and ".join(mla_authors)
        if len(authors or []) > 3:
            author_str += ", et al"

    ref = f'{author_str}. "{title}."'
    if journal:
        ref += f" {journal}"
    if volume:
        ref += f", vol. {volume}"
    if issue:
        ref += f", no. {issue}"
    if year:
        ref += f", {year}"
    if pages:
        ref += f", pp. {pages}"
    ref += "."
    if doi:
        d = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        ref += f" {d}."
    return ref


def _build_chicago(authors, year, title, journal, volume, issue, pages, doi):
    """
    Build Chicago 17th edition (author-date) reference.
    Format: Last, First. Year. "Title." Journal Volume (Issue): Pages.
    ⚠️  Auto-generated — always verify before academic submission.
    """
    chi_authors = []
    for i, name in enumerate((authors or [])[:3]):
        parts = name.strip().split()
        if len(parts) >= 2:
            if i == 0:
                chi_authors.append(f"{parts[-1]}, {' '.join(parts[:-1])}")
            else:
                chi_authors.append(name.strip())
        elif name.strip():
            chi_authors.append(name.strip())

    if not chi_authors:
        author_str = "Unknown"
    elif len(chi_authors) == 1:
        author_str = chi_authors[0]
        if len(authors or []) > 1:
            author_str += ", et al."
    else:
        author_str = ", ".join(chi_authors)
        if len(authors or []) > 3:
            author_str += ", et al."

    ref = f"{author_str}. {year or 'n.d.'}. \"{title}.\""
    if journal:
        ref += f" {journal}"
    if volume:
        ref += f" {volume}"
        if issue:
            ref += f" ({issue})"
    if pages:
        ref += f": {pages}"
    ref += "."
    if doi:
        d = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        ref += f" {d}."
    return ref


def _build_harvard(authors, year, title, journal, volume, issue, pages, doi):
    """
    Build Harvard reference.
    Format: Last, F. (Year) 'Title', Journal, Volume(Issue), pp. Pages.
    ⚠️  Auto-generated — always verify before academic submission.
    """
    harv_authors = []
    for name in (authors or [])[:3]:
        parts = name.strip().split()
        if len(parts) >= 2:
            initials = ". ".join(p[0].upper() for p in parts[:-1] if p) + "."
            harv_authors.append(f"{parts[-1]}, {initials}")
        elif name.strip():
            harv_authors.append(name.strip())
    if len(authors or []) > 3:
        harv_authors.append("et al.")

    author_str = ", ".join(harv_authors) if harv_authors else "Unknown"

    ref = f"{author_str} ({year or 'n.d.'}) '{title}'"
    if journal:
        ref += f", {journal}"
    if volume:
        ref += f", {volume}"
        if issue:
            ref += f"({issue})"
    if pages:
        ref += f", pp. {pages}"
    ref += "."
    if doi:
        d = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        ref += f" Available at: {d} (Accessed: {__import__('datetime').datetime.now().strftime('%d %B %Y')})."
    return ref


def _all_citations(authors, year, title, journal, volume, issue, pages, doi):
    """Return all 4 citation styles as a dict."""
    return {
        "apa":     _build_apa(authors, year, title, journal, volume, issue, pages, doi),
        "mla":     _build_mla(authors, year, title, journal, volume, issue, pages, doi),
        "chicago": _build_chicago(authors, year, title, journal, volume, issue, pages, doi),
        "harvard": _build_harvard(authors, year, title, journal, volume, issue, pages, doi),
    }


# ─── OPENALEX HELPERS ────────────────────────────────────────────────
def reconstruct_abstract(abstract_index):
    """OpenAlex stores abstracts as inverted index — rebuild the plain text."""
    if not abstract_index:
        return ""
    words = []
    for word, positions in abstract_index.items():
        for pos in positions:
            words.append((pos, word))
    words.sort(key=lambda x: x[0])
    return " ".join(w for _, w in words)

def format_paper(paper):
    """Format a single OpenAlex paper into Sturch's standard result shape."""
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

    abstract   = reconstruct_abstract(paper.get("abstract_inverted_index"))
    year       = paper.get("publication_year", "n.d.")
    title      = paper.get("title", "")
    journal    = source.get("display_name", "")
    doi        = paper.get("doi", "")
    volume     = biblio.get("volume", "")
    issue      = biblio.get("issue", "")
    first_page = biblio.get("first_page", "")
    last_page  = biblio.get("last_page", "")
    pages      = f"{first_page}\u2013{last_page}" if first_page and last_page else ""

    missing = []
    if not volume:  missing.append("volume")
    if not issue:   missing.append("issue")
    if not pages:   missing.append("page range")
    if not doi:     missing.append("DOI")

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
        "apa_reference": _build_apa(authors, year, title, journal, volume, issue, pages, doi),
        "apa_missing":   missing,
        "data_source":   "OpenAlex (openalex.org)",
        "ref_warning":   "Auto-generated — verify before academic use",
    }


# ─── SEMANTIC SCHOLAR ────────────────────────────────────────────────
# Free public API — no key required.
# Docs: https://api.semanticscholar.org/api-docs/
# 200M+ papers. Great coverage for CS, AI, and most STEM fields.

def search_semantic_scholar(query, page=1, per_page=RESULTS_PER_SOURCE):
    """
    Search Semantic Scholar and return results in Sturch's standard shape.
    No API key needed — free public endpoint.
    Rate limit: 100 requests/5min unauthenticated (plenty for our use).
    """
    offset = (page - 1) * per_page
    params = {
        "query":  query,
        "limit":  per_page,
        "offset": offset,
        # Request only the fields we actually use — keeps response small and fast
        "fields": (
            "title,authors,year,abstract,"
            "citationCount,externalIds,journal,"
            "isOpenAccess,openAccessPdf"
        ),
    }
    try:
        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params=params,
            timeout=15,
            headers={"User-Agent": "Sturch/2.0 (academic research tool)"},
        )
        if not resp.ok:
            return []

        papers = resp.json().get("data", [])
        results = []

        for p in papers:
            authors   = [a.get("name", "") for a in p.get("authors", [])]
            year      = p.get("year")
            title     = p.get("title", "")
            abstract  = p.get("abstract", "") or ""
            citations = p.get("citationCount", 0) or 0
            doi       = (p.get("externalIds") or {}).get("DOI", "")
            journal   = (p.get("journal") or {}).get("name", "") or "Semantic Scholar"
            is_oa     = p.get("isOpenAccess", False)
            oa_url    = (p.get("openAccessPdf") or {}).get("url", "")

            # Determine missing APA fields
            missing = []
            if not doi:   missing.append("DOI")
            missing += ["volume", "issue", "page range"]  # SS doesn't return these

            results.append({
                "title":         title,
                "authors":       authors,
                "year":          year,
                "journal":       journal,
                "abstract":      abstract,
                "citations":     citations,
                "is_oa":         is_oa,
                "oa_url":        oa_url,
                "doi":           f"https://doi.org/{doi}" if doi else "",
                "concepts":      [],
                "openalex_id":   p.get("paperId", ""),  # reusing field as unique ID
                "volume":        "",
                "issue":         "",
                "pages":         "",
                "apa_reference": _build_apa(authors, year, title, journal, "", "", "", doi),
                "apa_missing":   missing,
                "data_source":   "Semantic Scholar (semanticscholar.org)",
                "ref_warning":   "Auto-generated — verify before academic use",
            })

        return results

    except Exception:
        # Never crash the whole search if one source fails
        return []


# ─── ARXIV ───────────────────────────────────────────────────────────
# Completely free — no key, no signup, no rate limit beyond common sense.
# Docs: https://info.arxiv.org/help/api/index.html
# Best for: CS, Math, Physics, Quantitative Biology, Economics.
# All papers are open access by definition.

def search_arxiv(query, page=1, per_page=RESULTS_PER_SOURCE):
    """
    Search arXiv and return results in Sturch's standard shape.
    Returns Atom XML — parsed with xml.etree.ElementTree (built-in, no pip needed).
    """
    start  = (page - 1) * per_page
    params = {
        "search_query": f"all:{query}",
        "start":        start,
        "max_results":  per_page,
        "sortBy":       "relevance",
        "sortOrder":    "descending",
    }
    try:
        resp = requests.get(
            "https://export.arxiv.org/api/query",
            params=params,
            timeout=15,
        )
        if not resp.ok:
            return []

        # arXiv returns Atom XML — parse it
        root = ET.fromstring(resp.text)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        results = []

        for entry in entries:
            title    = (entry.findtext("atom:title",   "", ns) or "").strip().replace("\n", " ")
            abstract = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")
            authors  = [
                a.findtext("atom:name", "", ns)
                for a in entry.findall("atom:author", ns)
            ]
            published = entry.findtext("atom:published", "", ns) or ""
            year      = int(published[:4]) if published and published[:4].isdigit() else None

            # Find DOI and arXiv page link from <link> tags
            doi_link  = ""
            page_link = ""
            for link in entry.findall("atom:link", ns):
                rel   = link.get("rel", "")
                ltype = link.get("type", "")
                href  = link.get("href", "")
                if link.get("title") == "doi":
                    doi_link = href
                elif ltype == "text/html" or rel == "alternate":
                    page_link = href

            results.append({
                "title":         title,
                "authors":       authors,
                "year":          year,
                "journal":       "arXiv",
                "abstract":      abstract,
                "citations":     0,       # arXiv API doesn't return citation counts
                "is_oa":         True,    # arXiv is always open access
                "oa_url":        page_link,
                "doi":           doi_link,
                "concepts":      [],
                "openalex_id":   page_link,  # arXiv URL as unique ID
                "volume":        "",
                "issue":         "",
                "pages":         "",
                "apa_reference": _build_apa(authors, year, title, "arXiv", "", "", "", doi_link),
                "apa_missing":   ["volume", "issue", "page range"],
                "data_source":   "arXiv (arxiv.org)",
                "ref_warning":   "Auto-generated — verify before academic use",
            })

        return results

    except Exception:
        return []


# ─── PUBMED ──────────────────────────────────────────────────────────
# Free NCBI E-utilities API — no key required for basic use.
# Docs: https://www.ncbi.nlm.nih.gov/books/NBK25501/
# Best for: Medicine, Biology, Pharmacology, Public Health.
# Two-step: esearch (get IDs) → esummary (get metadata).
# Note: esummary does NOT return full abstracts — would need efetch for that.

def search_pubmed(query, page=1, per_page=RESULTS_PER_SOURCE):
    """
    Search PubMed via NCBI E-utilities and return results in Sturch's standard shape.
    No API key needed. Adding email to User-Agent is NCBI best practice.
    """
    retstart = (page - 1) * per_page

    # ── Step 1: Get PubMed IDs matching the query ─────────────────
    search_params = {
        "db":       "pubmed",
        "term":     query,
        "retmax":   per_page,
        "retstart": retstart,
        "retmode":  "json",
        "sort":     "relevance",
    }
    try:
        search_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=search_params,
            timeout=15,
            headers={"User-Agent": "Sturch/2.0 (academic research tool)"},
        )
        if not search_resp.ok:
            return []

        ids = search_resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

    except Exception:
        return []

    # ── Step 2: Fetch metadata for those IDs ──────────────────────
    try:
        summary_params = {
            "db":      "pubmed",
            "id":      ",".join(ids),
            "retmode": "json",
        }
        summary_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params=summary_params,
            timeout=15,
            headers={"User-Agent": "Sturch/2.0 (academic research tool)"},
        )
        if not summary_resp.ok:
            return []

        summary_data = summary_resp.json().get("result", {})
        results = []

        for pmid in ids:
            p = summary_data.get(pmid, {})
            if not p or not isinstance(p, dict):
                continue

            title   = p.get("title", "")
            authors = [a.get("name", "") for a in p.get("authors", [])]
            journal = p.get("fulljournalname", "") or p.get("source", "")
            volume  = p.get("volume", "")
            issue   = p.get("issue", "")
            pages   = p.get("pages", "")

            # Parse year from pubdate (e.g. "2023 Jan 15" or "2023")
            pubdate = p.get("pubdate", "")
            year    = int(pubdate[:4]) if pubdate and pubdate[:4].isdigit() else None

            # Find DOI in articleids list
            doi = ""
            for artid in p.get("articleids", []):
                if artid.get("idtype") == "doi":
                    doi = artid.get("value", "")
                    break

            pubmed_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

            # Track which APA fields are missing
            missing = [
                label for label, val in [
                    ("volume", volume),
                    ("issue", issue),
                    ("page range", pages),
                    ("DOI", doi),
                ]
                if not val
            ]

            results.append({
                "title":         title,
                "authors":       authors,
                "year":          year,
                "journal":       journal,
                "abstract":      "",      # esummary doesn't include abstracts
                "citations":     0,       # PubMed E-utilities don't return citation counts
                "is_oa":         False,
                "oa_url":        pubmed_url,
                "doi":           f"https://doi.org/{doi}" if doi else "",
                "concepts":      [],
                "openalex_id":   f"pmid:{pmid}",  # using as unique ID field
                "volume":        volume,
                "issue":         issue,
                "pages":         pages,
                "apa_reference": _build_apa(authors, year, title, journal, volume, issue, pages, doi),
                "apa_missing":   missing,
                "data_source":   "PubMed (pubmed.ncbi.nlm.nih.gov)",
                "ref_warning":   "Auto-generated — verify before academic use",
            })

        return results

    except Exception:
        return []


# ─── ROUTES ──────────────────────────────────────────────────────────

@app.route('/')
def home():
    if 'user_id' in session:
        return render_template('index.html')
    return render_template('landing.html')  # landing page for visitors


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if 'user_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        data     = request.get_json() or request.form
        email    = normalize_email(data.get('email') or '')
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
        email    = normalize_email(data.get('email') or '')
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
        "email":             user['email'],
        "points_used":       points_used(user),
        "points_remaining":  points_remaining(user),
        "total_points":      total_points(user),
        "is_paid":           user.get('is_paid', False),
        "can_search":        can_search(user),
        "can_load_more":     can_load_more(user),
        "search_cost":       SEARCH_COST,
        "load_more_cost":    LOAD_MORE_COST,
    })


@app.route('/api/search')
@login_required
def search():
    """
    Multi-source search endpoint.

    Queries OpenAlex, Semantic Scholar, arXiv, and PubMed simultaneously
    using ThreadPoolExecutor. Results are merged, deduplicated (by DOI then
    by title prefix), and sorted by citation count descending before returning.

    Query params:
      q          — search query (required)
      page       — page number (default: 1)
      year_from  — filter: minimum publication year (OpenAlex only)
      year_to    — filter: maximum publication year (OpenAlex only)
    """
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
            "message": f"Not enough points. A search costs {SEARCH_COST} points. You have {points_remaining(user)} left. Get 200 more for $1!",
        }), 403

    # ── Define the OpenAlex fetch (needs year filter params) ─────────
    def fetch_openalex():
        params = {
            "search":   query,
            "per-page": RESULTS_PER_SOURCE,
            "page":     page,
            "sort":     "cited_by_count:desc",
        }
        if year_from and year_to:
            params["filter"] = f"publication_year:{year_from}-{year_to}"
        elif year_from:
            params["filter"] = f"publication_year:{year_from}-"
        elif year_to:
            params["filter"] = f"publication_year:-{year_to}"

        try:
            resp   = requests.get(OPENALEX_URL, params=params, timeout=15)
            data   = resp.json()
            papers = data.get("results", [])
            count  = data.get("meta", {}).get("count", 0)
            return [format_paper(p) for p in papers], count
        except Exception:
            return [], 0

    # ── Run all 4 sources in parallel ────────────────────────────────
    # ThreadPoolExecutor fires all 4 requests at the same time.
    # Total wait time ≈ slowest single source (not sum of all 4).
    all_results = []
    total_count = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_openalex):                          "openalex",
            executor.submit(search_semantic_scholar, query, page):    "semantic_scholar",
            executor.submit(search_arxiv,            query, page):    "arxiv",
            executor.submit(search_pubmed,           query, page):    "pubmed",
        }

        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                if source == "openalex":
                    papers, count = result
                    total_count += count
                    all_results.extend(papers)
                else:
                    all_results.extend(result)
            except Exception:
                # If one source crashes, the rest still return normally
                pass

    # ── Deduplicate across sources ────────────────────────────────────
    # Priority: same DOI = definitely the same paper.
    # Fallback: first 60 chars of lowercased title = probably the same paper.
    seen_dois   = set()
    seen_titles = set()
    deduped     = []

    for paper in all_results:
        doi        = (paper.get("doi") or "").strip().lower()
        title_key  = (paper.get("title") or "").strip().lower()[:60]

        if doi and doi in seen_dois:
            continue
        if title_key and title_key in seen_titles:
            continue

        if doi:
            seen_dois.add(doi)
        if title_key:
            seen_titles.add(title_key)

        deduped.append(paper)

    # ── Sort merged results by citations (highest first) ─────────────
    deduped.sort(key=lambda x: x.get("citations", 0) or 0, reverse=True)

    # ── Deduct 10 points and log ─────────────────────────────────────
    new_points_used = points_used(user) + SEARCH_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_points_used})

    sb_post("search_logs", {
        "user_id":     user['id'],
        "query":       query,
        "results":     len(deduped),
        "points_used": SEARCH_COST,
        "searched_at": datetime.now().isoformat(),
    })

    return jsonify({
        "results":           deduped,
        "total":             total_count,
        "query":             query,
        "points_remaining":  max(0, total_points(user) - new_points_used),
        "points_used":       SEARCH_COST,
        "sources_used":      ["OpenAlex", "Semantic Scholar", "arXiv", "PubMed"],
        "data_source":       "OpenAlex, Semantic Scholar, arXiv, PubMed",
        "ref_disclaimer":    "References auto-generated — verify before academic use",
    })


@app.route('/api/load-more')
@login_required
def load_more():
    """
    Load the next 100 papers for the same query — costs only 5 points.

    Query params:
      q          — same search query (required)
      page       — page number to fetch (required, must be > 1)
      year_from  — same filters as /api/search
      year_to    — same filters as /api/search
    """
    query     = request.args.get('q', '').strip()
    page      = int(request.args.get('page', 2))
    year_from = request.args.get('year_from', '')
    year_to   = request.args.get('year_to', '')

    if not query:
        return jsonify({"error": "Please enter a search query"}), 400

    if page < 2:
        return jsonify({"error": "Page must be 2 or higher for load-more"}), 400

    user = get_user(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    if not can_load_more(user):
        return jsonify({
            "error":   "limit_reached",
            "message": f"Not enough points. Loading more costs {LOAD_MORE_COST} points. You have {points_remaining(user)} left. Get 200 more for $1!",
        }), 403

    # ── Same parallel fetch as /api/search but on a different page ────
    def fetch_openalex():
        params = {
            "search":   query,
            "per-page": RESULTS_PER_SOURCE,
            "page":     page,
            "sort":     "cited_by_count:desc",
        }
        if year_from and year_to:
            params["filter"] = f"publication_year:{year_from}-{year_to}"
        elif year_from:
            params["filter"] = f"publication_year:{year_from}-"
        elif year_to:
            params["filter"] = f"publication_year:-{year_to}"
        try:
            resp   = requests.get(OPENALEX_URL, params=params, timeout=15)
            data   = resp.json()
            papers = data.get("results", [])
            count  = data.get("meta", {}).get("count", 0)
            return [format_paper(p) for p in papers], count
        except Exception:
            return [], 0

    all_results = []
    total_count = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_openalex):                          "openalex",
            executor.submit(search_semantic_scholar, query, page):    "semantic_scholar",
            executor.submit(search_arxiv,            query, page):    "arxiv",
            executor.submit(search_pubmed,           query, page):    "pubmed",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                if source == "openalex":
                    papers, count = result
                    total_count += count
                    all_results.extend(papers)
                else:
                    all_results.extend(result)
            except Exception:
                pass

    # ── Deduplicate ───────────────────────────────────────────────────
    seen_dois   = set()
    seen_titles = set()
    deduped     = []
    for paper in all_results:
        doi       = (paper.get("doi") or "").strip().lower()
        title_key = (paper.get("title") or "").strip().lower()[:60]
        if doi and doi in seen_dois:
            continue
        if title_key and title_key in seen_titles:
            continue
        if doi:       seen_dois.add(doi)
        if title_key: seen_titles.add(title_key)
        deduped.append(paper)

    deduped.sort(key=lambda x: x.get("citations", 0) or 0, reverse=True)

    # ── Deduct 5 points ───────────────────────────────────────────────
    new_points_used = points_used(user) + LOAD_MORE_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_points_used})

    sb_post("search_logs", {
        "user_id":     user['id'],
        "query":       query,
        "results":     len(deduped),
        "points_used": LOAD_MORE_COST,
        "searched_at": datetime.now().isoformat(),
    })

    return jsonify({
        "results":          deduped,
        "total":            total_count,
        "query":            query,
        "page":             page,
        "points_remaining": max(0, total_points(user) - new_points_used),
        "points_used":      LOAD_MORE_COST,
        "sources_used":     ["OpenAlex", "Semantic Scholar", "arXiv", "PubMed"],
        "data_source":      "OpenAlex, Semantic Scholar, arXiv, PubMed",
        "ref_disclaimer":   "References auto-generated — verify before academic use",
    })


@app.route('/upgrade')
@login_required
def upgrade():
    user = get_user(session['user_id'])
    return render_template('upgrade.html', user=user)


@app.route('/health')
def health():
    return jsonify({
        "status":  "ok",
        "app":     "Sturch",
        "version": "2.0-beta",
        "sources": ["OpenAlex", "Semantic Scholar", "arXiv", "PubMed"],
    })


# ─── PAPER READER PAGE ───────────────────────────────────────────────
@app.route('/paper')
@login_required
def paper():
    """Serve the dedicated paper reading page."""
    return render_template('paper.html')


# ─── FULL PAPER FETCH ────────────────────────────────────────────────
# Tries to get the full text of a paper from its source.
# Strategy per source:
#   arXiv   → convert abstract URL to HTML page (arxiv.org/html/XXXX)
#   PubMed  → PMC E-utilities full XML text
#   Others  → try oa_url, scrape readable text, fall back to abstract

def _clean_html(html):
    """Strip HTML tags and scripts, return clean readable plain text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "figure", "aside"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _fetch_arxiv_full(oa_url):
    """
    arXiv has HTML versions at arxiv.org/html/<id>
    Convert abstract URL → HTML URL and scrape it.
    Fallback: ar5iv.org mirror.
    """
    try:
        html_url = oa_url.replace("arxiv.org/abs/", "arxiv.org/html/")
        resp = requests.get(html_url, timeout=20,
                            headers={"User-Agent": "Sturch/2.0"})
        if resp.ok:
            return _clean_html(resp.text)
        # Fallback to ar5iv mirror
        ar5iv_url = oa_url.replace("arxiv.org/abs/", "ar5iv.org/html/")
        resp2 = requests.get(ar5iv_url, timeout=20,
                             headers={"User-Agent": "Sturch/2.0"})
        if resp2.ok:
            return _clean_html(resp2.text)
    except Exception:
        pass
    return None


def _fetch_pubmed_full(openalex_id):
    """
    Use NCBI PMC E-utilities to get full article text.
    openalex_id is stored as 'pmid:XXXXX' from our PubMed formatter.
    Two steps: find PMC ID from PMID → fetch full XML.
    """
    try:
        pmid = openalex_id.replace("pmid:", "").strip()
        if not pmid.isdigit():
            return None
        # Step 1: find PMC ID linked to this PMID
        link_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
            params={"dbfrom": "pubmed", "db": "pmc", "id": pmid, "retmode": "json"},
            timeout=15, headers={"User-Agent": "Sturch/2.0"}
        )
        if not link_resp.ok:
            return None
        pmc_ids = []
        for ls in link_resp.json().get("linksets", []):
            for lname in ls.get("linksetdbs", []):
                if lname.get("linkname") == "pubmed_pmc":
                    pmc_ids = lname.get("links", [])
        if not pmc_ids:
            return None
        # Step 2: fetch full XML from PMC
        fetch_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pmc", "id": pmc_ids[0], "rettype": "xml", "retmode": "xml"},
            timeout=20, headers={"User-Agent": "Sturch/2.0"}
        )
        if not fetch_resp.ok:
            return None
        soup  = BeautifulSoup(fetch_resp.text, "xml")
        body  = soup.find("body")
        paras = (body or soup).find_all("p")
        text  = "\n\n".join(p.get_text(strip=True) for p in paras if p.get_text(strip=True))
        return text if text else None
    except Exception:
        return None


def _fetch_oa_url(oa_url):
    """
    Generic fallback: fetch open access URL and extract readable text.
    Skips PDFs — can't parse them without heavy libraries.
    """
    try:
        resp = requests.get(oa_url, timeout=20,
                            headers={"User-Agent": "Sturch/2.0"},
                            allow_redirects=True)
        if not resp.ok:
            return None
        if "pdf" in resp.headers.get("Content-Type", "").lower():
            return None  # skip PDFs
        return _clean_html(resp.text)
    except Exception:
        return None


@app.route('/api/fetch-paper')
@login_required
def fetch_paper():
    """
    Fetch the full text of a paper given its metadata.

    Query params:
      source      — data_source string (e.g. "arXiv (arxiv.org)")
      oa_url      — open access URL
      openalex_id — paper ID (used for PubMed PMID lookup)
      abstract    — fallback text if full fetch fails

    Returns JSON:
      { "text": "...", "source": "arxiv_html|pubmed_pmc|oa_url|abstract", "full": true/false, "notice": "..." }
    """
    source      = request.args.get("source",      "").lower()
    oa_url      = request.args.get("oa_url",      "").strip()
    openalex_id = request.args.get("openalex_id", "").strip()
    abstract    = request.args.get("abstract",    "").strip()

    full_text    = None
    fetch_source = "abstract"

    # ── arXiv: reliable HTML versions ────────────────────────────────
    if "arxiv" in source and oa_url:
        full_text = _fetch_arxiv_full(oa_url)
        if full_text:
            fetch_source = "arxiv_html"

    # ── PubMed: PMC full text ─────────────────────────────────────────
    elif "pubmed" in source and openalex_id.startswith("pmid:"):
        full_text = _fetch_pubmed_full(openalex_id)
        if full_text:
            fetch_source = "pubmed_pmc"
        if not full_text and oa_url:
            full_text = _fetch_oa_url(oa_url)
            if full_text:
                fetch_source = "oa_url"

    # ── OpenAlex / Semantic Scholar: try oa_url ───────────────────────
    elif oa_url:
        full_text = _fetch_oa_url(oa_url)
        if full_text:
            fetch_source = "oa_url"

    # ── Final fallback: abstract ──────────────────────────────────────
    if not full_text:
        if abstract:
            return jsonify({
                "text":   abstract,
                "source": "abstract",
                "full":   False,
                "notice": "Full text unavailable — showing abstract only."
            })
        return jsonify({
            "text":   "",
            "source": "none",
            "full":   False,
            "notice": "No text available for this paper."
        }), 404

    return jsonify({
        "text":         full_text,
        "source":       fetch_source,
        "full":         True,
        "notice":       "",
        "image_notice": (
            "📷 Images and figures in this paper are not displayed here. "
            "To view them, visit the original page using the link above."
        ) if full_text else "",
    })


# ─── SEARCH HISTORY ─────────────────────────────────────────────────
@app.route('/api/history')
@login_required
def get_history():
    """Return the last 50 searches for the logged-in user."""
    user_id = session['user_id']
    result  = sb_get("search_logs", f"user_id=eq.{user_id}&order=searched_at.desc&limit=50")
    return jsonify(result or [])


# ─── BOOKMARKS ───────────────────────────────────────────────────────
@app.route('/api/bookmarks', methods=['GET'])
@login_required
def get_bookmarks():
    user_id = session['user_id']
    result  = sb_get("bookmarks", f"user_id=eq.{user_id}&order=created_at.desc")
    return jsonify(result or [])

@app.route('/api/bookmarks', methods=['POST'])
@login_required
def add_bookmark():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No paper data"}), 400
    user_id  = session['user_id']
    paper_id = (data.get('openalex_id') or data.get('doi') or (data.get('title','')[:60])).strip()
    # Prevent duplicates
    existing = sb_get("bookmarks", f"user_id=eq.{user_id}&paper_id=eq.{paper_id}")
    if existing:
        return jsonify({"error": "already_bookmarked"}), 409
    result = sb_post("bookmarks", {
        "user_id":    user_id,
        "paper_id":   paper_id,
        "paper_data": data,
        "created_at": datetime.now().isoformat(),
    })
    item = (result[0] if isinstance(result, list) else result) or {}
    return jsonify({"success": True, "id": item.get("id", "")})

@app.route('/api/bookmarks/<bookmark_id>', methods=['DELETE'])
@login_required
def delete_bookmark(bookmark_id):
    user_id = session['user_id']
    ok = sb_delete("bookmarks", f"id=eq.{bookmark_id}&user_id=eq.{user_id}")
    return jsonify({"success": ok})


# ─── COLLECTIONS ─────────────────────────────────────────────────────
@app.route('/api/collections', methods=['GET'])
@login_required
def get_collections():
    user_id = session['user_id']
    result  = sb_get("collections", f"user_id=eq.{user_id}&order=created_at.desc")
    return jsonify(result or [])

@app.route('/api/collections', methods=['POST'])
@login_required
def create_collection():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "Collection name required"}), 400
    user_id = session['user_id']
    result  = sb_post("collections", {
        "user_id":    user_id,
        "name":       name,
        "created_at": datetime.now().isoformat(),
    })
    col = (result[0] if isinstance(result, list) else result) or {}
    return jsonify(col)

@app.route('/api/collections/<col_id>', methods=['DELETE'])
@login_required
def delete_collection(col_id):
    user_id = session['user_id']
    ok = sb_delete("collections", f"id=eq.{col_id}&user_id=eq.{user_id}")
    return jsonify({"success": ok})

@app.route('/api/collections/<col_id>/papers', methods=['GET'])
@login_required
def get_collection_papers(col_id):
    user_id = session['user_id']
    result  = sb_get("collection_papers", f"collection_id=eq.{col_id}&user_id=eq.{user_id}&order=created_at.desc")
    return jsonify(result or [])

@app.route('/api/collections/<col_id>/papers', methods=['POST'])
@login_required
def add_to_collection(col_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No paper data"}), 400
    user_id  = session['user_id']
    paper_id = (data.get('openalex_id') or data.get('doi') or (data.get('title','')[:60])).strip()
    result   = sb_post("collection_papers", {
        "collection_id": col_id,
        "user_id":       user_id,
        "paper_id":      paper_id,
        "paper_data":    data,
        "created_at":    datetime.now().isoformat(),
    })
    item = (result[0] if isinstance(result, list) else result) or {}
    return jsonify({"success": True, "id": item.get("id", "")})

@app.route('/api/collections/<col_id>/papers/<entry_id>', methods=['DELETE'])
@login_required
def remove_from_collection(col_id, entry_id):
    user_id = session['user_id']
    ok = sb_delete("collection_papers", f"id=eq.{entry_id}&collection_id=eq.{col_id}&user_id=eq.{user_id}")
    return jsonify({"success": ok})


# ─── RELATED PAPERS ──────────────────────────────────────────────────
# Costs 5 points — same as load-more
# Searches using paper concepts or first words of title

def _openalex_related(query):
    params = {"search": query, "per-page": 25, "page": 1, "sort": "cited_by_count:desc"}
    try:
        resp   = requests.get(OPENALEX_URL, params=params, timeout=15)
        data   = resp.json()
        return [format_paper(p) for p in data.get("results", [])], data.get("meta",{}).get("count",0)
    except Exception:
        return [], 0

@app.route('/api/related')
@login_required
def related_papers():
    """
    Find related papers based on a paper's concepts or title.
    Costs LOAD_MORE_COST (5) points.

    Query params:
      title    — paper title (used if no concepts)
      concepts — comma-separated concept tags (preferred)
    """
    title    = request.args.get('title', '').strip()
    concepts = request.args.get('concepts', '').strip()

    if not title and not concepts:
        return jsonify({"error": "title or concepts required"}), 400

    user = get_user(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    if not can_load_more(user):
        return jsonify({
            "error":   "limit_reached",
            "message": f"Not enough points. Related papers cost {LOAD_MORE_COST} points. You have {points_remaining(user)} left.",
        }), 403

    # Build search query
    if concepts:
        query = ' '.join(concepts.split(',')[:3]).strip()
    else:
        query = ' '.join(title.split()[:5])

    all_results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_openalex_related, query):               "openalex",
            executor.submit(search_semantic_scholar, query, 1):      "semantic_scholar",
            executor.submit(search_arxiv, query, 1):                 "arxiv",
            executor.submit(search_pubmed, query, 1):                "pubmed",
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                result = future.result()
                if src == "openalex":
                    papers, _ = result
                    all_results.extend(papers)
                else:
                    all_results.extend(result)
            except Exception:
                pass

    # Deduplicate and exclude the source paper
    seen_dois   = set()
    seen_titles = set()
    deduped     = []
    orig_title  = title.lower()[:60]

    for paper in all_results:
        doi       = (paper.get("doi") or "").strip().lower()
        title_key = (paper.get("title") or "").strip().lower()[:60]
        if title_key == orig_title:
            continue   # exclude the original paper
        if doi and doi in seen_dois:
            continue
        if title_key and title_key in seen_titles:
            continue
        if doi:       seen_dois.add(doi)
        if title_key: seen_titles.add(title_key)
        deduped.append(paper)

    deduped.sort(key=lambda x: x.get("citations", 0) or 0, reverse=True)

    # Deduct 5 points
    new_pts = points_used(user) + LOAD_MORE_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_pts})

    return jsonify({
        "results":          deduped[:20],
        "query":            query,
        "points_remaining": max(0, total_points(user) - new_pts),
        "points_used":      LOAD_MORE_COST,
    })


# ─── EMAIL HELPER ────────────────────────────────────────────────────

def send_email(to_email, subject, html_body):
    """Send email via Resend API (HTTPS — works on Railway)."""
    if not RESEND_API_KEY:
        return False
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    "Sturch <onboarding@resend.dev>",
                "to":      [to_email],
                "subject": subject,
                "html":    html_body,
            },
            timeout=15
        )
        if not resp.ok:
            print(f"Resend error: {resp.text}")
        return resp.ok
    except Exception as e:
        print(f"Email error: {e}")
        return False


def generate_reset_code():
    """Generate a 6-digit numeric reset code."""
    return ''.join(random.choices(string.digits, k=6))


# ─── PASSWORD RESET ROUTES ────────────────────────────────────────────

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'GET':
        return render_template('forgot.html')

    data  = request.get_json() or request.form
    email = normalize_email(data.get('email') or '')

    if not email or '@' not in email:
        return jsonify({"error": "Enter a valid email address"}), 400

    user = get_user_by_email(email)
    # Always return success even if email not found — prevents user enumeration
    if not user:
        return jsonify({"success": True, "message": "If that email exists, a code has been sent."})

    code = generate_reset_code()

    # Save code to Supabase (expires in 15 minutes)
    sb_post("password_resets", {
        "email":      email,
        "code":       code,
        "used":       False,
        "created_at": datetime.now().isoformat(),
    })

    # Send email
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;background:#0b0d12;color:#d8e0f8;padding:32px;border-radius:12px">
      <h2 style="font-family:Georgia,serif;color:#4f8de8;margin-bottom:8px">Sturch Password Reset</h2>
      <p style="color:#8a94b8;margin-bottom:24px">Your one-time reset code:</p>
      <div style="background:#181d2e;border:2px solid #4f8de8;border-radius:10px;padding:20px;text-align:center;margin-bottom:24px">
        <span style="font-size:2.5rem;font-weight:700;letter-spacing:12px;color:#4f8de8;font-family:monospace">{code}</span>
      </div>
      <p style="color:#8a94b8;font-size:.85rem">This code expires in <strong>15 minutes</strong>. Do not share it with anyone.</p>
      <p style="color:#8a94b8;font-size:.85rem">If you did not request this, ignore this email.</p>
      <hr style="border:none;border-top:1px solid #1f2540;margin:20px 0"/>
      <p style="color:#5a6385;font-size:.75rem">Sturch — Built for Filipino STE students 🇵🇭</p>
    </div>
    """

    sent = send_email(email, "Sturch — Your password reset code", html)
    if not sent:
        return jsonify({"error": "Could not send email. Try again later."}), 500

    return jsonify({"success": True, "message": "Reset code sent to your email."})


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'GET':
        email = request.args.get('email', '')
        return render_template('reset.html', email=email)

    data     = request.get_json() or request.form
    email    = normalize_email(data.get('email') or '')
    code     = (data.get('code') or '').strip()
    password = (data.get('password') or '').strip()

    if not email or not code or not password:
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    # Find valid unused code within 15 minutes
    cutoff = (datetime.now() - __import__('datetime').timedelta(minutes=15)).isoformat()
    results = sb_get("password_resets",
        f"email=eq.{email}&code=eq.{code}&used=eq.false&created_at=gte.{cutoff}&order=created_at.desc&limit=1"
    )

    if not results:
        return jsonify({"error": "Invalid or expired code. Please request a new one."}), 400

    reset_id = results[0]['id']

    # Mark code as used
    sb_patch("password_resets", f"id=eq.{reset_id}", {"used": True})

    # Update password
    hashed = hash_password(password)
    sb_patch("users", f"email=eq.{email}", {"password_hash": hashed})

    return jsonify({"success": True, "redirect": "/login"})


# ─── PAYMONGO PAYMENT ─────────────────────────────────────────────────

def pm_headers():
    """PayMongo API headers with base64-encoded secret key."""
    encoded = base64.b64encode(f"{PAYMONGO_SECRET_KEY}:".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


@app.route('/api/create-payment', methods=['POST'])
@login_required
def create_payment():
    """
    Create a PayMongo payment link for 200 points ($1 / ₱55 approx).
    Returns the checkout URL to redirect the user to.
    """
    if not PAYMONGO_SECRET_KEY:
        return jsonify({"error": "Payment not configured yet"}), 503

    user = get_user(session['user_id'])
    if not user:
        return jsonify({"error": "User not found"}), 404

    try:
        payload = {
            "data": {
                "attributes": {
                    "amount":      5500,          # ₱55 in centavos (~$1)
                    "currency":    "PHP",
                    "description": "Sturch — 200 research points",
                    "remarks":     f"user_id:{user['id']}",
                    "redirect": {
                        "success": f"{APP_URL}/payment/success",
                        "failed":  f"{APP_URL}/payment/failed",
                    }
                }
            }
        }
        resp = requests.post(
            "https://api.paymongo.com/v1/links",
            headers=pm_headers(),
            json=payload,
            timeout=15
        )
        if not resp.ok:
            print(f"PayMongo error: {resp.text}")
            return jsonify({"error": "Could not create payment. Try again."}), 500

        link_data    = resp.json()["data"]
        checkout_url = link_data["attributes"]["checkout_url"]
        link_id      = link_data["id"]

        # Log payment attempt
        sb_post("payments", {
            "user_id":     user['id'],
            "amount":      1.00,
            "points_added": 200,
            "status":      "pending",
            "reference":   link_id,
            "created_at":  datetime.now().isoformat(),
        })

        return jsonify({"checkout_url": checkout_url, "link_id": link_id})

    except Exception as e:
        print(f"PayMongo exception: {e}")
        return jsonify({"error": "Payment error. Try again."}), 500


@app.route('/payment/success')
@login_required
def payment_success():
    """
    User lands here after successful PayMongo payment.
    We verify by checking the payment link status.
    """
    link_id = request.args.get('link_id', '')
    user    = get_user(session['user_id'])
    if not user:
        return redirect(url_for('login'))

    # Verify payment with PayMongo
    verified = False
    if link_id and PAYMONGO_SECRET_KEY:
        try:
            resp = requests.get(
                f"https://api.paymongo.com/v1/links/{link_id}",
                headers=pm_headers(),
                timeout=15
            )
            if resp.ok:
                status = resp.json()["data"]["attributes"]["status"]
                if status == "paid":
                    verified = True
        except Exception:
            pass

    if verified:
        # Add 200 points
        new_paid = user.get('paid_searches', 0) + 200
        sb_patch("users", f"id=eq.{user['id']}", {
            "paid_searches": new_paid,
            "is_paid": True
        })
        # Update payment record
        sb_patch("payments", f"reference=eq.{link_id}", {"status": "paid"})

    return render_template('payment_result.html',
        success=verified,
        points=200 if verified else 0,
        user=user
    )


@app.route('/payment/failed')
@login_required
def payment_failed():
    return render_template('payment_result.html', success=False, points=0, user=get_user(session['user_id']))


@app.route('/api/payment-webhook', methods=['POST'])
def payment_webhook():
    """
    PayMongo webhook — called automatically when payment completes.
    Set this URL in PayMongo dashboard: APP_URL/api/payment-webhook
    More reliable than relying on redirect alone.
    """
    try:
        data       = request.get_json()
        event_type = data.get("data", {}).get("attributes", {}).get("type", "")

        if event_type == "link.payment.paid":
            attrs   = data["data"]["attributes"]["data"]["attributes"]
            remarks = attrs.get("remarks", "")
            link_id = data["data"]["attributes"]["data"]["id"]

            # Extract user_id from remarks
            if remarks.startswith("user_id:"):
                user_id = remarks.replace("user_id:", "").strip()
                user    = get_user(user_id)
                if user:
                    new_paid = user.get('paid_searches', 0) + 200
                    sb_patch("users", f"id=eq.{user_id}", {
                        "paid_searches": new_paid,
                        "is_paid": True
                    })
                    sb_patch("payments", f"reference=eq.{link_id}", {"status": "paid"})

        return jsonify({"received": True}), 200
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 400


# ─── CITATIONS ENDPOINT ──────────────────────────────────────────────
@app.route('/api/citations', methods=['POST'])
@login_required
def get_citations():
    """
    Generate all citation styles for a paper.
    Accepts paper data as JSON body.
    Returns APA, MLA, Chicago, Harvard.
    """
    data    = request.get_json() or {}
    authors = data.get('authors') or []
    year    = data.get('year') or 'n.d.'
    title   = data.get('title') or ''
    journal = data.get('journal') or ''
    volume  = data.get('volume') or ''
    issue   = data.get('issue') or ''
    pages   = data.get('pages') or ''
    doi     = data.get('doi') or ''

    if not title:
        return jsonify({"error": "Title is required"}), 400

    return jsonify(_all_citations(authors, year, title, journal, volume, issue, pages, doi))


# ─── SHARE ENDPOINT ──────────────────────────────────────────────────
@app.route('/api/share', methods=['POST'])
@login_required
def share_paper():
    """
    Generate a shareable link for a paper.
    Priority: DOI > OA URL > search link by title.
    """
    data  = request.get_json() or {}
    doi   = (data.get('doi') or '').strip()
    oa    = (data.get('oa_url') or '').strip()
    title = (data.get('title') or '').strip()

    if doi:
        link = doi if doi.startswith('http') else f"https://doi.org/{doi}"
        source = "DOI"
    elif oa:
        link   = oa
        source = "Open Access"
    elif title:
        link   = f"https://scholar.google.com/scholar?q={requests.utils.quote(title)}"
        source = "Google Scholar search"
    else:
        return jsonify({"error": "No shareable link available"}), 404

    return jsonify({"url": link, "source": source})


# ─── ANSWER FINDER ───────────────────────────────────────────────────
import re as _re

ANSWER_TEMPLATES = [
    (r"^what is the difference between (.+) and (.+)$", "difference"),
    (r"^what is the effect of (.+)$",                   "effect"),
    (r"^what causes (.+)$",                             "causes"),
    (r"^what are (.+)$",                                "what_are"),
    (r"^what is (.+)$",                                 "what_is"),
    (r"^how does (.+) work$",                           "how_does"),
    (r"^how does (.+)$",                                "how_does"),
    (r"^why is (.+)$",                                  "why_is"),
    (r"^define (.+)$",                                  "define"),
]

def _extract_keyword(query):
    q = query.strip().lower()
    for pattern, ttype in ANSWER_TEMPLATES:
        m = _re.match(pattern, q)
        if m:
            return m.group(1).strip(), ttype
    return None, None

def _find_answer_sentences(abstract, keyword, ttype):
    if not abstract:
        return []
    kw       = keyword.lower()
    sentences = _re.split(r'(?<=[.!?])\s+', abstract)
    results  = []
    patterns = [
        kw + " is ", kw + " are ", kw + " refers to",
        kw + " can be", kw + " was ", kw + " involves",
        kw + " is defined", "defined as", kw + " causes",
        kw + " affects", kw + " plays", kw + " helps",
    ]
    for sent in sentences:
        sl = sent.lower()
        if any(p in sl for p in patterns) and len(sent.split()) >= 8:
            results.append(sent.strip())
        if len(results) >= 3:
            break
    return results


@app.route('/api/answer')
@login_required
def answer_finder():
    """
    Answer Finder — free, no points deducted.
    Supported templates:
      What is [topic]
      What are [topic]
      What causes [topic]
      How does [topic] work
      Why is [topic]
      Define [topic]
    """
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Query required"}), 400

    keyword, ttype = _extract_keyword(query)
    if not keyword:
        return jsonify({
            "error":   "unsupported_template",
            "message": "Try: What is [topic], What are [topic], How does [topic] work, What causes [topic], Define [topic]"
        }), 400

    params = {
        "search":   keyword,
        "per-page": 15,
        "page":     1,
        "sort":     "cited_by_count:desc",
    }
    try:
        resp   = requests.get(OPENALEX_URL, params=params, timeout=15)
        papers = resp.json().get("results", [])
    except Exception:
        return jsonify({"error": "Could not fetch results"}), 500

    answers = []
    sources = []

    for paper in papers:
        abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))
        if not abstract:
            continue
        sentences = _find_answer_sentences(abstract, keyword, ttype)
        if sentences:
            answers.extend(sentences)
            loc    = (paper.get("primary_location") or {})
            src    = (loc.get("source") or {})
            sources.append({
                "title":     paper.get("title", ""),
                "journal":   src.get("display_name", ""),
                "year":      paper.get("publication_year"),
                "doi":       paper.get("doi", ""),
                "oa_url":    (paper.get("open_access") or {}).get("oa_url", ""),
                "citations": paper.get("cited_by_count", 0),
            })
        if len(answers) >= 5:
            break

    # Deduplicate
    seen  = set()
    dedup = []
    for a in answers:
        key = a.lower()[:60]
        if key not in seen:
            seen.add(key)
            dedup.append(a)

    return jsonify({
        "keyword":  keyword,
        "template": ttype,
        "query":    query,
        "answers":  dedup[:5],
        "sources":  sources[:3],
        "message":  "" if dedup else "No direct answer found. Try searching for papers instead.",
    })


@app.route('/answer')
@login_required
def answer_page():
    return render_template('answer.html')


# ─── RUN ─────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
