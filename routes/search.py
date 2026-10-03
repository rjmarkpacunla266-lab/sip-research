"""routes/search.py — Search, load-more, paper reader, related, history"""
import requests
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, session, jsonify
from core import (login_required, get_user, OPENALEX_URL,
                  RESULTS_PER_SOURCE, format_paper, reconstruct_abstract,
                  search_arxiv, search_pubmed, search_crossref, search_europe_pmc, search_openalex,
                  sb_post, sb_get, sb_patch, _all_citations)
from bs4 import BeautifulSoup as BS

search_bp = Blueprint("search", __name__)

# ─── /api/me ─────────────────────────────────────────────────────────
@search_bp.route("/api/me")
@login_required
def get_me():
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "email": user["email"],
    })

# ─── Shared search engine ────────────────────────────────────────────
ALL_SOURCES = ["openalex", "arxiv", "pubmed", "crossref", "europepmc"]
SOURCE_LABELS = {"openalex": "OpenAlex", "arxiv": "arXiv", "pubmed": "PubMed",
                 "crossref": "Crossref", "europepmc": "Europe PMC"}

def _to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None

def _read_filters():
    """Read sources / year / OA / sort from the query string."""
    chosen  = [s for s in request.args.get("sources", "").split(",") if s in ALL_SOURCES]
    return {
        "sources": chosen or ALL_SOURCES,
        "year_from": _to_int(request.args.get("year_from")),
        "year_to":   _to_int(request.args.get("year_to")),
        "oa_only":   request.args.get("oa") == "1",
        "sort":      request.args.get("sort", "cited"),
    }

def _fetch_openalex(query, page, f):
    return search_openalex(query, page, RESULTS_PER_SOURCE, year_from=f["year_from"],
                           year_to=f["year_to"], oa_only=f["oa_only"], sort=f["sort"])

def _merge_dedupe(papers):
    """One entry per paper. Same DOI/title in several sources -> keep the most-cited copy."""
    best = {}
    for p in papers:
        doi = (p.get("doi") or "").strip().lower().replace("https://doi.org/", "")
        key = doi or (p.get("title") or "").strip().lower()[:60]
        if not key:
            continue
        old = best.get(key)
        if old is None:
            best[key] = p
            continue
        keep, other = (p, old) if (p.get("citations") or 0) > (old.get("citations") or 0) else (old, p)
        if other.get("is_oa") and not keep.get("is_oa"):
            keep["is_oa"] = True
            keep["oa_url"] = keep.get("oa_url") or other.get("oa_url")
        best[key] = keep
    return list(best.values())

def _sort_date(p):
    """ISO date for ordering; year-only papers sort as 'YYYY-00-00' (before dated ones that year)."""
    d = (p.get("date") or "").strip()
    if d:
        return d
    y = _to_int(p.get("year"))
    return f"{y:04d}-00-00" if y else ""

def _apply_filters_and_sort(papers, f):
    """Safety net: sources that ignore year/OA params are filtered here."""
    out = []
    for p in papers:
        y = _to_int(p.get("year"))
        if f["year_from"] and (y is None or y < f["year_from"]): continue
        if f["year_to"]   and (y is None or y > f["year_to"]):   continue
        if f["oa_only"] and not p.get("is_oa"):                  continue
        out.append(p)
    if f["sort"] == "recent":
        out.sort(key=lambda x: (_sort_date(x), x.get("citations") or 0), reverse=True)
    else:
        out.sort(key=lambda x: x.get("citations") or 0, reverse=True)
    return out

def _run_search(query, page, user, log=True):
    f = _read_filters()
    jobs = {}
    kw   = dict(year_from=f["year_from"], year_to=f["year_to"], oa_only=f["oa_only"], sort=f["sort"])
    with ThreadPoolExecutor(max_workers=5) as ex:
        if "openalex" in f["sources"]:  jobs[ex.submit(_fetch_openalex, query, page, f)] = "openalex"
        if "arxiv" in f["sources"]:     jobs[ex.submit(search_arxiv, query, page, RESULTS_PER_SOURCE, **kw)]      = "arxiv"
        if "pubmed" in f["sources"]:    jobs[ex.submit(search_pubmed, query, page, RESULTS_PER_SOURCE, **kw)]     = "pubmed"
        if "crossref" in f["sources"]:  jobs[ex.submit(search_crossref, query, page, RESULTS_PER_SOURCE, **kw)]   = "crossref"
        if "europepmc" in f["sources"]: jobs[ex.submit(search_europe_pmc, query, page, RESULTS_PER_SOURCE, **kw)] = "europepmc"
        raw, total = [], 0
        for fut in as_completed(jobs):
            try:
                res = fut.result()
                if jobs[fut] == "openalex":
                    papers, count = res
                    total += count
                    raw.extend(papers)
                else:
                    raw.extend(res)
            except Exception:
                pass
    results = _apply_filters_and_sort(_merge_dedupe(raw), f)
    if log:
        sb_post("search_logs", {"user_id": user["id"], "query": query, "results": len(results),
                                "searched_at": datetime.now().isoformat()})
    return {"results": results, "total": total or len(results), "query": query, "page": page,
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "ref_disclaimer": "References auto-generated — verify before academic use"}

# ─── /api/search ─────────────────────────────────────────────────────
@search_bp.route("/api/search")
@login_required
def search():
    query = request.args.get("q", "").strip()
    page  = max(1, _to_int(request.args.get("page")) or 1)
    if not query:
        return jsonify({"error": "Please enter a search query"}), 400
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(_run_search(query, page, user, log=True))

# ─── /api/load-more ──────────────────────────────────────────────────
@search_bp.route("/api/load-more")
@login_required
def load_more():
    query = request.args.get("q", "").strip()
    page  = max(1, _to_int(request.args.get("page")) or 2)
    if not query:
        return jsonify({"error": "Query required"}), 400
    if page < 2:
        return jsonify({"error": "Page must be 2 or higher"}), 400
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(_run_search(query, page, user, log=False))

# ─── /api/history ────────────────────────────────────────────────────
@search_bp.route("/api/history")
@login_required
def get_history():
    logs = sb_get("search_logs", f"user_id=eq.{session['user_id']}&order=searched_at.desc&limit=50")
    return jsonify(logs or [])

# ─── /api/related ────────────────────────────────────────────────────
def _openalex_related(query):
    params = {"search": query, "per-page": 20, "page": 1, "sort": "cited_by_count:desc"}
    try:
        resp = requests.get(OPENALEX_URL, params=params, timeout=15)
        return [format_paper(p) for p in resp.json().get("results", [])]
    except Exception:
        return []

@search_bp.route("/api/related")
@login_required
def related_papers():
    concepts = request.args.get("concepts", "")
    title    = request.args.get("title", "")
    query    = concepts or title
    if not query:
        return jsonify({"error": "concepts or title required"}), 400
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    results = _openalex_related(query)
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": (user.get("search_count") or 0) + 1})
    return jsonify({"results": results})

# ─── /api/citations ──────────────────────────────────────────────────
@search_bp.route("/api/citations", methods=["POST"])
@login_required
def get_citations():
    data = request.get_json() or {}
    if not data.get("title"):
        return jsonify({"error": "Title is required"}), 400
    return jsonify(_all_citations(
        data.get("authors") or [], data.get("year") or "n.d.",
        data.get("title") or "", data.get("journal") or "",
        data.get("volume") or "", data.get("issue") or "",
        data.get("pages") or "", data.get("doi") or ""))

# ─── /api/share ──────────────────────────────────────────────────────
@search_bp.route("/api/share", methods=["POST"])
@login_required
def share_paper():
    data  = request.get_json() or {}
    doi   = (data.get("doi") or "").strip()
    oa    = (data.get("oa_url") or "").strip()
    title = (data.get("title") or "").strip()
    if doi:
        link   = doi if doi.startswith("http") else f"https://doi.org/{doi}"
        source = "DOI"
    elif oa:
        link   = oa
        source = "Open Access"
    elif title:
        link   = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(title)
        source = "Google Scholar"
    else:
        return jsonify({"error": "No shareable link available"}), 404
    return jsonify({"url": link, "source": source})

# ─── Paper reader helpers ─────────────────────────────────────────────
def _clean_html(html):
    soup = BS(html, "html.parser")
    for tag in soup(["script","style","nav","header","footer","aside","figure","img"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)

def _fetch_arxiv_full(oa_url):
    try:
        arxiv_id = oa_url.rstrip("/").split("/")[-1]
        resp     = requests.get(f"https://export.arxiv.org/abs/{arxiv_id}", timeout=15)
        if not resp.ok:
            return None
        soup    = BS(resp.text, "html.parser")
        abs_tag = soup.find("blockquote", class_="abstract")
        return abs_tag.get_text(strip=True).replace("Abstract:", "").strip() if abs_tag else None
    except Exception:
        return None

def _fetch_oa_url(oa_url):
    try:
        resp = requests.get(oa_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if not resp.ok:
            return None
        ct = resp.headers.get("Content-Type", "")
        if "pdf" in ct:
            return None
        return _clean_html(resp.text)[:8000]
    except Exception:
        return None

@search_bp.route("/paper")
@login_required
def paper():
    return render_template("paper.html")

@search_bp.route("/api/fetch-paper")
@login_required
def fetch_paper():
    oa_url      = request.args.get("oa_url", "").strip()
    openalex_id = request.args.get("openalex_id", "").strip()
    abstract    = request.args.get("abstract", "").strip()
    if not oa_url and not openalex_id and not abstract:
        return jsonify({"error": "oa_url or openalex_id required"}), 400

    text         = None
    image_notice = False

    if oa_url:
        if "arxiv.org" in oa_url:
            text = _fetch_arxiv_full(oa_url)
        if not text:
            text = _fetch_oa_url(oa_url)

    if not text and openalex_id and openalex_id.startswith("pmid:"):
        pmid = openalex_id.replace("pmid:", "")
        try:
            resp = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                                params={"db": "pubmed", "id": pmid, "retmode": "text", "rettype": "abstract"},
                                timeout=15)
            if resp.ok and resp.text.strip():
                text = resp.text.strip()[:6000]
        except Exception:
            pass

    # Fallback: use abstract from search results if full text unavailable
    if not text and abstract:
        text = abstract
        image_notice = True

    if not text:
        return jsonify({"error": "Could not fetch full text for this paper.", "available": False})

    return jsonify({"text": text, "available": True, "image_notice": image_notice,
                    "abstract_only": image_notice})
