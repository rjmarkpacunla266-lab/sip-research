"""routes/tools.py — Answer Finder, Source Tracer, Topic Finder.
All three use the shared engine (engine.py), so they support the same filters as Search Papers:
sources, years, Open Access and sort."""
import re as _re
import difflib
import math
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
    (r"^(?:what (?:is|are) )?(?:the )?differences? between (.+?) and (.+)$",                  "difference"),
    (r"^(?:what (?:is|are) )?the (?:effects?|impacts?|consequences?) of (.+)$",               "effect"),
    (r"^(?:effects?|impacts?|consequences?) of (.+)$",                                        "effect"),
    (r"^(?:what (?:is|are) )?the (?:causes?(?: of)?|reasons? (?:for|behind)|drivers? of) (.+)$", "causes"),
    (r"^what causes (.+)$",                             "causes"),
    (r"^what are (.+)$",                                "what_are"),
    (r"^what is (.+)$",                                 "what_is"),
    (r"^how does (.+) work$",                           "how_does"),
    (r"^how does (.+)$",                                "how_does"),
    (r"^why is (.+)$",                                  "why_is"),
    (r"^define (.+)$",                                  "define"),
]

# words the user may have typed after the topic ("why is X important", "how does X works")
_TRAIL = {
    "how_does": r"\s+works?$",
    "why_is":   r"\s+(?:important|essential|critical|crucial|necessary|significant|useful|needed)$",
}

MIN_SCORE  = 4      # a sentence below this is not shown (better fewer answers than wrong ones)
BEST_SCORE = 6      # the top sentence gets a "Best answer" badge at or above this

def _clean_kw(kw):
    return _re.sub(r"^(?:the|a|an)\s+", "", (kw or "").strip())

def _extract_keyword(query):
    """-> (keyword, template, second keyword or None). Second keyword is only used by 'difference'."""
    q = query.strip().lower().rstrip("?.! ")
    for pattern, ttype in ANSWER_TEMPLATES:
        m = _re.match(pattern, q)
        if not m:
            continue
        kw  = m.group(1)
        kw2 = m.group(2) if ttype == "difference" else None
        if ttype in _TRAIL:
            kw = _re.sub(_TRAIL[ttype], "", kw.strip())
        kw, kw2 = _clean_kw(kw), (_clean_kw(kw2) if kw2 else None)
        if kw and (kw2 or ttype != "difference"):
            return kw, ttype, kw2
    return None, None, None

def _kw_regex(kw):
    parts = [_re.escape(w) for w in kw.split()]
    return r"\b" + r"\s+".join(parts) + r"(?:s|es)?\b"

def _kw_present(sent, kw):
    if term_in(sent, kw):
        return True
    words = [w for w in kw.split() if len(w) > 3]
    return bool(words) and all(term_in(sent, w) for w in words)

# ── Scoring rules ────────────────────────────────────────────────────
# Each rule is (weight, regex). <K> stands for the topic. "pos" rules say the sentence really answers
# THIS kind of question (the direction matters: a cause sentence has the topic as the thing being caused).
# "neg" rules catch sentences that look similar but answer the opposite question.
_DEF_POS = [
    (6, r"^\W*(?:the\s+|a\s+|an\s+)?<K>\s+(?:is|are|refers?\s+to|means?|denotes?|describes?|can\s+be\s+defined\s+as)\b"),
    (4, r"<K>.{0,40}?\b(?:is|are)\s+(?:defined|known|described|considered)\s+as\b"),
    (3, r"<K>.{0,40}?\b(?:is|are)\s+(?:a|an|the)\b"),
    (3, r"<K>.{0,40}?\b(?:refers?\s+to|consists?\s+of|comprises?|defined\s+as)\b"),
]
_DEF_NEG = [
    (4, r"\b(?:topic|buzzword|debate|discussion)\b"),
    (3, r"\b(?:is|are)\s+(?:an?\s+)?(?:\w+\s+)?(?:ongoing|growing|increasingly|important|major|pressing|serious|significant)\s+(?:\w+\s+)?(?:topic|issue|challenge|concern|problem|threat|area|field)\b"),
]

_RULES = {
    "what_is":  {"pos": _DEF_POS, "neg": _DEF_NEG},
    "define":   {"pos": _DEF_POS, "neg": _DEF_NEG},
    "what_are": {"pos": _DEF_POS + [(2, r"<K>.{0,40}?\b(?:include[sd]?|such\s+as|types?\s+of|categories)\b")], "neg": _DEF_NEG},
    "causes": {
        "pos": [
            (5, r"<K>.{0,70}?\b(?:caused|driven|triggered|fuell?ed|attributed|brought\s+about)\s+(?:\w+\s+)?(?:by|to)\b"),
            (5, r"\b(?:causes?|drivers?|reasons?|factors?|sources?|contributors?|origins?|determinants?)\s+(?:of|for|behind)\s+(?:\w+\s+){0,3}?<K>"),
            (4, r"<K>.{0,70}?\b(?:results?\s+from|stems?\s+from|arises?\s+from|originates?\s+from|due\s+to|because\s+of|owing\s+to|is\s+a\s+result\s+of)\b"),
            (4, r"\b(?:cause[sd]?|drives?|driving|contribut(?:e|es|ed|ing)\s+(?:\w+ly\s+)?to|lead(?:s|ing)?\s+to|led\s+to|result(?:s|ed|ing)?\s+in)\s+(?:\w+\s+){0,3}?<K>"),
        ],
        "neg": [
            # the topic is the CAUSE or the thing being discussed for its effects, not the thing being caused
            (5, r"\b(?:effects?|impacts?|consequences?|threats?|risks?|implications?)\s+(?:of|from|on)\s+(?:\w+\s+){0,2}?<K>"),
            (5, r"<K>\s+(?:is\s+(?:likely|expected|projected|predicted)\s+to|will|may|might|can|could|has\s+led\s+to|have\s+led\s+to|leads?\s+to|causes?|results?\s+in|affects?|impacts?|threatens?|exacerbates?|increases?|reduces?)\b(?!\s+be\b)"),
        ],
    },
    "effect": {
        "pos": [
            (5, r"\b(?:effects?|impacts?|consequences?|implications?)\s+(?:of|from)\s+(?:\w+\s+){0,2}?<K>"),
            (4, r"<K>.{0,60}?\b(?:causes?|leads?\s+to|results?\s+in|increases?|decreases?|reduces?|affects?|impacts?|threatens?|exacerbates?|is\s+associated\s+with|contributes?\s+to)\b"),
            (4, r"\b(?:as\s+a\s+result\s+of|due\s+to|owing\s+to|consequence\s+of)\s+(?:\w+\s+){0,2}?<K>"),
        ],
        "neg": [
            (3, r"<K>.{0,40}?\b(?:is|are)\s+(?:mainly\s+|primarily\s+)?caused\s+by\b"),
        ],
    },
    "how_does": {
        "pos": [
            (3, r"<K>.{0,60}?\b(?:works?|occurs?|operates?|functions?|proceeds?|happens?|takes?\s+place)\b"),
            (3, r"\b(?:mechanisms?|process|pathways?|steps?|stages?)\b"),
            (2, r"\b(?:converts?|produces?|transforms?|binds?|synthesi[sz]es?|uses?)\b"),
            (2, r"\b(?:by|through|via)\s+(?:\w+\s+){0,2}\w+ing\b"),
        ],
        "neg": [],
    },
    "why_is": {
        "pos": [
            (3, r"\bbecause\b|\bdue\s+to\b|\bso\s+that\b|\bsince\b"),
            (3, r"\bplays?\s+(?:a\s+)?(?:key|crucial|vital|important|central|critical)\s+role\b|\b(?:essential|crucial|vital|critical|indispensable|necessary)\b"),
            (2, r"\bimportan(?:t|ce)\b|\bbenefits?\b|\bsupports?\b"),
        ],
        "neg": [],
    },
    "difference": {
        "pos": [
            (3, r"\bdiffer\w*\b"),
            (3, r"\bcompared?\s+(?:to|with)\b|\bin\s+contrast\b|\bunlike\b|\bwhereas\b|\bversus\b|\bvs\b"),
            (2, r"\bwhile\b|\bbetween\b"),
        ],
        "neg": [],
    },
}

# words shown highlighted on the page so the reader sees WHY a sentence was picked
_CUES = {
    "causes":     ["caused by", "driven by", "triggered by", "due to", "results from", "stems from", "because of",
                   "causes of", "cause of", "drivers of", "contributes to", "leads to"],
    "effect":     ["effects of", "effect of", "impacts of", "impact of", "consequences of", "results in", "leads to",
                   "increases", "reduces", "affects"],
    "what_is":    ["refers to", "defined as", "known as", "consists of", "means"],
    "what_are":   ["refers to", "defined as", "known as", "consists of", "include", "such as"],
    "define":     ["refers to", "defined as", "known as", "consists of", "means"],
    "how_does":   ["mechanism", "process", "through", "via"],
    "why_is":     ["because", "important", "essential", "crucial", "critical", "vital", "due to"],
    "difference": ["differ", "differs", "compared to", "compared with", "whereas", "in contrast", "unlike", "versus"],
}

# sentences that only make sense with the sentence before them, or that talk about the paper itself
_DANGLING = _re.compile(r"^\W*(?:these|this|those|such|it|they|their|its|that|he|she|there)\b", _re.I)
_CONNECT  = _re.compile(r"^\W*(?:however|moreover|furthermore|therefore|thus|also|additionally|in addition|consequently|hence|nevertheless|meanwhile|overall)\b", _re.I)
_META     = _re.compile(r"\b(?:this (?:study|paper|article|review|work|chapter)|we (?:show|find|found|propose|investigate|examine|analy[sz]e|present|study|use|discuss|review|argue|aim)"
                        r"|our (?:results|findings|study|analysis|work|approach)|the present (?:study|paper)|literature|researchers?|authors?|studies|empirical(?:ly)?"
                        r"|systematic review|meta-analysis|relevant)\b", _re.I)

_ABBR = _re.compile(r"\b(et al|e\.g|i\.e|vs|fig|figs|approx|cf|dr|prof|u\.s|u\.k|eq|resp|etc)\.", _re.I)

def _split_sentences(text):
    """Split an abstract into sentences without breaking on 'et al.', 'e.g.', 'U.S.' and the like."""
    t = _ABBR.sub(lambda m: m.group(0)[:-1] + "\u2024", text)
    parts = _re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", t)
    return [p.replace("\u2024", ".").strip() for p in parts if p.strip()]

def _score_sentence(sent, kw, ttype, idx, kw2=None):
    """-> (score, number of direction-aware patterns that matched)."""
    rules = _RULES.get(ttype) or _RULES["what_is"]
    K = _kw_regex(kw)
    score = hits = 0
    for w, pat in rules["pos"]:
        if _re.search(pat.replace("<K>", K), sent, _re.I):
            score += w
            hits += 1
    for w, pat in rules.get("neg", []):
        if _re.search(pat.replace("<K>", K), sent, _re.I):
            score -= w
    if ttype == "difference" and kw2:
        score += 4                                         # both things are in the sentence (checked by caller)
    if _DANGLING.search(sent):
        score -= 4
    elif _CONNECT.search(sent):
        score -= 1
    if _META.search(sent):
        score -= 4
    n = len(sent.split())
    if n > 45: score -= 2
    if n < 8:  score -= 2
    if idx == 0:
        score += 1
    return score, hits

def _answer_candidates(paper, kw, ttype, kw2=None, per_paper=2):
    abstract = paper.get("abstract") or ""
    if not abstract:
        return []
    cands = []
    for i, s in enumerate(_split_sentences(abstract)):
        n = len(s.split())
        if s.endswith("?") or n < 6 or n > 60:
            continue
        if ttype == "difference":
            if not (kw2 and _kw_present(s, kw) and _kw_present(s, kw2)):
                continue
        elif not _kw_present(s, kw):
            continue
        score, hits = _score_sentence(s, kw, ttype, i, kw2)
        if hits and score >= MIN_SCORE:                    # must really match the question type
            cands.append({"text": s, "score": score, "paper": paper})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]

_BOOST = {"causes": "causes", "effect": "effects", "how_does": "mechanism", "why_is": "importance"}

def _answer_query(kw, ttype, kw2):
    """The paper search is aimed at the question, not only at the topic."""
    if ttype == "difference":
        return f"{kw} {kw2}"
    return f"{kw} {_BOOST[ttype]}" if ttype in _BOOST else kw

def _collect(papers, kw, ttype, kw2, cands, seen):
    for p in papers:
        for c in _answer_candidates(p, kw, ttype, kw2):
            key = c["text"].lower()[:80]
            if key not in seen:
                seen.add(key)
                cands.append(c)

def _rank(c):
    """Sentence quality first; citations add a gentle (log-scaled) boost instead of only breaking ties."""
    return c["score"] + 1.2 * math.log10(1 + (c["paper"].get("citations") or 0))

@tools_bp.route("/api/answer")
@login_required
def answer_finder():
    """Answer Finder. Params: q (e.g. 'What causes climate change') + shared filters
    (sources, year_from, year_to, oa, sort = match | cited | recent)."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400
    keyword, ttype, kw2 = _extract_keyword(query)
    if not keyword:
        return jsonify({
            "error":   "unsupported_template",
            "message": "Try: What is [topic], What are [topic], What causes [topic], Effect of [topic], How does [topic] work, Why is [topic] important, Define [topic], or Difference between [A] and [B]"
        }), 400
    try:
        f = read_filters(request.args.get)
        if request.args.get("sort") not in ("match", "cited", "recent"):
            f["sort"] = "match"
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")

        q = _answer_query(keyword, ttype, kw2)
        papers, _ = search_papers(q, 1, pool_f, per_source=15)
        cands, seen = [], set()
        _collect(papers, keyword, ttype, kw2, cands, seen)
        searched = len(papers)
        if len(cands) < 3 and q != keyword and ttype != "difference":
            more, _ = search_papers(keyword, 1, pool_f, per_source=15)      # fall back to the plain topic
            _collect(more, keyword, ttype, kw2, cands, seen)
            searched += len(more)

        if f["sort"] == "cited":
            cands.sort(key=lambda c: (c["paper"].get("citations") or 0, c["score"]), reverse=True)
        elif f["sort"] == "recent":
            cands.sort(key=lambda c: (sort_date(c["paper"]), c["score"]), reverse=True)
        else:
            cands.sort(key=_rank, reverse=True)
        top = cands[:5]
        best = 0 if (f["sort"] == "match" and top and top[0]["score"] >= BEST_SCORE) else None
        return jsonify({
            "keyword":  keyword, "keyword2": kw2, "template": ttype, "query": query,
            "answers":  [c["text"] for c in top],
            "sources":  [_pick(c["paper"]) for c in top],          # same order as answers
            "cues":     _CUES.get(ttype, []),
            "best_index": best,
            "searched": searched,
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "message":  "" if top else "No clear answer found in the top papers. Try searching for papers instead.",
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
