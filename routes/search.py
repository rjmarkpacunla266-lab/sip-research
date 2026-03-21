"""routes/search.py — Search, load-more, paper reader, related, history"""
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, session, jsonify
from core import (login_required, get_user, total_points, points_used, points_remaining,
                  can_search, can_load_more, SEARCH_COST, LOAD_MORE_COST, OPENALEX_URL,
                  RESULTS_PER_SOURCE, format_paper, reconstruct_abstract,
                  search_semantic_scholar, search_arxiv, search_pubmed,
                  sb_patch, sb_post, sb_get, _all_citations,
                  BeautifulSoup)
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
        "email":            user["email"],
        "points_used":      points_used(user),
        "points_remaining": points_remaining(user),
        "total_points":     total_points(user),
        "is_paid":          user.get("is_paid", False),
        "can_search":       can_search(user),
        "can_load_more":    can_load_more(user),
        "search_cost":      SEARCH_COST,
        "load_more_cost":   LOAD_MORE_COST,
    })

# ─── /api/search ─────────────────────────────────────────────────────
@search_bp.route("/api/search")
@login_required
def search():
    query     = request.args.get("q", "").strip()
    page      = int(request.args.get("page", 1))
    year_from = request.args.get("year_from", "")
    year_to   = request.args.get("year_to", "")
    if not query:
        return jsonify({"error": "Please enter a search query"}), 400
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not can_search(user):
        return jsonify({"error": "limit_reached",
                        "message": f"Not enough points. A search costs {SEARCH_COST} pts. You have {points_remaining(user)} left."}), 403

    def fetch_openalex():
        params = {"search": query, "per-page": RESULTS_PER_SOURCE, "page": page, "sort": "cited_by_count:desc"}
        if year_from and year_to:  params["filter"] = f"publication_year:{year_from}-{year_to}"
        elif year_from:            params["filter"] = f"publication_year:{year_from}-"
        elif year_to:              params["filter"] = f"publication_year:-{year_to}"
        try:
            resp = requests.get(OPENALEX_URL, params=params, timeout=15)
            data = resp.json()
            return [format_paper(p) for p in data.get("results", [])], data.get("meta", {}).get("count", 0)
        except Exception:
            return [], 0

    all_results = []
    total_count = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_openalex):                       "openalex",
            executor.submit(search_semantic_scholar, query, page): "semantic",
            executor.submit(search_arxiv, query, page):            "arxiv",
            executor.submit(search_pubmed, query, page):           "pubmed",
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if futures[future] == "openalex":
                    papers, count = result
                    total_count += count
                    all_results.extend(papers)
                else:
                    all_results.extend(result)
            except Exception:
                pass

    seen_dois = set(); seen_titles = set(); deduped = []
    for paper in all_results:
        doi = (paper.get("doi") or "").strip().lower()
        tk  = (paper.get("title") or "").strip().lower()[:60]
        if (doi and doi in seen_dois) or (tk and tk in seen_titles):
            continue
        if doi: seen_dois.add(doi)
        if tk:  seen_titles.add(tk)
        deduped.append(paper)
    deduped.sort(key=lambda x: x.get("citations", 0) or 0, reverse=True)

    new_pts = points_used(user) + SEARCH_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_pts})
    sb_post("search_logs", {"user_id": user["id"], "query": query, "results": len(deduped),
                            "points_used": SEARCH_COST, "searched_at": datetime.now().isoformat()})

    return jsonify({"results": deduped, "total": total_count, "query": query,
                    "points_remaining": max(0, total_points(user) - new_pts),
                    "points_used": SEARCH_COST,
                    "sources_used": ["OpenAlex", "Semantic Scholar", "arXiv", "PubMed"],
                    "ref_disclaimer": "References auto-generated — verify before academic use"})

# ─── /api/load-more ──────────────────────────────────────────────────
@search_bp.route("/api/load-more")
@login_required
def load_more():
    query     = request.args.get("q", "").strip()
    page      = int(request.args.get("page", 2))
    year_from = request.args.get("year_from", "")
    year_to   = request.args.get("year_to", "")
    if not query:
        return jsonify({"error": "Query required"}), 400
    if page < 2:
        return jsonify({"error": "Page must be 2 or higher"}), 400
    user = get_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    if not can_load_more(user):
        return jsonify({"error": "limit_reached",
                        "message": f"Not enough points. Load-more costs {LOAD_MORE_COST} pts. You have {points_remaining(user)} left."}), 403

    def fetch_openalex():
        params = {"search": query, "per-page": RESULTS_PER_SOURCE, "page": page, "sort": "cited_by_count:desc"}
        if year_from and year_to:  params["filter"] = f"publication_year:{year_from}-{year_to}"
        elif year_from:            params["filter"] = f"publication_year:{year_from}-"
        elif year_to:              params["filter"] = f"publication_year:-{year_to}"
        try:
            resp = requests.get(OPENALEX_URL, params=params, timeout=15)
            data = resp.json()
            return [format_paper(p) for p in data.get("results", [])], data.get("meta", {}).get("count", 0)
        except Exception:
            return [], 0

    all_results = []
    total_count = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_openalex):                       "openalex",
            executor.submit(search_semantic_scholar, query, page): "semantic",
            executor.submit(search_arxiv, query, page):            "arxiv",
            executor.submit(search_pubmed, query, page):           "pubmed",
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if futures[future] == "openalex":
                    papers, count = result
                    total_count += count
                    all_results.extend(papers)
                else:
                    all_results.extend(result)
            except Exception:
                pass

    seen_dois = set(); seen_titles = set(); deduped = []
    for paper in all_results:
        doi = (paper.get("doi") or "").strip().lower()
        tk  = (paper.get("title") or "").strip().lower()[:60]
        if (doi and doi in seen_dois) or (tk and tk in seen_titles):
            continue
        if doi: seen_dois.add(doi)
        if tk:  seen_titles.add(tk)
        deduped.append(paper)
    deduped.sort(key=lambda x: x.get("citations", 0) or 0, reverse=True)

    new_pts = points_used(user) + LOAD_MORE_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_pts})
    sb_post("search_logs", {"user_id": user["id"], "query": query, "results": len(deduped),
                            "points_used": LOAD_MORE_COST, "searched_at": datetime.now().isoformat()})

    return jsonify({"results": deduped, "total": total_count, "query": query, "page": page,
                    "points_remaining": max(0, total_points(user) - new_pts),
                    "sources_used": ["OpenAlex", "Semantic Scholar", "arXiv", "PubMed"]})

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
    if not can_load_more(user):
        return jsonify({"error": "limit_reached", "message": "Not enough points for related papers."}), 403
    results = _openalex_related(query)
    new_pts = points_used(user) + LOAD_MORE_COST
    sb_patch("users", f"id=eq.{user['id']}", {"search_count": new_pts})
    return jsonify({"results": results, "points_remaining": max(0, total_points(user) - new_pts)})

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
    import urllib.parse
    data  = request.get_json() or {}
    doi   = (data.get("doi") or "").strip()
    oa    = (data.get("oa_url") or "").strip()
    title = (data.get("title") or "").strip()
    if doi:
        link = doi if doi.startswith("http") else f"https://doi.org/{doi}"; source = "DOI"
    elif oa:
        link = oa; source = "Open Access"
    elif title:
        link = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(title); source = "Google Scholar"
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
    if not oa_url and not openalex_id:
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

    if not text:
        return jsonify({"error": "Could not fetch full text for this paper.", "available": False})

    return jsonify({"text": text, "available": True, "image_notice": image_notice})
