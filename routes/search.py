"""routes/search.py — Search, load-more, paper reader, related, history"""
import os
import re
import requests
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, session, jsonify
from core import (login_required, get_user, OPENALEX_URL, OPENALEX_API_KEY, format_paper, reconstruct_abstract,
                  sb_post, sb_get, sb_patch, sb_try, _all_citations)
from engine import (ALL_SOURCES, SOURCE_LABELS, to_int as _to_int, read_filters, merge_dedupe,
                    apply_filters_and_sort, search_sources, term_in, rank_score, term_weights, sort_date)
from searchhelp import synonym_terms, suggest_query, content_terms, strong_terms, search_query, query_parts, parse_operators, apply_operators
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
    terms = strong_terms(_safe_terms(query))        # little and generic words (the, of, effect) are never 'missing'
    if len(terms) < 2 or f["field"] == "fulltext":     # we cannot check full text ourselves, so no "Missing" labels
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
    w = term_weights(out, terms)             # missing a rare word costs more than missing a common one
    out.sort(key=lambda x: (sum(w.get(t, 1) for t in x["missing"]), -(x.get("citations") or 0)))
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
    words = [w.lower() for w in strong_terms(_safe_terms(query))]      # generic words are not highlighted
    for p in results:
        if p.get("via"):
            words += [w.lower() for w in re.findall(r"[A-Za-z0-9\-]+", p["via"])]
    return [w for w in dict.fromkeys(words) if len(w) > 1]

def _mark_fulltext(results, query, f):
    """Full-text mode: OpenAlex looked inside the paper for your words. When a paper does not show them
    in its title or abstract, say so, so nobody wonders why it matched. We never read or store the text."""
    if f.get("field") != "fulltext":
        return
    terms = strong_terms(_safe_terms(query))
    for p in results:
        if not (p.get("data_source") or "").lower().startswith("openalex"):
            continue
        shown = (p.get("title") or "") + " " + (p.get("abstract") or "")
        if terms and not all(term_in(shown, t) for t in terms):
            p["fulltext_hit"] = True

def _rank(results, query, f, resort=True):
    """Score every paper against ALL the words you typed (generic and bracketed words count a
    little; rare words count more), then order by that score when 'Best match' is selected."""
    parts = query_parts(query)
    terms, optional = parts["all"], parts["optional"]
    if terms:
        main = [t for t in strong_terms(terms) if t not in optional]
        weights = term_weights(results, main)
        for p in results:
            p["score"] = round(rank_score(p, terms, f, weights, optional), 2)
        if resort and f["sort"] == "match":
            results.sort(key=lambda x: (x["score"], x.get("citations") or 0), reverse=True)
    return results

def _run_search_local(query, page, user, log=True, post=None):
    f = read_filters(request.args.get)
    sq = search_query(query)          # words that must match: little words (the, of, vs) and generic words (effect, study) are not required
    raw, total = search_sources(sq, page, f)
    results = _rank(apply_filters_and_sort(merge_dedupe(raw), f, sq), query, f)
    strict_count = len(results)
    _mark_fulltext(results, sq, f)
    relaxed = False
    if page == 1 and len(results) < 10:
        keys = {_key(p) for p in results}
        try:                                                     # 1) same meaning, different word
            syn = _rank(_synonym_matches(sq, page, f, keys), query, f)[:10]
            if syn:
                results = results + syn
        except Exception:
            pass
        try:                                                     # 2) papers with only some of the words
            keys = {_key(p) for p in results}
            extra = _rank(_partial_matches(sq, page, f, keys), query, f, resort=False)[:20 - len(results)]
            if extra:
                results = results + extra
                relaxed = True
        except Exception:
            pass
    if page == 1 and f.get("smart"):
        results = _add_smart(results, query, f, sq)
    if post:                              # operators ("phrase", -word, author:) are checked before abstracts are hidden
        results = post(results)
    hide_restricted_abstracts(results)
    suggestion = ""
    if page == 1 and strict_count < 10:
        try:
            suggestion = suggest_query(query, results)
        except Exception:
            suggestion = ""
    if log:
        _log_search(user, query, len(results), f, request.args.get("tg", ""))
    return {"results": results, "total": max(total or 0, len(results)) if not relaxed else len(results),
            "query": query, "page": page, "relaxed": relaxed,
            "highlight": _highlight_words(query, results),
            "did_you_mean": suggestion,
            "smart_hits": sum(1 for p in results if p.get("smart")),
            "smart_state": f.get("smart_state", ""),
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "ref_disclaimer": "References auto-generated — verify before academic use"}

# ─── /api/search ─────────────────────────────────────────────────────
def _run_search(query, page, user, log=True):
    """Search with Google-Scholar-style operators: "exact phrase", a OR b, -word, intitle:word, author:Name.
    A search without operators goes straight to the normal search."""
    ops = parse_operators(query)
    if not ops["active"]:
        return _run_search_plain(query, page, user, log)
    f = read_filters(request.args.get)
    base = {"query": query, "page": page, "relaxed": False, "highlight": [], "did_you_mean": "",
            "operators": ops["summary"], "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "ref_disclaimer": "References auto-generated — verify before academic use"}
    if not ops["variants"]:
        base.update({"results": [], "total": 0,
                     "operators_note": "Add at least one topic word. author: and -word only narrow a search."})
        return base
    merged, seen, hl, relaxed = [], set(), [], False
    smart_state, smart_hits = "", 0
    for v in ops["variants"]:                                    # OR: one search per alternative (max 3)
        r = _run_search_plain(v, page, user, log=False, post=lambda rs: apply_operators(rs, ops))
        relaxed = relaxed or r["relaxed"]
        smart_state = r.get("smart_state") or smart_state
        smart_hits += r.get("smart_hits") or 0
        hl += [w for w in r["highlight"] if w not in hl]
        for p in r["results"]:
            k = _key(p)
            if k and k in seen:
                continue
            seen.add(k); merged.append(p)
    full = [p for p in merged if not p.get("missing")]
    part = [p for p in merged if p.get("missing")]
    if f["sort"] == "recent":   full.sort(key=lambda x: (sort_date(x), x.get("citations") or 0), reverse=True)
    elif f["sort"] == "cited":  full.sort(key=lambda x: x.get("citations") or 0, reverse=True)
    else:                       full.sort(key=lambda x: (x.get("score") or 0, x.get("citations") or 0), reverse=True)
    part.sort(key=lambda x: len(x["missing"]))
    results = full + part
    if log:
        _log_search(user, query, len(results), f, request.args.get("tg", ""))
    base.update({"results": results, "total": len(results), "relaxed": relaxed, "highlight": hl,
                 "smart_state": smart_state, "smart_hits": smart_hits})
    return base

# ─── Optional private search service ────────────────────────────────
# If SEARCH_API_URL and SEARCH_API_KEY are set, plain searches are done by your second (private)
# website. If they are not set, or it does not answer in time, the search runs here as before.
SEARCH_API_URL = os.environ.get("SEARCH_API_URL", "").rstrip("/")
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "")

def _run_search_remote(query, page):
    args = {k: v for k, v in request.args.items() if k not in ("tg",)}
    args["q"], args["page"] = query, page
    r = requests.get(SEARCH_API_URL + "/v1/search", params=args, timeout=25,
                     headers={"X-Api-Key": SEARCH_API_KEY})
    data = r.json() if r.ok else None
    return data if isinstance(data, dict) and isinstance(data.get("results"), list) else None

def _run_search_plain(query, page, user, log=True, post=None):
    data, why = None, "private service not set up"
    if SEARCH_API_URL and SEARCH_API_KEY and post is None:
        why = "private service did not give a usable answer (wrong key, asleep, or error)"
        try:
            data = _run_search_remote(query, page)
        except Exception:
            data = None
    elif post is not None:
        why = "search with operators runs on this site"
    if data is None:
        out = _run_search_local(query, page, user, log, post)
        out["engine"] = "this site (" + why + ")"
        return out
    data["engine"] = "private service"
    if log:
        _log_search(user, query, len(data["results"]), read_filters(request.args.get), request.args.get("tg", ""))
    return data

# ─── Optional "smart" search: OpenAlex's own semantic (meaning-based) search ─────────────
# OFF unless SMART_SEARCH=1 is set (only on the private service). It only finds and orders REAL
# papers from OpenAlex; it never writes text. It stops by itself after SMART_DAILY_LIMIT searches
# a day (default 300) to protect the free allowance, and any error falls back to the normal rules.
SMART_ON = os.environ.get("SMART_SEARCH", "1") == "1"
try:
    SMART_DAILY_LIMIT = int(os.environ.get("SMART_DAILY_LIMIT", "80"))
except ValueError:
    SMART_DAILY_LIMIT = 80
_smart_day = {"day": None, "n": 0}

def _semantic_matches(query, f):
    """Up to 50 papers whose meaning is closest to the search, in OpenAlex's order. [] if off/failed."""
    if not f.get("smart"):                      # the Smart search switch on the page
        return []
    if not SMART_ON:
        f["smart_state"] = "unavailable"; return []
    if f.get("field") == "fulltext":
        f["smart_state"] = "fulltext"; return []
    today = datetime.now().date().isoformat()
    if _smart_day["day"] != today:
        _smart_day["day"], _smart_day["n"] = today, 0
    if _smart_day["n"] >= SMART_DAILY_LIMIT:
        f["smart_state"] = "limit"; return []
    _smart_day["n"] += 1
    base = []
    if f["year_from"]: base.append("from_publication_date:%s-01-01" % f["year_from"])
    if f["year_to"]:   base.append("to_publication_date:%s-12-31" % f["year_to"])
    if f["oa_only"]:   base.append("is_oa:true")
    params = {"search.semantic": query[:2000], "per-page": 50}
    if base:
        params["filter"] = ",".join(base)
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    try:
        resp = requests.get(OPENALEX_URL, params=params, timeout=15)
        if not resp.ok:
            f["smart_state"] = "error"; return []
        out = [format_paper(p) for p in resp.json().get("results", [])]
        f["smart_state"] = "ok" if out else "none"
        return out
    except Exception:
        f["smart_state"] = "error"; return []

def _smart_diag(query):
    """What would happen if smart search ran now? Shows the real answer from OpenAlex. No secrets."""
    out = {"smart_search_switch_on": SMART_ON, "openalex_key_set": bool(OPENALEX_API_KEY),
           "searches_today": _smart_day["n"], "daily_limit": SMART_DAILY_LIMIT}
    params = {"search.semantic": query[:2000], "per-page": 5}
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
    try:
        resp = requests.get(OPENALEX_URL, params=params, timeout=15)
        out["openalex_http_status"] = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.ok:
            out["papers_returned"] = len(body.get("results", []))
            out["first_titles"] = [(p.get("title") or p.get("display_name") or "")[:80] for p in body.get("results", [])[:3]]
        else:
            out["openalex_message"] = str(body.get("message") or body.get("error") or resp.text)[:300]
    except Exception as e:
        out["error"] = type(e).__name__ + ": " + str(e)[:150]
    return out

def _add_smart(results, query, f, sq):
    """Papers the rules AND the meaning search both found get a bonus; a few papers only the meaning
    search found are added, marked 'similar meaning'. Returns the new list."""
    sem = _semantic_matches(query, f)
    if not sem:
        return results
    f2 = dict(f); f2["field"] = "both"
    sem = apply_filters_and_sort(merge_dedupe(sem), f2, sq)      # keeps the filters (years, open access)
    order = {}
    raw_rank = 0
    for p in sem:
        order[_key(p)] = raw_rank; raw_rank += 1
    have = {_key(p) for p in results}
    for p in results:
        i = order.get(_key(p))
        if i is not None:
            p["score"] = round((p.get("score") or 0) + 3 * (1 - i / max(1, len(sem))), 2)
            p["smart"] = True
    extra = []
    for p in sem[:15]:
        if _key(p) not in have:
            p["via"] = "similar meaning (OpenAlex AI search)"
            p["smart_only"] = True
            p["score"] = round((p.get("score") or 0) + 3 * (1 - order[_key(p)] / max(1, len(sem))), 2)
            p["smart"] = True
            extra.append(p)
    results = results + extra
    if f["sort"] == "match":
        results.sort(key=lambda x: (x.get("score") or 0, x.get("citations") or 0), reverse=True)
    return results

@search_bp.route("/api/smart-status")
@login_required
def smart_status():
    """Open /api/smart-status in your browser while logged in (guest is fine) to see why smart search is or is not working."""
    q = request.args.get("q", "tomato fertilizer growth")[:200]
    out = {"private_service_set": bool(SEARCH_API_URL and SEARCH_API_KEY)}
    if SEARCH_API_URL and SEARCH_API_KEY:
        try:
            r = requests.get(SEARCH_API_URL + "/v1/smart-check", params={"q": q}, timeout=45,
                             headers={"X-Api-Key": SEARCH_API_KEY})
            out["service_http_status"] = r.status_code
            try:
                out["service"] = r.json()
            except Exception:
                out["service"] = r.text[:200]
        except Exception as e:
            out["service_error"] = type(e).__name__
    else:
        out["this_site"] = _smart_diag(q)
    return jsonify(out)

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
    if OPENALEX_API_KEY:
        params["api_key"] = OPENALEX_API_KEY
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
