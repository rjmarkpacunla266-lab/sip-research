"""routes/search.py — Search, load-more, paper reader, related, history"""
import re
import requests
import unicodedata
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, session, jsonify
from core import (login_required, get_user, OPENALEX_URL,
                  RESULTS_PER_SOURCE, format_paper, reconstruct_abstract,
                  search_arxiv, search_pubmed, search_crossref, search_europe_pmc, search_openalex, _safe_terms,
                  sb_post, sb_get, sb_patch, sb_try, _all_citations)
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

def _q(value):
    """URL-encode a value for a PostgREST filter."""
    return urllib.parse.quote(str(value), safe="")

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
        "field":     request.args.get("field", "both") if request.args.get("field") in ("title", "abstract", "both") else "both",
    }

def _fetch_openalex(query, page, f):
    return search_openalex(query, page, RESULTS_PER_SOURCE, year_from=f["year_from"],
                           year_to=f["year_to"], oa_only=f["oa_only"], sort=f["sort"], field=f["field"])

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

def _norm(text):
    """Lowercase and strip accents (é -> e)."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()

def _compact(text):
    """Letters+digits only (spaces, - _ / . etc. removed), plus a flag per character
    saying whether it begins a word in the original text."""
    chars, starts, new_word = [], [], True
    for c in _norm(text):
        if c.isalnum():
            chars.append(c); starts.append(new_word); new_word = False
        else:
            new_word = True
    return "".join(chars), starts

def _term_in(text, term):
    """Is the word in the text? Handles plurals, accents and spelling variants such as
    'bio-fuel', 'bio fuel', 'bio_fuel' for 'biofuel'. A compact match must begin at the
    start of a word, so 'heart' does not match 'the art'."""
    t = _norm(term)
    if t and t in _norm(text):
        return True
    ct = "".join(c for c in t if c.isalnum())
    if not ct:
        return False
    comp, starts = _compact(text)
    i = comp.find(ct)
    while i != -1:
        if starts[i]:
            return True
        i = comp.find(ct, i + 1)
    return False

def _matches_field(p, terms, field):
    """Check that every search word really sits in the chosen part of the paper."""
    title    = p.get("title") or ""
    abstract = p.get("abstract") or ""
    if field == "title":
        return all(_term_in(title, t) for t in terms)
    if abstract:
        if field == "abstract":
            return all(_term_in(abstract, t) for t in terms)
        return all(_term_in(title, t) or _term_in(abstract, t) for t in terms)
    if "pubmed" in (p.get("data_source") or "").lower():
        return True                    # PubMed matched in its own index; it sends no abstract
    return field == "both" and all(_term_in(title, t) for t in terms)

def _apply_filters_and_sort(papers, f, query=""):
    """Safety net: sources that ignore year/OA/field options are filtered here."""
    terms = _safe_terms(query)
    out = []
    for p in papers:
        if f.get("field") in ("title", "abstract") and terms and not _matches_field(p, terms, f["field"]):
            continue
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

def _run_search(query, page, user, log=True):
    f = _read_filters()
    jobs = {}
    kw   = dict(year_from=f["year_from"], year_to=f["year_to"], oa_only=f["oa_only"], sort=f["sort"], field=f["field"])
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
    results = _apply_filters_and_sort(_merge_dedupe(raw), f, query)
    if log:
        _log_search(user, query, len(results), f, request.args.get("tg", ""))
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
    data = request.get_json() or {}
    if not data.get("title"):
        return jsonify({"error": "Title is required"}), 400
    return jsonify(_all_citations(
        data.get("authors") or [], data.get("year") or "n.d.",
        data.get("title") or "", data.get("journal") or "",
        data.get("volume") or "", data.get("issue") or "",
        data.get("pages") or "", data.get("doi") or ""))

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
                                      str(p.get("doi") or "")))
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
