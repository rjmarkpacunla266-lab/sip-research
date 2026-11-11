"""routes/search.py — Search, load-more, paper reader, related, history"""
import re
import requests
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, session, jsonify
from core import (login_required, get_user, OPENALEX_URL, format_paper, reconstruct_abstract,
                  sb_post, sb_get, sb_patch, sb_try, _all_citations)
from engine import (ALL_SOURCES, SOURCE_LABELS, to_int as _to_int, read_filters, merge_dedupe,
                    apply_filters_and_sort, search_sources, term_in)
from searchhelp import synonym_terms, suggest_query, content_terms
from core import _safe_terms, hide_restricted_abstracts
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

def _q(value):
    """URL-encode a value for a PostgREST filter."""
    return urllib.parse.quote(str(value), safe="")

def _log_search(user, query, count, f, tags_raw):
    """Save one history row. The filters are stored too, so 'Search again' restores them.
    If the optional `filters` column doesn't exist yet, fall back to saving without it."""
    tags = [x.strip()[:80] for x in (tags_raw or "").split("|") if x.strip()][:10]
    every_source = set(f["sources"]) == set(ALL_SOURCES)
    row = {"user_id": user["id"], "query": query[:300], "results": count,
           "searched_at": datetime.now().isoformat(),
           "filters": {"tags": tags, "sources": [] if every_source else f["sources"],
                       "year_from": f["year_from"], "year_to": f["year_to"],
                       "oa": f["oa_only"], "sort": f["sort"], "field": f["field"]}}
    ok, _, _ = sb_try("POST", "search_logs", data=row)
    if not ok:
        row.pop("filters")
        sb_try("POST", "search_logs", data=row)

def _hit(p, term, field):
    t, a = p.get("title") or "", p.get("abstract") or ""
    if field == "title":    return term_in(t, term)
    if field == "abstract": return term_in(a, term)
    return term_in(t, term) or term_in(a, term)

def _partial_matches(query, page, f, have_keys):
    """Google-style fallback: papers that contain SOME of the words, each tagged with
    the words it is missing. Rule-based only."""
    terms = content_terms(_safe_terms(query))       # little words (the, of, vs) are never 'missing'
    if len(terms) < 2:
        return []
    f2 = dict(f); f2["field"] = "both"      # let the filters pass; we check the field ourselves
    raw, _ = search_sources(query, page, f, loose=True)
    out = []
    for p in apply_filters_and_sort(merge_dedupe(raw), f2, query):
        key = (p.get("doi") or p.get("title") or "").lower()
        if key in have_keys:
            continue
        miss = [t for t in terms if not _hit(p, t, f["field"])]
        if not miss or len(miss) == len(terms):
            continue
        p["missing"] = miss
        out.append(p)
    out.sort(key=lambda x: (len(x["missing"]), -(x.get("citations") or 0)))
    return out

def _key(p):
    return (p.get("doi") or p.get("title") or "").lower()

def _synonym_matches(query, page, f, have_keys):
    """Papers that use a synonym instead of one of your words (student -> learner, ...).
    Each is tagged with the synonym that matched. arXiv is skipped here because it limits
    us to one request every 3 seconds."""
    terms = _safe_terms(query)
    variants = []
    for t in terms:
        for s in synonym_terms(t)[:1]:
            variants.append((s, " ".join(s if x == t else x for x in terms)))
    f2 = dict(f); f2["sources"] = [s for s in f["sources"] if s != "arxiv"]
    if not variants or not f2["sources"]:
        return []
    out = []
    for syn, q2 in variants[:2]:
        raw, _ = search_sources(q2, page, f2)
        for p in apply_filters_and_sort(merge_dedupe(raw), f2, q2):
            k = _key(p)
            if not k or k in have_keys:
                continue
            have_keys.add(k)
            p["via"] = syn
            out.append(p)
    out.sort(key=lambda x: -(x.get("score") or 0))
    return out

def _highlight_words(query, results):
    """Words the page should highlight: your search words plus any synonym that matched."""
    words = [w.lower() for w in content_terms(_safe_terms(query))]
    for p in results:
        if p.get("via"):
            words += [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", p["via"])]
    return [w for w in dict.fromkeys(words) if len(w) > 1]

def _run_search(query, page, user, log=True):
    f = read_filters(request.args.get)
    raw, total = search_sources(query, page, f)
    results = apply_filters_and_sort(merge_dedupe(raw), f, query)
    strict_count = len(results)
    relaxed = False
    if page == 1 and len(results) < 10:
        keys = {_key(p) for p in results}
        try:                                                     # 1) same meaning, different word
            syn = _synonym_matches(query, page, f, keys)[:10]
            if syn:
                results = results + syn
        except Exception:
            pass
        try:                                                     # 2) papers with only some of the words
            keys = {_key(p) for p in results}
            extra = _partial_matches(query, page, f, keys)[:20 - len(results)]
            if extra:
                results = results + extra
                relaxed = True
        except Exception:
            pass
    hide_restricted_abstracts(results)
    suggestion = ""
    if page == 1 and strict_count < 10:
        try:
            suggestion = suggest_query(query, results)
        except Exception:
            suggestion = ""
    if log:
        _log_search(user, query, len(results), f, request.args.get("tg", ""))
    return {"results": results, "total": (total or len(results)) if not relaxed else len(results),
            "query": query, "page": page, "relaxed": relaxed,
            "highlight": _highlight_words(query, results),
            "did_you_mean": suggestion,
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
    ok, rows, err = sb_try("GET", "search_logs",
                           f"user_id=eq.{_q(session['user_id'])}&order=searched_at.desc&limit=300")
    if not ok:
        return jsonify({"error": "Could not load search history", "detail": err}), 500
    return jsonify(rows)

@search_bp.route("/api/history", methods=["DELETE"])
@login_required
def clear_history():
    ok, _, err = sb_try("DELETE", "search_logs", f"user_id=eq.{_q(session['user_id'])}")
    if not ok:
        return jsonify({"error": "Could not clear history", "detail": err}), 500
    return jsonify({"success": True})

@search_bp.route("/api/history/delete", methods=["POST"])
@login_required
def delete_history_items():
    data = request.get_json(silent=True) or {}
    ids  = [str(i) for i in (data.get("ids") or [])][:200]
    ids  = [i for i in ids if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", i)]
    if not ids:
        return jsonify({"error": "No valid ids"}), 400
    ok, _, err = sb_try("DELETE", "search_logs",
                        f"id=in.({','.join(ids)})&user_id=eq.{_q(session['user_id'])}")
    if not ok:
        return jsonify({"error": "Could not delete", "detail": err}), 500
    return jsonify({"success": True})

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
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data.get("title"):
        return jsonify({"error": "Title is required"}), 400
    return jsonify(_all_citations(
        data.get("authors"), data.get("year"), str(data.get("title"))[:600],
        str(data.get("journal") or "")[:300], data.get("volume"), data.get("issue"),
        data.get("pages"), data.get("doi"), str(data.get("oa_url") or "")[:500]))

# ─── /api/citations/bulk ─────────────────────────────────────────────
@search_bp.route("/api/citations/bulk", methods=["POST"])
@login_required
def citations_bulk():
    data   = request.get_json(silent=True) or {}
    papers = data.get("papers")
    if not isinstance(papers, list) or not papers:
        return jsonify({"error": "papers list required"}), 400
    out = []
    for p in papers[:300]:
        try:
            if not isinstance(p, dict):
                raise ValueError("bad paper")
            authors = [str(a)[:120] for a in (p.get("authors") or []) if a][:30] if isinstance(p.get("authors"), list) else []
            out.append(_all_citations(authors, str(p.get("year") or "n.d."), str(p.get("title") or ""),
                                      str(p.get("journal") or ""), str(p.get("volume") or ""),
                                      str(p.get("issue") or ""), str(p.get("pages") or ""),
                                      str(p.get("doi") or ""), str(p.get("oa_url") or "")[:500]))
        except Exception:
            out.append({})        # the page falls back to its own formatting for this one
    return jsonify({"citations": out})

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
def paper_page():
    # The in-site reader was removed: papers now open on the original publisher / source page.
    from flask import redirect
    return redirect("/search")
