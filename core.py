"""
core.py — Sturch shared utilities
All blueprints import from here.
"""
import os
import hashlib
import secrets
import requests
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import session, redirect, url_for, request
from dotenv import load_dotenv

load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────────────────
RESEND_API_KEY      = os.getenv("RESEND_API_KEY", "")
GMAIL_USER          = os.getenv("GMAIL_USER", "pacunlarjmark@gmail.com")
APP_URL             = os.getenv("APP_URL", "http://localhost:8080")
PAYMONGO_SECRET_KEY = os.getenv("PAYMONGO_SECRET_KEY", "")
PAYMONGO_PUBLIC_KEY = os.getenv("PAYMONGO_PUBLIC_KEY", "")
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_SECRET_KEY")
OPENALEX_URL        = "https://api.openalex.org/works"

def get_int_env(name, default):
    try:
        return int(os.getenv(name))
    except (TypeError, ValueError):
        return default

FREE_POINTS         = get_int_env("FREE_POINTS", 100)
PAID_POINTS         = get_int_env("PAID_POINTS", 200)
SEARCH_COST         = 10
LOAD_MORE_COST      = 5
MAX_ACCOUNTS_PER_IP = get_int_env("MAX_ACCOUNTS_PER_IP", 3)
RESULTS_PER_SOURCE  = 100

# ─── SUPABASE ────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }

def sb_get(table, filters=""):
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.get(url, headers=sb_headers())
    return resp.json() if resp.ok else []

def sb_post(table, data):
    url  = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=sb_headers(), json=data)
    return resp.json() if resp.ok else None

def sb_patch(table, filters, data):
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.patch(url, headers=sb_headers(), json=data)
    return resp.ok

def sb_delete(table, filters):
    url  = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    resp = requests.delete(url, headers=sb_headers())
    return resp.ok

# ─── AUTH HELPERS ────────────────────────────────────────────────────
def normalize_email(email):
    email = email.strip().lower()
    if '@' not in email:
        return email
    local, domain = email.split('@', 1)
    local = local.split('+')[0]
    if domain in ('gmail.com', 'googlemail.com'):
        local = local.replace('.', '')
    return f"{local}@{domain}"

def hash_password(password):
    salt = secrets.token_hex(16)
    h    = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{h}"

def check_password(password, hashed):
    try:
        salt, h = hashed.split(":")
        return hashlib.sha256((password + salt).encode()).hexdigest() == h
    except Exception:
        return False

def get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

def is_ip_allowed_to_signup(ip):
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    result = sb_get("ip_signups", f"ip=eq.{ip}&created_at=gte.{today_start}")
    return len(result) < MAX_ACCOUNTS_PER_IP

def record_ip_signup(ip):
    sb_post("ip_signups", {"ip": ip, "created_at": datetime.now().isoformat()})

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

# ─── USER HELPERS ────────────────────────────────────────────────────
def get_user(user_id):
    result = sb_get("users", f"id=eq.{user_id}&select=*")
    return result[0] if result else None

def get_user_by_email(email):
    result = sb_get("users", f"email=eq.{email}&select=*")
    return result[0] if result else None

def total_points(user):
    return FREE_POINTS + user.get('paid_searches', 0)

def points_used(user):
    return user.get('search_count', 0)

def points_remaining(user):
    return max(0, total_points(user) - points_used(user))

def can_search(user):
    return points_remaining(user) >= SEARCH_COST

def can_load_more(user):
    return points_remaining(user) >= LOAD_MORE_COST

# ─── CITATION BUILDERS ───────────────────────────────────────────────
def _build_apa(authors, year, title, journal, volume, issue, pages, doi):
    apa_authors = []
    for name in (authors or [])[:3]:
        parts = name.strip().split()
        if len(parts) >= 2:
            apa_authors.append(f"{parts[-1]}, {parts[0][0]}.")
        elif name.strip():
            apa_authors.append(name.strip())
    if len(authors or []) > 3:
        apa_authors.append("et al.")
    vol_issue  = f", {volume}({issue})" if volume and issue else (f", {volume}" if volume else "")
    pages_part = f", {pages}" if pages else ""
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
    mla_authors = []
    for i, name in enumerate((authors or [])[:3]):
        parts = name.strip().split()
        if len(parts) >= 2:
            mla_authors.append(f"{parts[-1]}, {' '.join(parts[:-1])}" if i == 0 else name.strip())
        elif name.strip():
            mla_authors.append(name.strip())
    if not mla_authors:
        author_str = "Unknown"
    elif len(mla_authors) == 1:
        author_str = mla_authors[0] + (", et al" if len(authors or []) > 1 else "")
    else:
        author_str = ", and ".join(mla_authors) + (", et al" if len(authors or []) > 3 else "")
    ref = f'{author_str}. "{title}."'
    if journal: ref += f" {journal}"
    if volume:  ref += f", vol. {volume}"
    if issue:   ref += f", no. {issue}"
    if year:    ref += f", {year}"
    if pages:   ref += f", pp. {pages}"
    ref += "."
    if doi:
        ref += f" {doi if doi.startswith('http') else 'https://doi.org/' + doi}."
    return ref

def _build_chicago(authors, year, title, journal, volume, issue, pages, doi):
    chi_authors = []
    for i, name in enumerate((authors or [])[:3]):
        parts = name.strip().split()
        if len(parts) >= 2:
            chi_authors.append(f"{parts[-1]}, {' '.join(parts[:-1])}" if i == 0 else name.strip())
        elif name.strip():
            chi_authors.append(name.strip())
    if not chi_authors:
        author_str = "Unknown"
    elif len(chi_authors) == 1:
        author_str = chi_authors[0] + (", et al." if len(authors or []) > 1 else "")
    else:
        author_str = ", ".join(chi_authors) + (", et al." if len(authors or []) > 3 else "")
    y   = str(year) if year else "n.d."
    ref = f'{author_str}. {y}. "{title}."'
    if journal: ref += f" {journal}"
    if volume:
        ref += f" {volume}"
        if issue: ref += f" ({issue})"
    if pages: ref += f": {pages}"
    ref += "."
    if doi:
        ref += f" {doi if doi.startswith('http') else 'https://doi.org/' + doi}."
    return ref

def _build_harvard(authors, year, title, journal, volume, issue, pages, doi):
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
    y   = str(year) if year else "n.d."
    ref = f"{author_str} ({y}) '{title}'"
    if journal: ref += f", {journal}"
    if volume:
        ref += f", {volume}"
        if issue: ref += f"({issue})"
    if pages: ref += f", pp. {pages}"
    ref += "."
    if doi:
        d = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        ref += f" Available at: {d} (Accessed: {datetime.now().strftime('%d %B %Y')})."
    return ref

def _all_citations(authors, year, title, journal, volume, issue, pages, doi):
    return {
        "apa":     _build_apa(authors, year, title, journal, volume, issue, pages, doi),
        "mla":     _build_mla(authors, year, title, journal, volume, issue, pages, doi),
        "chicago": _build_chicago(authors, year, title, journal, volume, issue, pages, doi),
        "harvard": _build_harvard(authors, year, title, journal, volume, issue, pages, doi),
    }

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
        for c in sorted(paper.get("concepts") or [], key=lambda x: x.get("score", 0), reverse=True)[:5]
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
    missing    = [l for l, v in [("volume", volume), ("issue", issue), ("page range", pages), ("DOI", doi)] if not v]
    return {
        "title": title, "authors": authors, "year": year, "journal": journal,
        "abstract": abstract, "citations": paper.get("cited_by_count", 0),
        "is_oa": oa.get("is_oa", False), "oa_url": oa.get("oa_url", ""),
        "doi": doi, "concepts": concepts, "openalex_id": paper.get("id", ""),
        "volume": volume, "issue": issue, "pages": pages,
        "apa_reference": _build_apa(authors, year, title, journal, volume, issue, pages, doi),
        "apa_missing": missing, "data_source": "OpenAlex (openalex.org)",
        "ref_warning": "Auto-generated — verify before academic use",
    }

# ─── SEMANTIC SCHOLAR ────────────────────────────────────────────────
def search_semantic_scholar(query, page=1, per_page=RESULTS_PER_SOURCE):
    offset = (page - 1) * per_page
    params = {
        "query": query, "limit": per_page, "offset": offset,
        "fields": "title,authors,year,abstract,citationCount,externalIds,journal,isOpenAccess,openAccessPdf",
    }
    try:
        resp = requests.get("https://api.semanticscholar.org/graph/v1/paper/search",
                            params=params, timeout=15,
                            headers={"User-Agent": "Sturch/2.0 (academic research tool)"})
        if not resp.ok:
            return []
        results = []
        for p in resp.json().get("data", []):
            authors   = [a.get("name", "") for a in p.get("authors", [])]
            year      = p.get("year")
            title     = p.get("title", "")
            doi       = (p.get("externalIds") or {}).get("DOI", "")
            journal   = (p.get("journal") or {}).get("name", "") or "Semantic Scholar"
            missing   = (["DOI"] if not doi else []) + ["volume", "issue", "page range"]
            results.append({
                "title": title, "authors": authors, "year": year, "journal": journal,
                "abstract": p.get("abstract", "") or "", "citations": p.get("citationCount", 0) or 0,
                "is_oa": p.get("isOpenAccess", False),
                "oa_url": (p.get("openAccessPdf") or {}).get("url", ""),
                "doi": f"https://doi.org/{doi}" if doi else "",
                "concepts": [], "openalex_id": p.get("paperId", ""),
                "volume": "", "issue": "", "pages": "",
                "apa_reference": _build_apa(authors, year, title, journal, "", "", "", doi),
                "apa_missing": missing, "data_source": "Semantic Scholar (semanticscholar.org)",
                "ref_warning": "Auto-generated — verify before academic use",
            })
        return results
    except Exception:
        return []

# ─── ARXIV ───────────────────────────────────────────────────────────
def search_arxiv(query, page=1, per_page=RESULTS_PER_SOURCE):
    start  = (page - 1) * per_page
    params = {"search_query": f"all:{query}", "start": start, "max_results": per_page,
              "sortBy": "relevance", "sortOrder": "descending"}
    try:
        resp = requests.get("https://export.arxiv.org/api/query", params=params, timeout=15)
        if not resp.ok:
            return []
        root    = ET.fromstring(resp.text)
        ns      = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns):
            title     = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
            abstract  = (entry.findtext("atom:summary", "", ns) or "").strip().replace("\n", " ")
            authors   = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
            published = entry.findtext("atom:published", "", ns) or ""
            year      = int(published[:4]) if published and published[:4].isdigit() else None
            doi_link = page_link = ""
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "doi": doi_link = link.get("href", "")
                elif link.get("type") == "text/html" or link.get("rel") == "alternate": page_link = link.get("href", "")
            results.append({
                "title": title, "authors": authors, "year": year, "journal": "arXiv",
                "abstract": abstract, "citations": 0, "is_oa": True, "oa_url": page_link,
                "doi": doi_link, "concepts": [], "openalex_id": page_link,
                "volume": "", "issue": "", "pages": "",
                "apa_reference": _build_apa(authors, year, title, "arXiv", "", "", "", doi_link),
                "apa_missing": ["volume", "issue", "page range"],
                "data_source": "arXiv (arxiv.org)",
                "ref_warning": "Auto-generated — verify before academic use",
            })
        return results
    except Exception:
        return []

# ─── PUBMED ──────────────────────────────────────────────────────────
def search_pubmed(query, page=1, per_page=RESULTS_PER_SOURCE):
    retstart = (page - 1) * per_page
    try:
        search_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": query, "retmax": per_page,
                    "retstart": retstart, "retmode": "json", "sort": "relevance"},
            timeout=15, headers={"User-Agent": "Sturch/2.0 (academic research tool)"})
        if not search_resp.ok:
            return []
        ids = search_resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
    except Exception:
        return []
    try:
        summary_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"},
            timeout=15, headers={"User-Agent": "Sturch/2.0 (academic research tool)"})
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
            pubdate = p.get("pubdate", "")
            year    = int(pubdate[:4]) if pubdate and pubdate[:4].isdigit() else None
            doi = next((a.get("value", "") for a in p.get("articleids", []) if a.get("idtype") == "doi"), "")
            missing = [l for l, v in [("volume", volume), ("issue", issue), ("page range", pages), ("DOI", doi)] if not v]
            results.append({
                "title": title, "authors": authors, "year": year, "journal": journal,
                "abstract": "", "citations": 0, "is_oa": False,
                "oa_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "doi": f"https://doi.org/{doi}" if doi else "",
                "concepts": [], "openalex_id": f"pmid:{pmid}",
                "volume": volume, "issue": issue, "pages": pages,
                "apa_reference": _build_apa(authors, year, title, journal, volume, issue, pages, doi),
                "apa_missing": missing, "data_source": "PubMed (pubmed.ncbi.nlm.nih.gov)",
                "ref_warning": "Auto-generated — verify before academic use",
            })
        return results
    except Exception:
        return []

# ─── EMAIL ───────────────────────────────────────────────────────────
def send_email(to_email, subject, html_body):
    if not RESEND_API_KEY:
        return False
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": "Sturch <onboarding@resend.dev>", "to": [to_email],
                  "subject": subject, "html": html_body},
            timeout=15)
        if not resp.ok:
            print(f"Resend error: {resp.text}")
        return resp.ok
    except Exception as e:
        print(f"Email error: {e}")
        return False
