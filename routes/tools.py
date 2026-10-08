"""routes/tools.py — Answer Finder, Source Tracer, Topic Finder.
All three use the shared engine (engine.py), so they support the same filters as Search Papers:
sources, years, Open Access and sort."""
import re as _re
import difflib
import traceback
from flask import Blueprint, render_template, request, jsonify
from core import login_required, _safe_terms
from engine import (read_filters, search_papers, apply_filters_and_sort, term_in, relevance,
                    sort_date, SOURCE_LABELS)

tools_bp = Blueprint("tools", __name__)


def _fail(what):
    traceback.print_exc()
    return jsonify({"error": f"{what} failed. Please try again."}), 500

def _pick(p, abstract_chars=0):
    """The fields a tool page needs from a paper (keeps responses small)."""
    out = {k: p.get(k) for k in ("title", "authors", "year", "date", "journal", "doi", "oa_url",
                                 "is_oa", "citations", "data_source", "apa_reference", "openalex_id")}
    out["authors"] = (out.get("authors") or [])[:6]
    out["citations"] = out.get("citations") or 0
    if abstract_chars:
        a = p.get("abstract") or ""
        out["abstract"] = a[:abstract_chars] + ("…" if len(a) > abstract_chars else "")
    return out


# ═════════════════════════════════════════════════════════════════════
# ANSWER FINDER
# ═════════════════════════════════════════════════════════════════════
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

_DEF = [r"\b(is|are|was|were)\s+(a|an|the)\b", r"\brefers?\s+to\b", r"\bdefined\s+as\b",
        r"\bknown\s+as\b", r"\bconsists?\s+of\b", r"\bmeans?\b", r"\bcan\s+be\s+(defined|described)\b"]
_PATTERNS = {
    "what_is":    _DEF,
    "define":     _DEF,
    "what_are":   _DEF + [r"\binclude[sd]?\b"],
    "causes":     [r"\bcaus(e|es|ed|ing)\b", r"\bdue\s+to\b", r"\bresult(s|ed|ing)?\s+(from|in)\b",
                   r"\blead(s|ing)?\s+to\b", r"\bled\s+to\b", r"\bdriven\s+by\b", r"\bbecause\b",
                   r"\bcontribut(e|es|ed)\b"],
    "effect":     [r"\beffects?\b", r"\bimpacts?\b", r"\bresult(s|ed)?\s+in\b", r"\blead(s|ing)?\s+to\b",
                   r"\bincreas(e|es|ed)\b", r"\bdecreas(e|es|ed)\b", r"\breduc(e|es|ed)\b"],
    "how_does":   [r"\bby\b", r"\bthrough\b", r"\bmechanisms?\b", r"\bprocess\b", r"\bvia\b",
                   r"\bworks?\b", r"\bconvert(s|ed)?\b"],
    "why_is":     [r"\bbecause\b", r"\bimportant\b", r"\bessential\b", r"\bcritical\b",
                   r"\bdue\s+to\b", r"\bsince\b", r"\bso\s+that\b"],
    "difference": [r"\bdiffer", r"\bcompared?\s+(to|with)\b", r"\bwhereas\b", r"\bwhile\b"],
}

def _extract_keyword(query):
    q = query.strip().lower()
    for pattern, ttype in ANSWER_TEMPLATES:
        m = _re.match(pattern, q)
        if m:
            return m.group(1).strip(), ttype
    return None, None

def _kw_present(sent, kw):
    if term_in(sent, kw):
        return True
    words = [w for w in kw.split() if len(w) > 3]
    return bool(words) and all(term_in(sent, w) for w in words)

def _score_sentence(sent, kw, ttype, idx):
    sl, score = sent.lower(), 0
    score += 2 * sum(1 for p in _PATTERNS.get(ttype, _DEF) if _re.search(p, sl))
    m = _re.search(_re.escape(kw.lower()), sl)
    if m:
        if m.start() < 40:
            score += 1
        if _re.match(r"\s*(is|are|refers|means|can be)\b", sl[m.end():m.end() + 15]):
            score += 3                                     # "<topic> is …" reads like a definition
    if idx == 0:
        score += 1
    n = len(sent.split())
    if n > 45: score -= 2
    if n < 8:  score -= 1
    if _re.search(r"\b(we|our|this study|this paper|this review|here we)\b", sl):
        score -= 1                                         # talks about the paper, not the topic
    return score

def _answer_candidates(paper, kw, ttype, per_paper=2):
    abstract = paper.get("abstract") or ""
    if not abstract:
        return []
    sents = _re.split(r"(?<=[.!?])\s+", abstract)
    cands = []
    for i, s in enumerate(sents):
        s = s.strip()
        if len(s.split()) >= 6 and _kw_present(s, kw):
            cands.append({"text": s, "score": _score_sentence(s, kw, ttype, i), "paper": paper})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]

@tools_bp.route("/api/answer")
@login_required
def answer_finder():
    """Answer Finder. Params: q (e.g. 'What is biofuel') + shared filters
    (sources, year_from, year_to, oa, sort = match | cited | recent)."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400
    keyword, ttype = _extract_keyword(query)
    if not keyword:
        return jsonify({
            "error":   "unsupported_template",
            "message": "Try: What is [topic], What are [topic], How does [topic] work, What causes [topic], Define [topic]"
        }), 400
    try:
        f = read_filters(request.args.get)
        if request.args.get("sort") not in ("match", "cited", "recent"):
            f["sort"] = "match"
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")
        papers, _ = search_papers(keyword, 1, pool_f, per_source=15)

        cands, seen = [], set()
        for p in papers:
            for c in _answer_candidates(p, keyword, ttype):
                key = c["text"].lower()[:80]
                if key not in seen:
                    seen.add(key)
                    cands.append(c)
        if f["sort"] == "cited":
            cands.sort(key=lambda c: (c["paper"].get("citations") or 0, c["score"]), reverse=True)
        elif f["sort"] == "recent":
            cands.sort(key=lambda c: (sort_date(c["paper"]), c["score"]), reverse=True)
        else:
            cands.sort(key=lambda c: (c["score"], c["paper"].get("citations") or 0), reverse=True)
        top = cands[:5]
        return jsonify({
            "keyword":  keyword, "template": ttype, "query": query,
            "answers":  [c["text"] for c in top],
            "sources":  [_pick(c["paper"]) for c in top],          # same order as answers
            "searched": len(papers),
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "message":  "" if top else "No direct answer found. Try searching for papers instead.",
        })
    except Exception:
        return _fail("Answer search")

@tools_bp.route("/answer")
@login_required
def answer_page():
    return render_template("answer.html")


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

def _extract_keywords(text):
    out = []
    for w in _re.findall(r'\b\w{4,}\b', (text or "").lower()):
        if w not in _STOPWORDS and w not in out:
            out.append(w)
    return out[:8]

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

def _score_paper(paper, quote, keywords):
    """-> (score 0..1, best sentence, wording similarity, share of the quote's keywords found)."""
    abstract = paper.get("abstract") or ""
    if not abstract:
        return 0.0, "", 0.0, 0.0
    sim, sentence = _best_match(abstract, quote)
    cov = sum(1 for k in keywords if term_in(abstract, k)) / max(len(keywords), 1)
    return sim * 0.5 + cov * 0.5, sentence, sim, cov

def _confidence(score, sim, cov):
    """A match only counts as 'found' when the words really overlap, not just by chance."""
    if score >= 0.7 and sim >= 0.6:    return "high", True
    if score >= 0.45 and cov >= 0.5:   return "medium", True
    return ("low", False)

@tools_bp.route("/api/source-tracer", methods=["POST"])
@login_required
def source_tracer():
    """Params (JSON): quote + shared filters (sources, year_from, year_to, oa, sort = match | cited | recent)."""
    data  = request.get_json(silent=True) or {}
    quote = str(data.get("quote") or "").strip()[:2000]
    if not quote:
        return jsonify({"error": "Quote is required"}), 400
    if len(quote) < 10:
        return jsonify({"error": "Quote is too short. Please enter at least 10 characters."}), 400
    keywords = _extract_keywords(quote)
    if not keywords:
        return jsonify({"error": "Could not extract keywords from the quote."}), 400
    try:
        f = read_filters(data.get)
        if data.get("sort") not in ("match", "cited", "recent"):
            f["sort"] = "match"
        pool_f = dict(f, sort="cited")
        papers, _ = search_papers(" ".join(keywords[:6]), 1, pool_f, per_source=20, loose=True)

        scored = []
        for p in papers:
            score, sentence, sim, cov = _score_paper(p, quote, keywords)
            row = _pick(p, abstract_chars=300)
            row["score"] = round(score, 4)
            row["sim"], row["cov"] = round(sim, 3), round(cov, 3)
            row["snippet"] = sentence if score >= 0.3 and sentence else row.get("abstract", "")
            scored.append(row)
        scored.sort(key=lambda r: r["score"], reverse=True)

        top_score = scored[0]["score"] if scored else 0
        if scored:
            confidence, found = _confidence(top_score, scored[0]["sim"], scored[0]["cov"])
        else:
            confidence, found = "none", False

        if scored:
            scored[0]["is_best"] = found
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
                       "may be hiding it. Related papers are shown below.")
        return jsonify({
            "quote": quote, "keywords": keywords, "found": found, "confidence": confidence,
            "confidence_pct": int(top_score * 100), "results": shown, "message": message,
            "searched": len(papers), "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
        })
    except Exception:
        return _fail("Source tracing")

@tools_bp.route("/source-tracer")
@login_required
def source_tracer_page():
    return render_template("source_tracer.html")


# ═════════════════════════════════════════════════════════════════════
# TOPIC FINDER
# ═════════════════════════════════════════════════════════════════════
@tools_bp.route("/api/topics")
@login_required
def topics():
    """Params: subject (required), focus, kw + shared filters
    (sources, year_from, year_to, oa, sort = cited | recent | match, field = both | title | abstract)."""
    subject = request.args.get("subject", "").strip()[:200]
    focus   = request.args.get("focus", "").strip()[:200]
    kw      = request.args.get("kw", "").strip()[:200]
    if not subject:
        return jsonify({"error": "Enter a field or subject area first"}), 400
    try:
        f = read_filters(request.args.get)
        terms = []
        for t in _safe_terms(f"{subject} {focus} {kw}"):
            if t.lower() not in [x.lower() for x in terms]:
                terms.append(t)
        terms = terms[:10]
        full_query = " ".join(terms)
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")

        papers, _ = search_papers(full_query, 1, pool_f, per_source=20)
        strict_n = len(papers)
        if strict_n < 10 and len(_safe_terms(subject)) < len(terms):
            more, _ = search_papers(" ".join(_safe_terms(subject)), 1, pool_f, per_source=20)
            have = {(p.get("doi") or p.get("title") or "").lower() for p in papers}
            papers = papers + [p for p in more if (p.get("doi") or p.get("title") or "").lower() not in have]
        papers = apply_filters_and_sort(papers, f, full_query)[:30]
        return jsonify({
            "results": [_pick(p, abstract_chars=260) for p in papers],
            "relaxed": len(papers) > strict_n, "subject": subject, "query": full_query,
            "terms": terms, "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
        })
    except Exception:
        return _fail("Topic search")

@tools_bp.route("/topic-generator")
@login_required
def topic_generator():
    return render_template("topic_finder.html")
