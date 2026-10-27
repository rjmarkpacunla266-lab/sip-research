"""
core.py — Sturch shared utilities
All blueprints import from here.
"""
import os
import re
import threading
import time
import html as _html
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
SUPABASE_URL        = os.getenv("SUPABASE_URL")
SUPABASE_KEY        = os.getenv("SUPABASE_SECRET_KEY")
OPENALEX_URL        = "https://api.openalex.org/works"

def get_int_env(name, default):
    try:
        return int(os.getenv(name))
    except (TypeError, ValueError):
        return default

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

def sb_try(method, table, filters="", data=None):
    """Supabase call that REPORTS failures instead of hiding them.
    Returns (ok, payload, error_text). Errors are also printed to the server log."""
    url = f"{SUPABASE_URL}/rest/v1/{table}" + (f"?{filters}" if filters else "")
    try:
        resp = requests.request(method, url, headers=sb_headers(), json=data, timeout=15)
    except Exception as e:
        print(f"[supabase] {method} {table} failed: {e}", flush=True)
        return False, None, str(e)
    if not resp.ok:
        err = (resp.text or "")[:300]
        print(f"[supabase] {method} {table} -> {resp.status_code}: {err}", flush=True)
        return False, None, err
    try:
        payload = resp.json() if resp.text else []
    except ValueError:
        payload = []
    return True, payload, ""

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
    salt = secrets.token_bytes(16)
    key  = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt:{salt.hex()}:{key.hex()}"

def _check_old_sha256(password, hashed):
    try:
        salt, h = hashed.split(":")
        return hashlib.sha256((password + salt).encode()).hexdigest() == h
    except Exception:
        return False

def check_password(password, hashed):
    if hashed.startswith("scrypt:"):
        try:
            _, salt_hex, key_hex = hashed.split(":")
            salt = bytes.fromhex(salt_hex)
            key  = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
            return key.hex() == key_hex
        except Exception:
            return False
    return _check_old_sha256(password, hashed)

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

# ─── CITATION BUILDERS ───────────────────────────────────────────────
def _s(v):
    """Any value -> clean string (None/ints/floats never crash the builders)."""
    return "" if v is None else str(v).strip()

def _dot(text):
    """Add a full stop unless the text already ends with one (avoids 'et al..' and 'Q..')."""
    text = _s(text)
    return text if text.endswith((".", "?", "!")) else text + "."

_PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "di", "da", "du", "le", "la", "los", "las",
              "ter", "ten", "bin", "ibn", "al", "el", "dos", "das", "do", "st"}
_SUFFIXES  = {"jr", "sr", "ii", "iii", "iv"}
_ORG_WORDS = {"organization", "organisation", "association", "society", "university", "institute", "committee",
              "consortium", "collaboration", "group", "commission", "department", "ministry", "agency", "council",
              "foundation", "centre", "center", "network", "team", "initiative", "board", "bureau", "federation",
              "union", "laboratory", "hospital", "college", "school", "academy", "alliance", "program", "programme"}
# Search sources put these in the journal field when the real journal is unknown. They are NOT journals.
_PLACEHOLDER_JOURNALS = {"crossref", "semantic scholar", "europe pmc", "pubmed", "openalex"}

def _name_parts(name):
    """-> (family, given_tokens, initials_only, suffix).
    Understands 'Jane Q. Smith', 'Smith, Jane Q.', 'Waterhouse A' / 'Van Der Meer JWM' (Europe PMC / PubMed style),
    particles ('Maria de la Cruz'), suffixes ('Martin Luther King Jr.') and group authors."""
    name = re.sub(r"\s+", " ", _s(name))
    if not name:
        return "", [], False, ""
    words = {w.strip(".,").lower() for w in name.split()}
    if len(name.split()) >= 2 and (words & _ORG_WORDS):
        return name, [], False, ""                          # group author: keep exactly as written
    suffix = ""
    toks = name.split()
    if len(toks) > 1 and toks[-1].strip(".,").lower() in _SUFFIXES:
        suffix = toks.pop().strip(",")
        if suffix.lower().rstrip(".") in ("jr", "sr"):
            suffix = suffix.rstrip(".") + "."
        name = " ".join(toks).rstrip(",")
    if "," in name:
        fam, _, giv = name.partition(",")
        gt = giv.split()
        return fam.strip(), gt, (all(len(t.strip(".")) <= 3 and t.strip(".").isupper() for t in gt) if gt else False), suffix
    parts = name.split()
    if len(parts) == 1:
        return parts[0], [], False, suffix
    last = parts[-1]
    if re.fullmatch(r"[A-Z]{1,3}", last) and any(c.islower() for c in "".join(parts[:-1])):
        return " ".join(parts[:-1]), [last], True, suffix   # 'Waterhouse A', 'Smith JK', 'Van Der Meer JWM'
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in _PARTICLES:     # keep 'de la', 'van der' with the surname
        i -= 1
    return " ".join(parts[i:]), parts[:i], False, suffix

def _initials(given, initials_only):
    out = []
    for tok in given:
        t = re.sub(r"^[^\w]+", "", tok.strip("."))          # drop stray ( [ " etc.
        if not t:
            continue
        if initials_only and t.isupper() and len(t) <= 3:
            out.extend(f"{c}." for c in t)                  # 'JK' -> J. K.
        else:
            out.append("-".join(f"{p[0].upper()}." for p in t.split("-") if p))
    return " ".join(out)

def _author_list(authors):
    """Accept a list (strings or {'name':..} dicts) or one string; never explode a string into letters."""
    if authors is None:
        return []
    if isinstance(authors, str):
        authors = re.split(r"\s*;\s*|\s*\n\s*|\s+and\s+", authors)
    elif not isinstance(authors, (list, tuple)):
        return []
    out = []
    for a in authors[:100]:
        if isinstance(a, dict):
            a = a.get("name") or a.get("display_name") or a.get("fullName") or ""
        a = _s(a)[:120]
        if a:
            out.append(a)
    return out

def _people(authors):
    return [_name_parts(a) for a in _author_list(authors)]

def _given_full(p):
    fam, given, ini, suf = p
    if ini:
        return _initials(given, True)
    return " ".join(t + "." if len(t) == 1 and t.isalpha() else t for t in given)   # 'A' -> 'A.' 

def _inverted(p, full=False):
    """'Smith, J. Q.' (APA/Harvard initials) or 'Smith, Jane Q.' (MLA/Chicago full names)."""
    fam, given, ini, suf = p
    g = _given_full(p) if (full or ini) else _initials(given, False)
    out = f"{fam}, {g}" if g else fam
    return f"{out}, {suf}" if suf else out

def _natural(p):
    """'Jane Q. Smith' (second and later authors in MLA/Chicago)."""
    fam, given, ini, suf = p
    out = f"{_given_full(p)} {fam}".strip()
    return f"{out} {suf}" if suf else out

def _doi_url(doi):
    """'10.1/x', 'doi:10.1/x', 'doi.org/10.1/x', 'http://dx.doi.org/10.1/x' -> 'https://doi.org/10.1/x'.
    Other links (e.g. an arXiv page) are kept as they are."""
    d = _s(doi)
    if not d:
        return ""
    d = re.sub(r"^doi:\s*", "", d, flags=re.I)
    m = re.match(r"^(?:https?://)?(?:dx\.)?doi\.org/(.+)$", d, re.I)
    if m:
        return "https://doi.org/" + m.group(1)
    return d if d.lower().startswith("http") else "https://doi.org/" + d

def _link_fallback(journal, doi, oa_url):
    """arXiv papers often have no DOI; cite the arXiv page instead of leaving the reference without a link."""
    if not _s(doi) and _s(journal).lower() == "arxiv":
        return _s(oa_url)
    return _s(doi)

def _pages(pages):
    """'1234-40' -> '1234–1240', '45-67' -> '45–67', 'e123-e130' -> 'e123–e130', 'e123' stays."""
    p = _s(pages)
    m = re.fullmatch(r"(\d+)\s*[-–—]+\s*(\d+)", p)
    if m:
        a, b = m.groups()
        if len(b) < len(a):
            b = a[:len(a) - len(b)] + b
        return f"{a}–{b}"
    return re.sub(r"\s*[-–—]+\s*", "–", p)

def _en_dash(pages):
    return _pages(pages)

def _pp(pages):
    """'pp. 45–67' for a range, 'p. 5' for one page, plain 'e123' for article numbers."""
    p = _pages(pages)
    if not p:
        return ""
    if "–" in p:
        return f"pp. {p}"
    return f"p. {p}" if p.isdigit() else p

def _year(year):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", _s(year))
    return m.group(1) if m else "n.d."

def _journal(journal):
    j = _s(journal)
    return "" if j.lower() in _PLACEHOLDER_JOURNALS else j

def _title(title):
    t = _s(title)
    m = re.fullmatch(r"\[([^\[\]]+)\]\.?", t)               # PubMed '[Title.]' = translated title
    if m:
        t = m.group(1).strip()
    return t or "Untitled"

def _build_apa(authors, year, title, journal, volume, issue, pages, doi):
    names = [_inverted(p) for p in _people(authors)]
    y, ttl, journal, volume, issue = _year(year), _title(title), _journal(journal), _s(volume), _s(issue)
    pg = _pages(pages)
    if not names:
        author_str = ""
    elif len(names) == 1:
        author_str = names[0]
    elif len(names) <= 20:                                  # APA 7: list up to 20 authors
        author_str = ", ".join(names[:-1]) + ", & " + names[-1]
    else:                                                   # 21+: first 19, ellipsis, last author
        author_str = ", ".join(names[:19]) + ", … " + names[-1]
    vol_issue  = f", {volume}({issue})" if volume and issue else (f", {volume}" if volume else "")
    pages_part = f", {pg}" if pg else ""
    # APA 7: no author -> the title moves to the author position
    out = f"{_dot(author_str)} ({y}). {_dot(ttl)}" if author_str else f"{_dot(ttl)} ({y})."
    if journal:
        out += f" {journal}{vol_issue}{pages_part}."
    d = _doi_url(doi)
    return out + (f" {d}" if d else "")

def _build_mla(authors, year, title, journal, volume, issue, pages, doi):
    ppl = _people(authors)
    y, ttl, journal, volume, issue = _year(year), _title(title), _journal(journal), _s(volume), _s(issue)
    if not ppl:
        author_str = ""
    elif len(ppl) == 1:
        author_str = _inverted(ppl[0], full=True)
    elif len(ppl) == 2:
        author_str = f"{_inverted(ppl[0], full=True)}, and {_natural(ppl[1])}"
    else:
        author_str = f"{_inverted(ppl[0], full=True)}, et al"
    bits = []
    if journal: bits.append(journal)
    if volume:  bits.append(f"vol. {volume}")
    if issue:   bits.append(f"no. {issue}")
    bits.append(y)
    pp = _pp(pages)
    if pp:      bits.append(pp)
    ref = (f'{_dot(author_str)} ' if author_str else "") + f'"{_dot(ttl)}"'
    ref += " " + _dot(", ".join(bits))
    d = _doi_url(doi)
    return ref + (f" {d}." if d else "")

def _build_chicago(authors, year, title, journal, volume, issue, pages, doi):
    """Chicago author-date style (17th ed.)."""
    ppl = _people(authors)
    y, ttl, journal, volume, issue = _year(year), _title(title), _journal(journal), _s(volume), _s(issue)
    if not ppl:
        author_str = ""
    else:
        shown = ppl if len(ppl) <= 10 else ppl[:7]
        names = [_inverted(shown[0], full=True)] + [_natural(p) for p in shown[1:]]
        if len(ppl) > 10:
            author_str = ", ".join(names) + ", et al."
        elif len(names) == 1:
            author_str = names[0]
        elif len(names) == 2:
            author_str = f"{names[0]}, and {names[1]}"
        else:
            author_str = ", ".join(names[:-1]) + ", and " + names[-1]
    ref  = f'{_dot(author_str)} {_dot(y)} "{_dot(ttl)}"' if author_str else f'"{_dot(ttl)}" {_dot(y)}'
    tail = journal
    pg   = _pages(pages)
    if volume:
        tail += f" {volume}"
        if issue: tail += f" ({issue})"
    if pg: tail += f": {pg}"
    if tail.strip():
        ref += " " + _dot(tail.strip())
    d = _doi_url(doi)
    return ref + (f" {d}." if d else "")

def _build_harvard(authors, year, title, journal, volume, issue, pages, doi):
    names = [_inverted(p) for p in _people(authors)]
    y, ttl, journal, volume, issue = _year(year), _title(title), _journal(journal), _s(volume), _s(issue)
    if not names:
        author_str = ""
    elif len(names) == 1:
        author_str = names[0]
    elif len(names) <= 3:
        author_str = ", ".join(names[:-1]) + " and " + names[-1]
    else:
        author_str = names[0] + " et al."
    ref = f"{author_str} ({y}) '{ttl.rstrip('.')}'" if author_str else f"'{ttl.rstrip('.')}' ({y})"
    if journal: ref += f", {journal}"
    if volume:
        ref += f", {volume}"
        if issue: ref += f"({issue})"
    pp = _pp(pages)
    if pp: ref += f", {pp}"
    ref += "."
    d = _doi_url(doi)
    if d:
        ref += f" Available at: {d} (Accessed: {datetime.now().strftime('%d %B %Y')})."
    return ref

def _all_citations(authors, year, title, journal, volume, issue, pages, doi, oa_url=""):
    doi = _link_fallback(journal, doi, oa_url)
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
    year       = paper.get("publication_year") or "n.d."
    title      = clean_text(paper.get("title", ""))
    journal    = source.get("display_name", "")
    doi        = paper.get("doi") or ""
    volume     = (biblio.get("volume") or "")
    issue      = (biblio.get("issue") or "")
    first_page = (biblio.get("first_page") or "")
    last_page  = (biblio.get("last_page") or "")
    pages      = f"{first_page}\u2013{last_page}" if first_page and last_page else (str(first_page or ""))
    missing    = [l for l, v in [("volume", volume), ("issue", issue), ("page range", pages), ("DOI", doi)] if not v]
    lic = str(((paper.get("best_oa_location") or {}).get("license") or loc.get("license") or "")).lower()
    return {
        "title": title, "date": paper.get("publication_date") or "", "authors": authors, "year": year, "journal": journal,
        "license": lic, "abstract": abstract, "citations": paper.get("cited_by_count", 0),
        "is_oa": oa.get("is_oa", False), "oa_url": oa.get("oa_url", ""),
        "doi": doi, "concepts": concepts, "openalex_id": paper.get("id", ""),
        "volume": volume, "issue": issue, "pages": pages,
        "apa_reference": _build_apa(authors, year, title, journal, volume, issue, pages, doi),
        "apa_missing": missing, "data_source": "OpenAlex (openalex.org)",
        "ref_warning": "Auto-generated — verify before academic use",
    }

# ─── ABSTRACT POLICY ─────────────────────────────────────────────────
# Abstracts are shown (and used by the tools) only when the source allows it:
#   arXiv: its terms put descriptive metadata, abstracts included, under CC0.
#   OpenAlex: only when the paper itself carries an open licence that allows reuse.
# Every other abstract is blanked after searching and filtering, and flagged so the page can say so.
_OPEN_LICENSES = {"cc0", "cc-by", "cc-by-sa", "public-domain", "pd"}

def abstract_allowed(p):
    src = (p.get("data_source") or "").lower()
    if "arxiv" in src:
        return True
    if "openalex" in src:
        return (p.get("license") or "").lower() in _OPEN_LICENSES
    return False

def hide_restricted_abstracts(papers):
    for p in papers:
        if p.get("abstract") and not abstract_allowed(p):
            p["abstract"] = ""
            p["abstract_hidden"] = True
    return papers

# ─── SEARCH HELPERS ──────────────────────────────────────────────────
_JUNK_MARKERS = ("share add to", "export ris", "exportris", "get e-alerts", "return to issue",
                 "view author information", "add full text with reference", "add description")

def clean_text(text):
    """Unescape HTML entities, strip tags (incl. escaped ones like &lt;i&gt;), collapse spaces."""
    if not text:
        return ""
    text = _html.unescape(str(text))
    text = re.sub(r"</?(?:sub|sup|i|b|em|strong|u|italic|bold|jats:italic|jats:bold|jats:sub|jats:sup)\b[^>]*>", "", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:)])", r"\1", text)

def clean_abstract(text):
    """clean_text + drop scraped page junk (publisher menus, 'Export RIS', etc.)."""
    text = clean_text(text)
    low = text.lower()
    if any(m in low for m in _JUNK_MARKERS):
        return ""
    return re.sub(r"^(abstract|summary)\b[:.\s]*", "", text, flags=re.I).strip()

def _fmt_date(parts):
    """Crossref date-parts [y, m, d] -> 'YYYY-MM-DD' (0 for unknown month/day)."""
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        d = int(parts[2]) if len(parts) > 2 and parts[2] else 0
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return ""

def _safe_terms(query):
    """Single words with quotes/brackets/operators removed (safe inside API query syntax)."""
    out = []
    for t in re.split(r"\s+", (query or "").strip()):
        t = re.sub(r"[^\w\-]", "", t)
        if t:
            out.append(t)
    return out

def _terms(query):
    return [t for t in re.split(r"\s+", (query or "").strip()) if t]

def _year_bounds(year_from, year_to):
    return (str(year_from) if year_from else None, str(year_to) if year_to else None)

# ─── OPENALEX ────────────────────────────────────────────────────────
def search_openalex(query, page=1, per_page=RESULTS_PER_SOURCE,
                    year_from=None, year_to=None, oa_only=False, sort="cited", field="both", loose=False):
    """Returns (papers, total). Matches title+abstract only, so papers that merely
    mention the topic somewhere in their full text don't flood the citation sort."""
    terms  = _safe_terms(query)
    strict = " AND ".join(terms) if terms else query
    fkey   = {"title": "title.search", "abstract": "abstract.search"}.get(field, "title_and_abstract.search")
    base = []
    if year_from: base.append(f"from_publication_date:{year_from}-01-01")
    if year_to:   base.append(f"to_publication_date:{year_to}-12-31")
    if oa_only:   base.append("is_oa:true")
    sort_by  = "publication_date:desc" if sort == "recent" else "cited_by_count:desc"
    attempts = [
        {"filter": ",".join(base + [f"{fkey}:{strict}"])},
        {"search": query, "filter": ",".join(base)},          # safety net
    ]
    if loose:                        # any-word relevance search (used by Source Tracer)
        attempts = [{"search": query, "filter": ",".join(base)}]
    for extra in attempts:
        params = {"per-page": per_page, "page": page, "sort": sort_by}
        if loose:
            params.pop("sort")       # let OpenAlex rank by relevance
        params.update({k: v for k, v in extra.items() if v})
        try:
            resp = requests.get(OPENALEX_URL, params=params, timeout=15)
            data = resp.json()
        except Exception:
            continue
        if resp.ok and data.get("results"):
            return [format_paper(p) for p in data["results"]], data.get("meta", {}).get("count", 0)
    return [], 0

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
# ─── arXiv: polite use ───────────────────────────────────────────────
# arXiv's terms: no more than one request every 3 seconds, one connection. Repeated searches are cached for 10 minutes.
_AX_CACHE, _AX_TTL = {}, 600
_AX_LOCK, _AX_NEXT = threading.Lock(), [0.0]

def _arxiv_wait():
    with _AX_LOCK:
        now = time.time()
        slot = max(now, _AX_NEXT[0])
        if slot - now > 6:
            return False
        _AX_NEXT[0] = slot + 3.0
    if slot > now:
        time.sleep(slot - now)
    return True

def search_arxiv(query, page=1, per_page=RESULTS_PER_SOURCE,
                 year_from=None, year_to=None, oa_only=False, sort="cited", field="both", loose=False):
    start = (page - 1) * per_page
    def _ax(t):
        if field == "title":    return f"ti:{t}"
        if field == "abstract": return f"abs:{t}"
        return f"(ti:{t} OR abs:{t})"
    q = (" OR " if loose else " AND ").join(_ax(t) for t in _safe_terms(query)) or f"all:{query}"
    if loose:
        q = f"({q})"
    if year_from or year_to:
        lo = f"{year_from or 1991}01010000"
        hi = f"{year_to or 2100}12312359"
        q += f" AND submittedDate:[{lo} TO {hi}]"
    params = {"search_query": q, "start": start, "max_results": per_page,
              "sortBy": "submittedDate" if sort == "recent" else "relevance",
              "sortOrder": "descending"}
    ckey = tuple(sorted((k, str(v)) for k, v in params.items()))
    hit = _AX_CACHE.get(ckey)
    if hit and time.time() - hit[0] < _AX_TTL:
        return [dict(p) for p in hit[1]]
    if not _arxiv_wait():
        return []                                   # arXiv asks for one request every 3 seconds; skip rather than queue for long
    try:
        resp = requests.get("https://export.arxiv.org/api/query", params=params, timeout=15)
        if not resp.ok:
            return []
        root    = ET.fromstring(resp.text)
        ns      = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns):
            title     = clean_text(entry.findtext("atom:title", "", ns) or "")
            abstract  = clean_abstract(entry.findtext("atom:summary", "", ns) or "")
            authors   = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
            published = entry.findtext("atom:published", "", ns) or ""
            year      = int(published[:4]) if published and published[:4].isdigit() else None
            doi_link = page_link = ""
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "doi": doi_link = link.get("href", "")
                elif link.get("type") == "text/html" or link.get("rel") == "alternate": page_link = link.get("href", "")
            results.append({
                "title": title, "date": published[:10], "authors": authors, "year": year, "journal": "arXiv",
                "abstract": abstract, "citations": 0, "is_oa": True, "oa_url": page_link,
                "doi": doi_link, "concepts": [], "openalex_id": page_link,
                "volume": "", "issue": "", "pages": "",
                "apa_reference": _build_apa(authors, year, title, "arXiv", "", "", "", doi_link or page_link),
                "apa_missing": ["volume", "issue", "page range"],
                "data_source": "arXiv (arxiv.org)",
                "ref_warning": "Auto-generated — verify before academic use",
            })
        if len(_AX_CACHE) > 300:
            _AX_CACHE.clear()
        _AX_CACHE[ckey] = (time.time(), [dict(p) for p in results])
        return results
    except Exception:
        return []

# ─── PUBMED ──────────────────────────────────────────────────────────
def search_pubmed(query, page=1, per_page=RESULTS_PER_SOURCE,
                  year_from=None, year_to=None, oa_only=False, sort="cited", field="both", loose=False):
    retstart = (page - 1) * per_page
    def _pm(t):
        if field == "title":    return f"{t}[Title]"
        if field == "abstract": return f"({t}[Title/Abstract] NOT {t}[Title])"
        return f"{t}[Title/Abstract]"
    term = (" OR " if loose else " AND ").join(_pm(t) for t in _safe_terms(query)) or query
    if oa_only:
        term = f"({term}) AND free full text[sb]"
    es_params = {"db": "pubmed", "term": term, "retmax": per_page, "retstart": retstart,
                 "retmode": "json", "sort": "pub_date" if sort == "recent" else "relevance"}
    if year_from or year_to:
        es_params.update({"datetype": "pdat",
                          "mindate": str(year_from or 1800), "maxdate": str(year_to or 2100)})
    try:
        search_resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params=es_params,
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
            title   = clean_text(p.get("title", ""))
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
                "title": title, "date": (p.get("sortpubdate", "") or "")[:10].replace("/", "-"), "authors": authors, "year": year, "journal": journal,
                "abstract": "", "citations": 0, "is_oa": bool(oa_only),
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


# ─── CROSSREF ────────────────────────────────────────────────────────
def search_crossref(query, page=1, per_page=RESULTS_PER_SOURCE,
                    year_from=None, year_to=None, oa_only=False, sort="cited", field="both", loose=False):
    offset = (page - 1) * per_page
    cr_params = {("query.title" if field == "title" else "query"): query, "rows": per_page, "offset": offset,
                 "sort": "published" if sort == "recent" else "is-referenced-by-count",
                 "order": "desc",
                 "select": "title,author,published,container-title,DOI,is-referenced-by-count,abstract,URL,license"}
    if loose:                                   # relevance order
        cr_params.pop("sort", None); cr_params.pop("order", None)
    flt = []
    if year_from: flt.append(f"from-pub-date:{year_from}-01-01")
    if year_to:   flt.append(f"until-pub-date:{year_to}-12-31")
    if flt:
        cr_params["filter"] = ",".join(flt)
    try:
        resp = requests.get(
            "https://api.crossref.org/works",
            params=cr_params,
            timeout=15,
            headers={"User-Agent": "Sturch/3.0 (mailto:pacunlarjmark@gmail.com)"})
        if not resp.ok:
            return []
        results = []
        for p in resp.json().get("message", {}).get("items", []):
            title   = clean_text(" ".join(p.get("title") or [""]))
            authors = [f"{a.get('given','')} {a.get('family','')}".strip()
                       for a in p.get("author") or []]
            pub     = p.get("published", {}).get("date-parts", [[None]])[0]
            year    = pub[0] if pub else None
            journal = " ".join(p.get("container-title") or []) or "Crossref"
            doi     = p.get("DOI", "")
            abstract = clean_abstract(p.get("abstract", "") or "")
            missing = [l for l, v in [("volume",""),("issue",""),("page range","")] if not v]
            results.append({
                "title": title, "date": _fmt_date(pub), "authors": authors, "year": year, "journal": journal,
                "abstract": abstract, "citations": p.get("is-referenced-by-count", 0) or 0,
                "is_oa": False, "oa_url": p.get("URL", ""),
                "doi": f"https://doi.org/{doi}" if doi else "",
                "concepts": [], "openalex_id": doi,
                "volume": "", "issue": "", "pages": "",
                "apa_reference": _build_apa(authors, year, title, journal, "", "", "", doi),
                "apa_missing": missing, "data_source": "Crossref (crossref.org)",
                "ref_warning": "Auto-generated — verify before academic use",
            })
        return results
    except Exception:
        return []

# ─── EUROPE PMC ──────────────────────────────────────────────────────
def _epmc_query(query, year_from, year_to, oa_only, strict, field="both", loose=False):
    if strict:
        def _ep(t):
            if field == "title":    return f'TITLE:"{t}"'
            if field == "abstract": return f'ABSTRACT:"{t}"'
            return f'(TITLE:"{t}" OR ABSTRACT:"{t}")'
        parts = [_ep(t) for t in _safe_terms(query)]
        q = " AND ".join(parts) or query
    elif loose:
        q = "(" + (" OR ".join(_safe_terms(query)) or query) + ")"
    else:
        q = f"({query})"
    if year_from or year_to:
        q += f" AND (PUB_YEAR:[{year_from or 1800} TO {year_to or 2100}])"
    if oa_only:
        q += " AND OPEN_ACCESS:y"
    return q

def search_europe_pmc(query, page=1, per_page=RESULTS_PER_SOURCE,
                      year_from=None, year_to=None, oa_only=False, sort="cited", field="both", loose=False):
    sort_by = "P_PDATE_D desc" if sort == "recent" else "CITED desc"
    def _call(strict):
        params = {"query": _epmc_query(query, year_from, year_to, oa_only, strict and not loose, field, loose),
                  "pageSize": per_page, "page": page, "format": "json",
                  "resultType": "core", "sort": sort_by}
        if loose:
            params.pop("sort")                  # relevance order
        return requests.get(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            params=params,
            timeout=15, headers={"User-Agent": "Sturch/3.0 (academic research tool)"})
    try:
        resp  = _call(True)           # title/abstract matches only (no full-text noise)
        items = resp.json().get("resultList", {}).get("result", []) if resp.ok else []
        if not items:                 # safety net: plain query + same filters
            resp  = _call(False)
            items = resp.json().get("resultList", {}).get("result", []) if resp.ok else []
        results = []
        for p in items:
            title   = clean_text(p.get("title", ""))
            authors = [a.get("fullName", "") for a in p.get("authorList", {}).get("author", [])]
            year    = p.get("pubYear")
            journal = p.get("journalTitle", "") or "Europe PMC"
            doi     = p.get("doi", "")
            abstract = clean_abstract(p.get("abstractText", "") or "")
            cites   = p.get("citedByCount", 0) or 0
            is_oa   = p.get("isOpenAccess", "N") == "Y"
            oa_url  = f"https://europepmc.org/article/{p.get('source','')}/{p.get('id','')}" if p.get("id") else ""
            missing = [l for l, v in [("volume", p.get("journalVolume","")),
                                       ("issue", p.get("issue","")),
                                       ("page range", p.get("pageInfo",""))] if not v]
            results.append({
                "title": title, "date": p.get("firstPublicationDate") or "", "authors": authors, "year": year, "journal": journal,
                "abstract": abstract, "citations": cites,
                "is_oa": is_oa, "oa_url": oa_url,
                "doi": f"https://doi.org/{doi}" if doi else "",
                "concepts": [], "openalex_id": p.get("id", ""),
                "volume": p.get("journalVolume",""), "issue": p.get("issue",""),
                "pages": p.get("pageInfo",""),
                "apa_reference": _build_apa(authors, year, title, journal,
                                            p.get("journalVolume",""), p.get("issue",""),
                                            p.get("pageInfo",""), doi),
                "apa_missing": missing, "data_source": "Europe PMC (europepmc.org)",
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
