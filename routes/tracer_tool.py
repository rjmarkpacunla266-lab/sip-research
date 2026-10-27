"""routes/tracer_tool.py: Source Tracer (/source-tracer and /api/source-tracer*). Edit this file for source-tracer changes only."""
import re as _re
import difflib
import math
import time
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
import requests
import xml.etree.ElementTree as ET
from flask import render_template, request, jsonify
from core import login_required, _safe_terms, OPENALEX_URL, format_paper, hide_restricted_abstracts
from engine import (read_filters, search_papers, apply_filters_and_sort, term_in, relevance,
                    sort_date, SOURCE_LABELS, search_sources, merge_dedupe, to_int)
import topicfinder
from routes.tools_common import tools_bp, _fail, _pick, _clean_abstract, _pubmed_abstracts, _client_paper, _DOI_OK


# ═════════════════════════════════════════════════════════════════════
# SOURCE TRACER
# ═════════════════════════════════════════════════════════════════════
_STOPWORDS = {
    'the','a','an','is','are','was','were','be','been','being','have','has','had','do','does','did',
    'will','would','could','should','may','might','shall','can','need','dare','ought','used','of','in',
    'on','at','to','for','with','by','from','and','or','but','if','as','that','this','it','its','their',
    'they','we','our','you','your','he','she','his','her','not','because','between','these','those',
    'which','while','there','about','where','when','also','such','than','then','into','over','under',
    'very','more','most','some','each','both','only','must','what','who','whom','whose','why','how',
    'any','all','one','two','been','just','even','much','many','like','make','made','must','upon'}

def _clean_text(text):
    text = (text or "").lower().strip()
    text = _re.sub(r'[^\w\s]', ' ', text)
    return _re.sub(r'\s+', ' ', text)

_GENERIC = {'different','caused','informs','including','however','study','studies','results','result','using','based',
            'related','associated','among','several','within','across','through','analysis','research','effects','effect',
            'paper','data','high','level','levels','rate','rates','increase','increased','number','important','significant',
            'review','shown','showed','found','find','finds','suggest','suggests','suggested','provide','provides','number',
            'between','during','after','before','whether','therefore','thus','since','through','following','known','often'}

def _extract_keywords(text):
    """Up to 8 distinctive words of the quote. Generic academic words ('different', 'caused'...) are dropped first;
    the longest (rarest) words are kept, then put back in quote order."""
    words = []
    for w in _re.findall(r'\b\w{4,}\b', (text or "").lower()):
        if w not in _STOPWORDS and w not in words:
            words.append(w)
    good = [w for w in words if w not in _GENERIC]
    if len(good) < 3:
        good = words
    keep = set(sorted(good, key=len, reverse=True)[:8])
    return [w for w in good if w in keep]

def _best_match(abstract, quote):
    """Best similarity between the quote and any 1–2 sentence window of the abstract."""
    sents = [s.strip() for s in _re.split(r"(?<=[.!?])\s+", abstract or "") if s.strip()]
    if not sents:
        return 0.0, ""
    q = _clean_text(quote)
    windows = sents + [sents[i] + " " + sents[i + 1] for i in range(len(sents) - 1)]
    best, best_s = 0.0, ""
    for w in windows:
        r = difflib.SequenceMatcher(None, q, _clean_text(w)).ratio()
        if r > best:
            best, best_s = r, w
    return best, best_s

def _pkey(p):
    doi = (p.get("doi") or "").strip().lower().replace("https://doi.org/", "")
    return doi or (p.get("title") or "").strip().lower()[:60]

SEARCH_DEADLINE = 35.0        # seconds: the tracer uses whatever the sources have answered by then (a slow source cannot stall it)
TRACE_TOTAL     = 100.0       # seconds: the whole full trace stops here (the page allows 2 minutes, gunicorn --timeout 120)

def _content_words(quote, limit=28):
    out = []
    for w in _clean_text(quote).split():
        if len(w) >= 3 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out[:limit]

def _trace_pool(quote, keywords, f):
    """Find candidate papers with three searches run at the same time. ALL of them use the sources' RELEVANCE mode
    ("loose": OpenAlex/Crossref/Europe PMC/PubMed/arXiv rank by how well the words match). Search Papers uses the strict
    mode, which needs every word of the quote in one paper and falls back to 'most cited first'. That is right for a topic,
    but for a long quote it returns famous papers instead of the paper that contains the sentence.
       1. the quote's content words (filler words removed),
       2. the same with 'Open Access only' switched on (open-access papers can be read in full text),
       3. the 6 most distinctive words, as a safety net.
    A paper near the top of several searches gets a rank bonus. Returns (papers, {paper key: rank bonus 0..1})."""
    f = dict(f, sort="match")                              # the pool is always relevance-ordered; the page's sort is applied later
    q1 = " ".join(_content_words(quote)) or " ".join(keywords)
    jobs = [(q1, 30, f)]
    if not f["oa_only"]:
        jobs.append((q1, 30, dict(f, oa_only=True)))
    kq = " ".join(keywords[:6]).strip()
    if kq and kq != q1:
        jobs.append((kq, 20, f))
    def run(job):
        q, per, fl = job
        return search_sources(q, 1, fl, per_source=per, loose=True)[0]
    ex = ThreadPoolExecutor(max_workers=len(jobs))
    futs = [ex.submit(run, j) for j in jobs]
    done, _pending = wait(futs, timeout=SEARCH_DEADLINE)
    ex.shutdown(wait=False, cancel_futures=True)           # do not wait for slow sources
    batches = []
    for fu in futs:
        if fu in done:
            try:
                batches.append(fu.result())
            except Exception:
                traceback.print_exc()
    rank, allraw = {}, []
    for raw in batches:
        pos = {}
        for p in raw:
            k, src = _pkey(p), (p.get("data_source") or "")
            i = pos.get(src, 0); pos[src] = i + 1
            rank[k] = rank.get(k, 0.0) + 1.0 / (1 + i)   # 1st result of a source = 1, 2nd = 0.5, 3rd = 0.33 ...
        allraw.extend(raw)
    papers = hide_restricted_abstracts(apply_filters_and_sort(merge_dedupe(allraw), dict(f, sort="cited"), " ".join(keywords)))
    return papers, {k: min(1.0, v / 1.5) for k, v in rank.items()}

def _title_overlap(title, quote):
    tw = [w for w in _re.findall(r"\b\w{4,}\b", (title or "").lower()) if w not in _STOPWORDS]
    return sum(1 for w in tw if term_in(quote, w)) / len(tw) if tw else 0.0

def _score_paper(paper, quote, keywords, rank_bonus=0.0):
    """-> (score 0..1, best sentence, wording similarity, share of keywords found in title/abstract, rank bonus, title overlap)."""
    title, abstract = paper.get("title") or "", paper.get("abstract") or ""
    sim, sentence = _best_match(abstract, quote) if abstract else (0.0, "")
    text = title + " " + abstract
    cov = sum(1 for k in keywords if term_in(text, k)) / max(len(keywords), 1)
    tov = _title_overlap(title, quote)
    score = 0.30 * sim + 0.30 * cov + 0.10 * tov + 0.30 * rank_bonus
    return score, sentence, sim, cov, rank_bonus, tov

def _confidence(score, sim, cov, rb=0.0):
    """A match only counts as 'found' when the words really overlap (or several searches agree), not just by chance."""
    if (score >= 0.7 and sim >= 0.6) or (score >= 0.65 and rb >= 0.9 and cov >= 0.6) or (sim >= 0.8 and cov >= 0.5):
        return "high", True
    if score >= 0.45 and (cov >= 0.5 or rb >= 0.7):
        return "medium", True
    return ("low", False)

# ── Looking for the quote INSIDE the paper (open-access full text), not only in the abstract ──
# A quote copied from an article usually comes from its body. The abstract rarely repeats it, so abstract-only matching
# can only guess from related words. For the best candidates that are open access (Europe PMC / PubMed Central hold the
# full text), the quote is compared with every sentence of the paper.
TRACE_FT_MAX = 15
_TR_SKIP = _re.compile(r"reference|acknowledg|funding|conflict|competing|contribution|supplement|abbreviation|availability", _re.I)

def _ft_best_match(ft, quote, keywords):
    """-> (similarity 0..1, best passage, section label, share of keywords in that passage) over the paper's sections.
    Fast on purpose: long papers have thousands of sentences, so each sentence is lower-cased ONCE and checked for the
    keywords with plain substring tests; only the few best passages go through the slower similarity comparison."""
    q = _clean_text(quote)
    stems = [k[:-1] if k.endswith("s") and len(k) > 4 else k for k in keywords]      # 'risks' also finds 'risk'
    need = max(2, math.ceil(0.5 * len(stems)))
    wins = []                                                  # (keyword hits, passage, section label)
    for path, text in (ft.get("sections") or {}).items():
        if _TR_SKIP.search(path):
            continue
        sents = [x.strip() for x in _re.split(r"(?<=[.!?])\s+", _clean_abstract(text)) if len(x.split()) >= 4]
        label = (_re.sub(r"^[\dIVXivx]+(?:\.\d+)*[.)]?\s+", "", path.split(">")[0].strip()) or path)[:60]
        low = [x.lower() for x in sents]
        found = [frozenset(i for i, st in enumerate(stems) if st in lw) for lw in low]
        for i in range(len(sents)):
            if not found[i]:
                continue                                       # every useful passage starts with a sentence that has a keyword
            acc = set()
            for n in (1, 2, 3):                                # a sentence, two or three in a row (quotes can span sentences)
                if i + n > len(sents):
                    break
                acc |= found[i + n - 1]
                if len(acc) >= need:
                    wins.append((len(acc), " ".join(sents[i:i + n]), label))
    wins.sort(key=lambda x: (x[0], -abs(len(x[1]) - len(quote))), reverse=True)
    best = (0.0, "", "", 0.0)
    for hits, w, label in wins[:40]:                           # compare the quote only with the most promising passages
        sm = difflib.SequenceMatcher(None, q, _clean_text(w))
        if sm.real_quick_ratio() < best[0] or sm.quick_ratio() < best[0]:
            continue
        r = sm.ratio()
        if r > best[0]:
            best = (r, w, label, hits / max(len(keywords), 1))
    return best

def _full_text_pass(scored, quote, keywords, budget=8.0):
    """Re-score the best open-access candidates using their full text. Changes rows in place; -> (tried, read).
    Never raises: if anything goes wrong the abstract-based result is simply kept."""
    return 0, 0   # full-text reading is paused together with Europe PMC
    try:
        pool = [r for r in scored if r.get("doi") and r.get("is_oa")][:TRACE_FT_MAX]
        if not pool or budget < 2:
            return 0, 0
        reads = topicfinder.fetch_fulltexts(pool, budget=budget, max_papers=len(pool))
        for row, ft in reads:
            sim, passage, label, cov = _ft_best_match(ft, quote, keywords)
            if sim > row["sim"] and passage:
                row["sim"], row["cov"] = round(sim, 3), round(max(row["cov"], cov), 3)
                row["snippet"] = passage[:400] + ("\u2026" if len(passage) > 400 else "")
                row["match_in"], row["section"] = "full text", label
                row["score"] = round(0.30 * row["sim"] + 0.30 * row["cov"] + 0.10 * row.get("tov", 0) + 0.30 * row["rb"], 4)
        return len(pool), len(reads)
    except Exception:
        traceback.print_exc()
        return 0, 0

def _score_rows(papers, ranks, quote, keywords):
    """Score papers against the quote (abstract only) -> rows sorted best first."""
    scored = []
    for p in papers:
        score, sentence, sim, cov, rb, tov = _score_paper(p, quote, keywords, ranks.get(_pkey(p), 0.0))
        row = _pick(p, abstract_chars=300)
        row["score"] = round(score, 4)
        row["sim"], row["cov"], row["rb"], row["tov"] = round(sim, 3), round(cov, 3), round(rb, 3), round(tov, 3)
        row["match_in"] = "abstract" if sim >= 0.25 and sentence else ""
        row["snippet"] = sentence if sim >= 0.25 and sentence else row.get("abstract", "")
        scored.append(row)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored

def _trace_response(mode, quote, keywords, f, scored, n_papers, ft_tried=0, ft_read=0):
    """Same answer format for the quick and the full trace."""
    top_score = scored[0]["score"] if scored else 0
    if scored:
        confidence, found = _confidence(top_score, scored[0]["sim"], scored[0]["cov"], scored[0]["rb"])
        scored[0]["is_best"] = found
    else:
        confidence, found = "none", False
    shown = scored[:8]
    if f["sort"] != "match":
        relevant = [r for r in scored if r["score"] >= max(0.15, top_score * 0.4)]
        shown = (relevant if len(relevant) >= 3 else scored[:5])
        if f["sort"] == "cited":
            shown = sorted(shown, key=lambda r: (r["citations"], r["score"]), reverse=True)[:8]
        else:
            shown = sorted(shown, key=lambda r: (sort_date(r), r["score"]), reverse=True)[:8]
    message = ""
    if not found:
        message = ("No exact source found. Possible reasons: the source may not be indexed in OpenAlex, "
                   "arXiv, PubMed, Crossref or Europe PMC, the quote may be paraphrased, or your filters "
                   "may be hiding it. Only open-access papers can be checked inside their full text"
                   + (f" ({ft_read} of the top {ft_tried} were readable)" if ft_tried else "")
                   + ". Related papers are shown below.")
    return {
        "mode": mode, "quote": quote, "keywords": keywords, "found": found, "confidence": confidence,
        "confidence_pct": int(top_score * 100), "results": shown, "message": message,
        "searched": n_papers, "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
        "fulltext_tried": ft_tried, "fulltext_read": ft_read,
        # the quick trace is only trusted when the wording matches closely; anything weaker keeps the full trace running
        "confident": bool(found and confidence == "high"),
    }

def _trace_input():
    """-> (quote, keywords, filters, error response or None)"""
    data  = request.get_json(silent=True) or {}
    quote = str(data.get("quote") or "").strip()[:2000]
    if not quote:
        return None, None, None, (jsonify({"error": "Quote is required"}), 400)
    if len(quote) < 10:
        return None, None, None, (jsonify({"error": "Quote is too short. Please enter at least 10 characters."}), 400)
    keywords = _extract_keywords(quote)
    if not keywords:
        return None, None, None, (jsonify({"error": "Could not extract keywords from the quote."}), 400)
    f = read_filters(data.get)
    if data.get("sort") not in ("match", "cited", "recent"):
        f["sort"] = "match"
    return quote, keywords, f, None

def _error_response(mode, quote, keywords, e):
    traceback.print_exc()
    # a normal answer (not a 500) so the page shows the reason instead of a blank "Something went wrong"
    return jsonify({"mode": mode, "quote": quote, "keywords": keywords, "found": False, "confident": False,
                    "confidence": "none", "confidence_pct": 0, "results": [], "searched": 0, "sources_used": [],
                    "message": "Source tracing hit an error: " + type(e).__name__ + ": " + str(e)[:200]})

QUICK_DEADLINE = 15.0         # seconds the quick trace may take
QUICK_SCORE_MAX = 40          # only the best-looking papers get the slower wording comparison

@tools_bp.route("/api/source-tracer/quick", methods=["POST"])
@login_required
def source_tracer_quick():
    """Quick Trace = the Search Papers search (strict mode, Open Access ON) shown inside the Source Tracer.
    Fast but less reliable: it only looks at titles and abstracts and needs the quote's words to appear together."""
    quote, keywords, f, err = _trace_input()
    if err:
        return err
    try:
        fq = dict(f, oa_only=True)                                  # the Open Access filter is always on, like the index search
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(search_papers, quote, 1, fq)
        try:
            papers = fut.result(timeout=QUICK_DEADLINE)[0]
        except Exception:
            papers = []
        ex.shutdown(wait=False, cancel_futures=True)
        # cheap keyword check first so the slower wording comparison runs on 40 papers, not hundreds
        papers = sorted(papers, key=lambda p: sum(1 for k in keywords if term_in((p.get("title") or "") + " " + (p.get("abstract") or ""), k)),
                        reverse=True)[:QUICK_SCORE_MAX]
        scored = _score_rows(papers, {}, quote, keywords)
        return jsonify(_trace_response("quick", quote, keywords, fq, scored, len(papers)))
    except Exception as e:
        return _error_response("quick", quote, keywords, e)

@tools_bp.route("/api/source-tracer", methods=["POST"])
@login_required
def source_tracer():
    """Full trace (up to ~100 s). Params (JSON): quote + shared filters (sources, year_from, year_to, oa, sort = match | cited | recent)."""
    quote, keywords, f, err = _trace_input()
    if err:
        return err
    t0 = time.monotonic()
    try:
        papers, ranks = _trace_pool(quote, keywords, f)
        scored = _score_rows(papers, ranks, quote, keywords)
        ft_tried = ft_read = 0
        if scored and scored[0]["sim"] < 0.85:               # an almost word-for-word abstract match needs no more checking
            ft_tried, ft_read = _full_text_pass(scored, quote, keywords, budget=min(40.0, TRACE_TOTAL - (time.monotonic() - t0)))
            scored.sort(key=lambda r: r["score"], reverse=True)
        return jsonify(_trace_response("full", quote, keywords, f, scored, len(papers), ft_tried, ft_read))
    except Exception as e:
        return _error_response("full", quote, keywords, e)

@tools_bp.route("/source-tracer")
@login_required
def source_tracer_page():
    return render_template("source_tracer.html")
