"""routes/tools.py — Answer Finder, Grammar Checker (future), Topic Generator (future)"""
import re as _re
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, render_template, request, jsonify
from core import (login_required, OPENALEX_URL, reconstruct_abstract,
                  search_semantic_scholar, search_arxiv, search_pubmed, format_paper)

tools_bp = Blueprint("tools", __name__)

# ─── ANSWER FINDER ───────────────────────────────────────────────────
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

def _extract_keyword(query):
    q = query.strip().lower()
    for pattern, ttype in ANSWER_TEMPLATES:
        m = _re.match(pattern, q)
        if m:
            return m.group(1).strip(), ttype
    return None, None

def _find_answer_sentences(abstract, keyword, ttype):
    if not abstract:
        return []
    kw        = keyword.lower()
    kw_words  = kw.split()
    sentences = _re.split(r"(?<=[.!?])\s+", abstract)
    results   = []
    for sent in sentences:
        sl = sent.lower()
        if len(sent.split()) < 6:
            continue
        kw_present = kw in sl or all(w in sl for w in kw_words if len(w) > 3)
        if not kw_present:
            continue
        def_patterns = [
            " is ", " are ", " refers to", " can be", " was ",
            " involves", "defined as", "defined ", "is a ", "are a ",
            " process ", " mechanism ", " occurs ", " known as",
            " describes ", " represents ", " consists ", " plays ",
            " enables ", " allows ", " helps ", " causes ", " affects ",
        ]
        if any(p in sl for p in def_patterns):
            results.append(sent.strip())
        if len(results) >= 3:
            break
    if not results:
        for sent in sentences:
            sl = sent.lower()
            if (kw in sl or all(w in sl for w in kw_words if len(w) > 3)) and len(sent.split()) >= 6:
                results.append(sent.strip())
            if len(results) >= 2:
                break
    return results

def _fetch_answers_from_source(papers_or_func, keyword, ttype, is_openalex=False):
    """Extract answer sentences from a list of papers."""
    answers = []
    sources = []
    papers  = papers_or_func if isinstance(papers_or_func, list) else []
    for paper in papers:
        abstract = ""
        if is_openalex:
            abstract = reconstruct_abstract(paper.get("abstract_inverted_index"))
        else:
            abstract = paper.get("abstract", "") or ""
        if not abstract:
            continue
        sentences = _find_answer_sentences(abstract, keyword, ttype)
        if sentences:
            answers.extend(sentences)
            if is_openalex:
                loc = (paper.get("primary_location") or {})
                src = (loc.get("source") or {})
                sources.append({
                    "title":     paper.get("title", ""),
                    "journal":   src.get("display_name", ""),
                    "year":      paper.get("publication_year"),
                    "doi":       paper.get("doi", ""),
                    "oa_url":    (paper.get("open_access") or {}).get("oa_url", ""),
                    "citations": paper.get("cited_by_count", 0),
                    "source":    "OpenAlex",
                })
            else:
                sources.append({
                    "title":     paper.get("title", ""),
                    "journal":   paper.get("journal", ""),
                    "year":      paper.get("year"),
                    "doi":       paper.get("doi", ""),
                    "oa_url":    paper.get("oa_url", ""),
                    "citations": paper.get("citations", 0),
                    "source":    paper.get("data_source", ""),
                })
        if len(answers) >= 5:
            break
    return answers, sources

@tools_bp.route("/api/answer")
@login_required
def answer_finder():
    """
    Answer Finder — free, no points deducted.
    Uses all 4 sources: OpenAlex, Semantic Scholar, arXiv, PubMed.
    Templates: What is, What are, What causes, How does, Why is, Define
    """
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query required"}), 400

    keyword, ttype = _extract_keyword(query)
    if not keyword:
        return jsonify({
            "error":   "unsupported_template",
            "message": "Try: What is [topic], What are [topic], How does [topic] work, What causes [topic], Define [topic]"
        }), 400

    # Fetch from all 4 sources in parallel
    oa_papers = []
    other_papers = []

    def fetch_oa():
        try:
            resp = requests.get(OPENALEX_URL,
                                params={"search": keyword, "per-page": 15, "page": 1, "sort": "cited_by_count:desc"},
                                timeout=15)
            return resp.json().get("results", [])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_oa):                                      "openalex",
            executor.submit(search_semantic_scholar, keyword, 1, 15):       "semantic",
            executor.submit(search_arxiv, keyword, 1, 15):                  "arxiv",
            executor.submit(search_pubmed, keyword, 1, 15):                 "pubmed",
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                result = future.result()
                if src == "openalex":
                    oa_papers = result
                else:
                    other_papers.extend(result)
            except Exception:
                pass

    all_answers = []
    all_sources = []

    # OpenAlex first (best abstracts)
    ans, src = _fetch_answers_from_source(oa_papers, keyword, ttype, is_openalex=True)
    all_answers.extend(ans)
    all_sources.extend(src)

    # Other sources
    ans, src = _fetch_answers_from_source(other_papers, keyword, ttype, is_openalex=False)
    all_answers.extend(ans)
    all_sources.extend(src)

    # Deduplicate answers
    seen  = set()
    dedup = []
    for a in all_answers:
        key = a.lower()[:60]
        if key not in seen:
            seen.add(key)
            dedup.append(a)

    return jsonify({
        "keyword":  keyword,
        "template": ttype,
        "query":    query,
        "answers":  dedup[:5],
        "sources":  all_sources[:4],
        "message":  "" if dedup else "No direct answer found. Try searching for papers instead.",
    })

@tools_bp.route("/answer")
@login_required
def answer_page():
    return render_template("answer.html")

# ─── TOPIC GENERATOR (coming soon) ───────────────────────────────────
@tools_bp.route("/topic-generator")
@login_required
def topic_generator():
    return render_template("topic_finder.html")

# ─── SOURCE TRACER ───────────────────────────────────────────────────
import difflib

def _clean_text(text):
    """Normalize text for comparison."""
    text = text.lower().strip()
    text = _re.sub(r'[^\w\s]', ' ', text)
    text = _re.sub(r'\s+', ' ', text)
    return text

def _similarity_score(a, b):
    """Return similarity ratio between two strings."""
    return difflib.SequenceMatcher(None, _clean_text(a), _clean_text(b)).ratio()

def _extract_keywords(text):
    """Extract meaningful keywords from input text."""
    stopwords = {'the','a','an','is','are','was','were','be','been','being',
                 'have','has','had','do','does','did','will','would','could',
                 'should','may','might','shall','can','need','dare','ought',
                 'used','of','in','on','at','to','for','with','by','from',
                 'and','or','but','if','as','that','this','it','its','their',
                 'they','we','our','you','your','he','she','his','her','not'}
    words = _re.findall(r'\b\w{4,}\b', text.lower())
    return [w for w in words if w not in stopwords][:8]

def _score_paper(paper_abstract, input_text, keywords):
    """Score a paper based on similarity and keyword overlap."""
    if not paper_abstract:
        return 0
    sim    = _similarity_score(input_text, paper_abstract[:500])
    kw_hit = sum(1 for kw in keywords if kw in paper_abstract.lower())
    kw_score = kw_hit / max(len(keywords), 1)
    return (sim * 0.6) + (kw_score * 0.4)

@tools_bp.route("/api/source-tracer", methods=["POST"])
@login_required
def source_tracer():
    data  = request.get_json() or {}
    quote = (data.get("quote") or "").strip()
    if not quote:
        return jsonify({"error": "Quote is required"}), 400
    if len(quote) < 10:
        return jsonify({"error": "Quote is too short. Please enter at least 10 characters."}), 400

    keywords = _extract_keywords(quote)
    if not keywords:
        return jsonify({"error": "Could not extract keywords from the quote."}), 400

    search_query = " ".join(keywords[:5])

    # Fetch from all 4 sources in parallel
    oa_papers    = []
    other_papers = []

    def fetch_oa():
        try:
            resp = requests.get(OPENALEX_URL,
                                params={"search": search_query, "per-page": 20,
                                        "page": 1, "sort": "cited_by_count:desc"},
                                timeout=15)
            return resp.json().get("results", [])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_oa):                                        "openalex",
            executor.submit(search_semantic_scholar, search_query, 1, 20):   "semantic",
            executor.submit(search_arxiv, search_query, 1, 20):              "arxiv",
            executor.submit(search_pubmed, search_query, 1, 20):             "pubmed",
        }
        for future in as_completed(futures):
            src = futures[future]
            try:
                result = future.result()
                if src == "openalex":
                    oa_papers = result
                else:
                    other_papers.extend(result)
            except Exception:
                pass

    # Score all papers
    scored = []

    for paper in oa_papers:
        abstract = reconstruct_abstract(paper.get("abstract_inverted_index")) or ""
        score    = _score_paper(abstract, quote, keywords)
        loc      = (paper.get("primary_location") or {})
        src      = (loc.get("source") or {})
        scored.append({
            "title":       paper.get("title", ""),
            "authors":     [(a.get("author") or {}).get("display_name", "")
                            for a in paper.get("authorships", [])
                            if (a.get("author") or {}).get("display_name")][:3],
            "year":        paper.get("publication_year"),
            "journal":     src.get("display_name", ""),
            "doi":         paper.get("doi", ""),
            "oa_url":      (paper.get("open_access") or {}).get("oa_url", ""),
            "citations":   paper.get("cited_by_count", 0),
            "abstract":    abstract[:300],
            "score":       score,
            "data_source": "OpenAlex",
        })

    for paper in other_papers:
        abstract = paper.get("abstract", "") or ""
        score    = _score_paper(abstract, quote, keywords)
        scored.append({
            "title":       paper.get("title", ""),
            "authors":     (paper.get("authors") or [])[:3],
            "year":        paper.get("year"),
            "journal":     paper.get("journal", ""),
            "doi":         paper.get("doi", ""),
            "oa_url":      paper.get("oa_url", ""),
            "citations":   paper.get("citations", 0) or 0,
            "abstract":    abstract[:300],
            "score":       score,
            "data_source": paper.get("data_source", ""),
        })

    # Sort by score
    scored.sort(key=lambda x: x["score"], reverse=True)

    # Determine confidence
    top_score = scored[0]["score"] if scored else 0
    if top_score >= 0.5:
        confidence = "high"
        confidence_pct = int(top_score * 100)
        found = True
    elif top_score >= 0.25:
        confidence = "medium"
        confidence_pct = int(top_score * 100)
        found = True
    elif scored:
        confidence = "low"
        confidence_pct = int(top_score * 100)
        found = False
    else:
        confidence = "none"
        confidence_pct = 0
        found = False

    message = ""
    if not found:
        message = ("No exact source found. Possible reasons: the source may not be indexed "
                   "in our databases, the quote may be paraphrased, or there may be a typo. "
                   "Related papers are shown below.")

    return jsonify({
        "quote":          quote,
        "keywords":       keywords,
        "found":          found,
        "confidence":     confidence,
        "confidence_pct": confidence_pct,
        "results":        scored[:5],
        "message":        message,
    })

@tools_bp.route("/source-tracer")
@login_required
def source_tracer_page():
    return render_template("source_tracer.html")
