"""routes/topic_tool.py: Topic Finder (/topic-generator and /api/topic*). Edit this file for topic-finder changes only."""
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
from core import login_required, _safe_terms, OPENALEX_URL, format_paper
from engine import (read_filters, search_papers, apply_filters_and_sort, term_in, relevance,
                    sort_date, SOURCE_LABELS, search_sources, merge_dedupe, to_int)
import topicfinder
from routes.tools_common import tools_bp, _fail, _pick, _clean_abstract, _pubmed_abstracts, _client_paper, _DOI_OK


# ═════════════════════════════════════════════════════════════════════
# TOPIC FINDER
# ═════════════════════════════════════════════════════════════════════
def _dedupe_terms(terms):
    out = []
    for t in terms:
        if t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out

def _on_topic(p, subj_terms):
    """A paper is on topic when (almost) every subject word is in its title or abstract.
    PubMed results without an abstract are judged on the title alone."""
    if not subj_terms:
        return True
    text = f"{p.get('title') or ''} {p.get('abstract') or ''}"
    need = max(1, math.ceil(0.67 * len(subj_terms)))
    hit = sum(1 for t in subj_terms if term_in(text, t))
    return hit >= need

# ── Topic Finder: paper type (review / original study) and the "recommended" order ──
# The sources do not all say whether a paper is a review, so it is judged from the title (and the first words of the
# abstract). It is a good guess, not a guarantee, and the page says so.
_REVIEW_TITLE = _re.compile(r"\b(review|reviews|meta[- ]?analys[ie]s|overview|scoping|umbrella|state[- ]of[- ]the[- ]art|"
                           r"recent advances|advances in|progress in|challenges and opportunities|a primer|roadmap)\b", _re.I)
_REVIEW_ABS = _re.compile(r"\b(this|present|current|in this|the present) (systematic |scoping |literature |narrative |critical )?"
                          r"(review|meta[- ]?analysis|overview)\b", _re.I)

def _paper_kind(p):
    title = p.get("title") or ""
    if _REVIEW_TITLE.search(title) or _REVIEW_ABS.search((p.get("abstract") or "")[:400]):
        return "review"
    if "arxiv" in (p.get("data_source") or "").lower() and _re.search(r"\bsurvey\b", title, _re.I):
        return "review"                    # on arXiv 'survey' means a literature survey; elsewhere it is often a questionnaire
    return "original"

def _balanced_key(p, terms, this_year):
    """'Recommended' order: papers that are recent, well cited FOR THEIR AGE, and on topic come first.
    (Plain 'most cited' always puts the oldest famous papers on top.)"""
    y = to_int(p.get("year")) or 0
    age = max(0, this_year - y) if y else 15
    rate = math.log10(1 + (p.get("citations") or 0) / (age + 1)) / 3.0        # ~1000 citations per year = 1
    recent = max(0.0, 1.0 - age / 12.0)
    rel = relevance(p, terms) / (3.0 * len(terms)) if terms else 0.0
    return 0.4 * min(1.0, rate) + 0.4 * recent + 0.2 * min(1.0, rel)

# ── Topic Finder: broad subjects and filler words ──
# A broad subject such as "Biology" matches almost every paper, so the results are scattered and the coverage numbers mean
# nothing. The page asks the student to pick something specific (a hand-written list, no AI), and filler words typed into
# Focus / Keywords ("of", "effect") are not counted as required words.
_BROAD = {
    "biology": ["biofuel", "coral reef bleaching", "mangrove restoration", "antibiotic resistance", "CRISPR gene editing", "microplastics in fish", "pollinator decline", "invasive species"],
    "chemistry": ["biofuel", "lithium-ion battery", "water purification", "plastic recycling", "green chemistry", "corrosion inhibitor", "perovskite solar cell", "catalyst"],
    "physics": ["solar cell efficiency", "wind turbine", "superconductor", "quantum computing", "nuclear fusion", "thermoelectric", "ultrasound", "photonics"],
    "psychology": ["social media and anxiety", "sleep deprivation", "classroom motivation", "bullying", "screen time children", "mindfulness", "procrastination", "memory and learning"],
    "computer science": ["machine learning", "cybersecurity", "blockchain", "computer vision", "natural language processing", "internet of things", "cloud computing", "software testing"],
    "environmental science": ["microplastics", "air pollution", "mangrove restoration", "plastic waste", "ocean acidification", "deforestation", "wastewater treatment", "carbon sequestration"],
    "environment": ["microplastics", "air pollution", "plastic waste", "deforestation", "flood risk", "wastewater treatment", "urban heat island", "carbon sequestration"],
    "science": ["biofuel", "solar energy", "microplastics", "water purification", "antibiotic resistance", "climate change", "renewable energy", "food preservation"],
    "medicine": ["antibiotic resistance", "dengue", "diabetes", "vaccine", "tuberculosis", "hypertension", "telemedicine", "herbal medicine"],
    "health": ["childhood obesity", "mental health students", "dengue prevention", "sleep and health", "vaccination", "nutrition", "air pollution and asthma", "hand hygiene"],
    "engineering": ["bioplastic", "solar panel", "flood control", "3D printing", "wind turbine", "water filtration", "earthquake-resistant buildings", "electric vehicle battery"],
    "technology": ["artificial intelligence in education", "3D printing", "internet of things", "solar technology", "electric vehicles", "drones", "virtual reality learning", "cybersecurity"],
    "mathematics": ["cryptography", "graph theory", "machine learning mathematics", "game theory", "epidemic modelling", "optimization", "statistics education", "fractals"],
    "agriculture": ["rice yield", "organic fertilizer", "hydroponics", "pest control", "drought-resistant crops", "soil erosion", "aquaponics", "food security"],
    "ecology": ["coral reef", "mangrove", "invasive species", "pollination", "forest fragmentation", "food web", "biodiversity loss", "wetland"],
    "genetics": ["CRISPR", "gene therapy", "genetically modified crops", "heredity disease", "DNA barcoding", "epigenetics", "genetic diversity", "mutation"],
    "education": ["online learning", "classroom motivation", "reading comprehension", "STEM education", "homework", "teacher training", "bullying", "educational technology"],
    "economics": ["inflation", "remittances", "poverty reduction", "minimum wage", "microfinance", "tourism", "e-commerce", "climate economics"],
    "business": ["small business", "e-commerce", "customer loyalty", "social media marketing", "startup funding", "supply chain", "entrepreneurship", "employee motivation"],
    "sociology": ["social media and youth", "poverty", "migration", "gender inequality", "urbanization", "family structure", "bullying", "volunteering"],
    "geology": ["earthquake", "volcano", "landslide", "soil erosion", "groundwater", "mineral", "tsunami", "coastal erosion"],
    "energy": ["solar energy", "biofuel", "wind power", "geothermal", "battery storage", "hydrogen fuel", "energy efficiency", "ocean energy"],
    "nutrition": ["childhood obesity", "vitamin D", "malnutrition", "plant-based diet", "food fortification", "sugar intake", "breastfeeding", "intermittent fasting"],
}
_BROAD_ALIAS = {"biological": "biology", "chemical": "chemistry", "physical": "physics", "sciences": "science", "computer": "computer science",
                "computing": "computer science", "informatics": "computer science", "mathematical": "mathematics", "math": "mathematics",
                "maths": "mathematics", "medical": "medicine", "farming": "agriculture", "ecological": "ecology", "economy": "economics",
                "environmental": "environment", "environmental sciences": "environmental science", "it": "technology"}

def _broad_for(subject):
    """-> list of specific suggestions when the subject is only a broad field name, else None."""
    s = _re.sub(r"[^a-z ]", " ", (subject or "").lower())
    s = _re.sub(r"\s+", " ", s).strip()
    s = _re.sub(r"^(?:the\s+)?(?:field\s+of\s+|study\s+of\s+)", "", s)
    s = _BROAD_ALIAS.get(s, s)
    return _BROAD.get(s)

# words that are not topics: they are dropped from the 'required words' (coverage line, word chips, search)
_FILLER = {"of", "the", "a", "an", "and", "or", "in", "on", "for", "to", "with", "by", "from", "at", "as", "is", "are", "was",
           "were", "be", "about", "between", "among", "how", "what", "why", "when", "does", "do", "that", "this", "its", "their",
           "effect", "effects", "impact", "impacts", "influence", "influences", "role", "roles", "relationship", "relationships",
           "cause", "causes", "comparison", "compare", "study", "studies", "analysis", "research", "factor", "factors", "using"}

def _specific(terms):
    """Terms without filler words (keeps the original order). Never returns an empty list for a non-empty input
    unless every word was filler."""
    return [t for t in terms if t.lower() not in _FILLER]

@tools_bp.route("/api/topic-hint")
@login_required
def topic_hint():
    """Called while the student types the subject: is it too broad, and what specific topics could they try instead?"""
    sug = _broad_for(request.args.get("q", ""))
    return jsonify({"broad": bool(sug), "suggestions": sug or []})

# ── Topic Finder: how big is the topic, and what exists locally ──
def _oa_filter(f, terms_expr, extra=""):
    """An OpenAlex 'filter' string for the same words and filters as the search (title+abstract, years, Open Access)."""
    base = []
    if f.get("year_from"): base.append(f"from_publication_date:{f['year_from']}-01-01")
    if f.get("year_to"):   base.append(f"to_publication_date:{f['year_to']}-12-31")
    if f.get("oa_only"):   base.append("is_oa:true")
    fkey = {"title": "title.search", "abstract": "abstract.search"}.get(f.get("field"), "title_and_abstract.search")
    if terms_expr:
        base.append(f"{fkey}:{terms_expr}")
    if extra:
        base.append(extra)
    return ",".join(base)

def _oa_query(filter_str, per_page=1, sort=None):
    """-> (number of matching papers or None, list of raw OpenAlex works). Never raises."""
    try:
        params = {"filter": filter_str, "per-page": per_page}
        if sort:
            params["sort"] = sort
        r = requests.get(OPENALEX_URL, params=params, timeout=10)
        if not r.ok:
            return None, []
        d = r.json()
        return (d.get("meta") or {}).get("count"), d.get("results") or []
    except Exception:
        return None, []

def _oa_years(filter_str):
    """How many papers per publication year for the same words and filters -> [{year, count}] (oldest first) or None. Never raises."""
    try:
        r = requests.get(OPENALEX_URL, params={"filter": filter_str, "group_by": "publication_year"}, timeout=10)
        if not r.ok:
            return None
        out = []
        for g in (r.json().get("group_by") or []):
            y, c = to_int(g.get("key")), to_int(g.get("count"))
            if y and c:
                out.append({"year": y, "count": c})
        return sorted(out, key=lambda x: x["year"])
    except Exception:
        return None

def _year_bars(rows, this_year, span=20):
    rows = [r for r in (rows or []) if this_year - span < r["year"] <= this_year]
    if not rows:
        return None
    lo = min(r["year"] for r in rows)
    have = {r["year"]: r["count"] for r in rows}
    return [{"year": y, "count": have.get(y, 0)} for y in range(lo, this_year + 1)]


_SIZE_LEVELS = [   # (smallest count, key, label, advice)
    (0,    "none",   "No match",      "No paper contains all your words together. This could be a research gap, or papers may use different words. Try a synonym or remove one word."),
    (1,    "tiny",   "Very narrow",   "Very few papers cover exactly this. It may be too narrow to find enough to read, or it may be a real gap. Try dropping one word."),
    (5,    "small",  "Narrow",        "A small, focused topic. Doable, but you may need to read around the edges."),
    (30,   "good",   "Good size",     "Enough research to build on, and small enough to focus on."),
    (601,  "broad",  "Broad",         "A lot of research. Add a focus word or tap a sub-topic to narrow it down."),
    (5001, "huge",   "Too broad",     "Too broad for one paper. Pick a more specific subject or tap a sub-topic."),
]

def _size_info(total):
    if total is None:
        return None
    level = _SIZE_LEVELS[0]
    for lv in _SIZE_LEVELS:
        if total >= lv[0]:
            level = lv
    return {"count": int(total), "level": level[1], "label": level[2], "advice": level[3],
            "scale": [{"key": k, "label": lab} for _, k, lab, _ in _SIZE_LEVELS]}

_COUNTRIES = {
    'PH': ('Philippines', ['Philippines', 'Philippine', 'Filipino', 'Filipinos']),
    'US': ('United States', ['United States', 'USA']),
    'GB': ('United Kingdom', ['United Kingdom', 'Britain', 'British']),
    'CA': ('Canada', ['Canada', 'Canadian']),
    'AU': ('Australia', ['Australia', 'Australian']),
    'NZ': ('New Zealand', ['New Zealand']),
    'IN': ('India', ['India', 'Indian']),
    'PK': ('Pakistan', ['Pakistan', 'Pakistani']),
    'BD': ('Bangladesh', ['Bangladesh', 'Bangladeshi']),
    'LK': ('Sri Lanka', ['Sri Lanka', 'Sri Lankan']),
    'NP': ('Nepal', ['Nepal', 'Nepali', 'Nepalese']),
    'ID': ('Indonesia', ['Indonesia', 'Indonesian']),
    'MY': ('Malaysia', ['Malaysia', 'Malaysian']),
    'SG': ('Singapore', ['Singapore', 'Singaporean']),
    'TH': ('Thailand', ['Thailand', 'Thai']),
    'VN': ('Vietnam', ['Vietnam', 'Vietnamese']),
    'KH': ('Cambodia', ['Cambodia', 'Cambodian']),
    'MM': ('Myanmar', ['Myanmar', 'Burmese']),
    'CN': ('China', ['China', 'Chinese']),
    'HK': ('Hong Kong', ['Hong Kong']),
    'TW': ('Taiwan', ['Taiwan', 'Taiwanese']),
    'JP': ('Japan', ['Japan', 'Japanese']),
    'KR': ('South Korea', ['South Korea', 'Korean']),
    'SA': ('Saudi Arabia', ['Saudi Arabia', 'Saudi']),
    'AE': ('United Arab Emirates', ['United Arab Emirates', 'UAE']),
    'QA': ('Qatar', ['Qatar', 'Qatari']),
    'IL': ('Israel', ['Israel', 'Israeli']),
    'TR': ('Turkey', ['Turkey', 'Turkish', 'Turkiye']),
    'IR': ('Iran', ['Iran', 'Iranian']),
    'IQ': ('Iraq', ['Iraq', 'Iraqi']),
    'EG': ('Egypt', ['Egypt', 'Egyptian']),
    'NG': ('Nigeria', ['Nigeria', 'Nigerian']),
    'GH': ('Ghana', ['Ghana', 'Ghanaian']),
    'KE': ('Kenya', ['Kenya', 'Kenyan']),
    'ET': ('Ethiopia', ['Ethiopia', 'Ethiopian']),
    'ZA': ('South Africa', ['South Africa', 'South African']),
    'TZ': ('Tanzania', ['Tanzania', 'Tanzanian']),
    'UG': ('Uganda', ['Uganda', 'Ugandan']),
    'ZW': ('Zimbabwe', ['Zimbabwe', 'Zimbabwean']),
    'MA': ('Morocco', ['Morocco', 'Moroccan']),
    'DZ': ('Algeria', ['Algeria', 'Algerian']),
    'TN': ('Tunisia', ['Tunisia', 'Tunisian']),
    'DE': ('Germany', ['Germany', 'German']),
    'FR': ('France', ['France', 'French']),
    'IT': ('Italy', ['Italy', 'Italian']),
    'ES': ('Spain', ['Spain', 'Spanish']),
    'PT': ('Portugal', ['Portugal', 'Portuguese']),
    'NL': ('Netherlands', ['Netherlands', 'Dutch']),
    'BE': ('Belgium', ['Belgium', 'Belgian']),
    'CH': ('Switzerland', ['Switzerland', 'Swiss']),
    'AT': ('Austria', ['Austria', 'Austrian']),
    'SE': ('Sweden', ['Sweden', 'Swedish']),
    'NO': ('Norway', ['Norway', 'Norwegian']),
    'DK': ('Denmark', ['Denmark', 'Danish']),
    'FI': ('Finland', ['Finland', 'Finnish']),
    'IE': ('Ireland', ['Ireland', 'Irish']),
    'PL': ('Poland', ['Poland', 'Polish']),
    'CZ': ('Czechia', ['Czech Republic', 'Czechia', 'Czech']),
    'GR': ('Greece', ['Greece', 'Greek']),
    'RU': ('Russia', ['Russia', 'Russian']),
    'UA': ('Ukraine', ['Ukraine', 'Ukrainian']),
    'RO': ('Romania', ['Romania', 'Romanian']),
    'HU': ('Hungary', ['Hungary', 'Hungarian']),
    'MX': ('Mexico', ['Mexico', 'Mexican']),
    'BR': ('Brazil', ['Brazil', 'Brazilian']),
    'AR': ('Argentina', ['Argentina', 'Argentine', 'Argentinian']),
    'CL': ('Chile', ['Chile', 'Chilean']),
    'CO': ('Colombia', ['Colombia', 'Colombian']),
    'PE': ('Peru', ['Peru', 'Peruvian']),
    'EC': ('Ecuador', ['Ecuador', 'Ecuadorian']),
}
DEFAULT_COUNTRY = "PH"

def _country_expr(words):
    """OpenAlex search text for 'mentions this country': (Philippines OR Philippine OR \"United States\" ...)."""
    return "(" + " OR ".join(f'"{w}"' if " " in w else w for w in words) + ")"

@tools_bp.route("/api/topics/local")
@login_required
def topics_local():
    """Local check: how many papers on this topic mention a country (default: the Philippines) or have authors at institutions there.
    Param country = an ISO code from _COUNTRIES (for example PH, US, IN). Uses OpenAlex only (the one source that can filter by author country)."""
    subject = request.args.get("subject", "").strip()[:200]
    focus   = request.args.get("focus", "").strip()[:200]
    kw      = request.args.get("kw", "").strip()[:200]
    code    = (request.args.get("country") or DEFAULT_COUNTRY).strip().upper()
    if not subject:
        return jsonify({"error": "Enter a subject first"}), 400
    if code not in _COUNTRIES:
        return jsonify({"error": "That country is not on the list."}), 400
    cname, cwords = _COUNTRIES[code]
    cexpr = _country_expr(cwords)
    try:
        f = read_filters(request.args.get)
        subj_terms = _dedupe_terms(_specific(_safe_terms(subject)) or _safe_terms(subject))[:6]
        terms = _dedupe_terms(subj_terms + _specific(_safe_terms(focus)) + _specific(_safe_terms(kw)))[:10]
        expr, subj_expr = " AND ".join(terms), " AND ".join(subj_terms)
        jobs = {
            "mentions": (_oa_filter(f, f"{expr} AND {cexpr}"), 5, "cited_by_count:desc"),
            "authors":  (_oa_filter(f, expr, f"authorships.countries:{code}"), 3, "cited_by_count:desc"),
            "topic":    (_oa_filter(f, expr), 1, None),
        }
        if terms != subj_terms:
            jobs["subject_mentions"] = (_oa_filter(f, f"{subj_expr} AND {cexpr}"), 1, None)
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = {k: ex.submit(_oa_query, *v) for k, v in jobs.items()}
            res = {k: fu.result() for k, fu in futs.items()}
        examples, seen = [], set()
        for work in (res["mentions"][1] + res["authors"][1]):
            p = format_paper(work)
            k = (p.get("doi") or p.get("title") or "").lower()
            if k and k not in seen:
                seen.add(k)
                examples.append(_pick(p))
        mentions, authors = res["mentions"][0], res["authors"][0]
        if mentions is None and authors is None:
            return jsonify({"error": "Could not reach OpenAlex for the local check. Try again."}), 502
        return jsonify({
            "country": {"code": code, "name": cname},
            "terms": terms, "topic_total": res["topic"][0],
            "mentions": mentions, "authors": authors,
            "subject_mentions": (res.get("subject_mentions") or (None, []))[0],
            "examples": examples[:5],
            "filtered": bool(f["year_from"] or f["year_to"] or f["oa_only"]),
        })
    except Exception:
        return _fail("Local check")

@tools_bp.route("/api/topics/gaps", methods=["POST"])
@login_required
def topics_gaps():
    """Second step of the Topic Finder. Body (JSON): subject, forms (the sub-topic names) and papers (the papers the first step returned).
    Reads the Discussion / Conclusion / Limitations of the open-access ones and returns research-gap sentences from the full text.
    This is the slowest part, so the page asks for it AFTER it has already shown the papers and sub-topics."""
    data = request.get_json(silent=True) or {}
    subject = str(data.get("subject") or "").strip()[:200]
    subj_terms = _dedupe_terms(_specific(_safe_terms(subject)) or _safe_terms(subject))[:6]
    forms = [x[:80] for x in (data.get("forms") or [])[:15] if isinstance(x, str)]
    papers = []
    for p in (data.get("papers") or [])[:40]:
        if not isinstance(p, dict):
            continue
        cp = _client_paper(p)
        doi = _re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (cp.get("doi") or "").strip())
        if _DOI_OK.match(doi):
            papers.append(cp)
    try:
        cands, used = topicfinder.fulltext_gaps(papers, subj_terms, forms, max_papers=8, budget=14.0)
        return jsonify({"gaps": topicfinder.pick_gaps(cands, 8), "used": used})
    except Exception:
        return _fail("Full-text gap search")

@tools_bp.route("/api/topics")
@login_required
def topics():
    """Params: subject (required), focus, kw + shared filters (sources, year_from, year_to, oa, sort),
    fulltext = 1 | 0 (read the Discussion/Conclusion of open-access papers for research gaps; default 1).
    ptype = all | review | original (judged from the title). sort = balanced (default: recent + well cited) | cited | recent | match.
    Returns: results (papers), ideas (recurring sub-topics + trend), gaps (research-gap sentences)."""
    subject = request.args.get("subject", "").strip()[:200]
    focus   = request.args.get("focus", "").strip()[:200]
    kw      = request.args.get("kw", "").strip()[:200]
    if not subject:
        return jsonify({"error": "Enter a field or subject area first"}), 400
    try:
        f = read_filters(request.args.get)
        subj_terms = _dedupe_terms(_specific(_safe_terms(subject)) or _safe_terms(subject))[:6]
        broad = _broad_for(subject)
        # filler words ("of", "effect") are not topics: they are not searched and not counted as required words
        focus_terms0 = _specific(_safe_terms(focus))
        kw_terms0 = _specific(_safe_terms(kw))
        terms = _dedupe_terms(subj_terms + focus_terms0 + kw_terms0)[:10]
        full_query = " ".join(terms)
        raw_sort = request.args.get("sort") or "balanced"
        balanced = raw_sort == "balanced"                            # read_filters turns unknown sorts into "cited"
        ptype = request.args.get("ptype", "all")
        ptype = ptype if ptype in ("review", "original") else "all"
        this_year = datetime.date.today().year
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")

        low = {x.lower() for x in subj_terms}
        focus_terms = [x for x in _dedupe_terms(focus_terms0) if x.lower() not in low][:4]
        kw_terms    = [x for x in _dedupe_terms(kw_terms0) if x.lower() not in low
                       and x.lower() not in {y.lower() for y in focus_terms}][:4]
        extra_terms = focus_terms + kw_terms

        # first pool: the full query (plus its 'recent' side for the recommended order, so new papers are not missed)
        jobs = [(full_query, pool_f["sort"])]
        if balanced:
            jobs.append((full_query, "recent"))
        if ptype == "review":                                        # reviews rarely rank high on their own words: ask for them
            rq = " ".join(subj_terms + focus_terms + ["review"])
            jobs.append((rq, pool_f["sort"]))
            if balanced:
                jobs.append((rq, "recent"))
        # how many papers (OpenAlex) contain ALL the words, with the same filters: the "topic size"
        size_total = None
        count_ex = ThreadPoolExecutor(max_workers=2)
        _cf = _oa_filter(f, " AND ".join(terms))
        count_fut = count_ex.submit(_oa_query, _cf) if "openalex" in f["sources"] else None
        years_fut = count_ex.submit(_oa_years, _cf) if "openalex" in f["sources"] else None
        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            first = list(ex.map(lambda j: search_papers(j[0], 1, dict(f, sort=j[1]), per_source=25)[0], jobs))
        if count_fut is not None:
            try:
                size_total = count_fut.result(timeout=8)[0]
            except Exception:
                size_total = None
        year_rows = None
        if years_fut is not None:
            try:
                year_rows = years_fut.result(timeout=4)
            except Exception:
                year_rows = None
        count_ex.shutdown(wait=False)
        papers, seen = [], set()
        for batch in first:
            for p in batch:
                k = (p.get("doi") or p.get("title") or "").lower()
                if k not in seen and _on_topic(p, subj_terms):
                    seen.add(k)
                    papers.append(p)
        strict_n = len(papers)
        if strict_n < 12 and extra_terms:
            # Too few papers match every word: search the subject with each extra group on its own
            # (subject + focus, subject + keywords, subject) so a paper about "bioethanol flammability"
            # is found even when nothing covers focus AND keywords together.
            queries = [q for q in (
                " ".join(subj_terms + focus_terms) if focus_terms else "",
                " ".join(subj_terms + kw_terms) if kw_terms else "",
                " ".join(subj_terms)) if q and q != full_query]
            with ThreadPoolExecutor(max_workers=3) as ex:
                batches = list(ex.map(lambda q: search_papers(q, 1, pool_f, per_source=25)[0], queries))
            have = {(p.get("doi") or p.get("title") or "").lower() for p in papers}
            for batch in batches:
                for p in batch:
                    k = (p.get("doi") or p.get("title") or "").lower()
                    if k not in have and _on_topic(p, subj_terms):
                        have.add(k)
                        papers.append(p)
        papers = apply_filters_and_sort(papers, f, full_query)
        if ptype != "all":
            papers = [p for p in papers if _paper_kind(p) == ptype]
        if balanced:
            terms_b = _safe_terms(full_query)
            papers.sort(key=lambda p: _balanced_key(p, terms_b, this_year), reverse=True)
        papers = papers[:120]
        _pubmed_abstracts(papers[:60])                              # PubMed sends no abstract; ideas need text
        for p in papers:
            p["_matched"] = topicfinder.matched_terms(p, extra_terms)
        if extra_terms and f["sort"] == "match":
            papers.sort(key=lambda p: len(p["_matched"]), reverse=True)   # stable: keeps the earlier order inside a group
        cov = topicfinder.coverage(papers, extra_terms)
        papers = papers[:60]
        for p in papers:
            p["_methods"] = topicfinder.detect_methods(p)
        methods = topicfinder.method_summary(papers)
        bars = _year_bars(year_rows, this_year)
        years = {"bars": bars, "basis": "all"} if bars else None
        if not years:                                                # OpenAlex not used or no answer: count the papers we loaded
            cnt = {}
            for p in papers:
                y = to_int(p.get("year"))
                if y:
                    cnt[y] = cnt.get(y, 0) + 1
            bars = _year_bars([{"year": y, "count": c} for y, c in cnt.items()], this_year)
            years = {"bars": bars, "basis": "loaded"} if bars else None

        ideas = topicfinder.extract_ideas(papers, subj_terms + extra_terms, prefer_specific=bool(broad))   # words the student typed are not suggested back
        forms = [i["topic"] for i in ideas]
        cands = []
        for p in papers:
            for c in topicfinder.extract_gaps(p.get("abstract") or "", p, subj_terms, forms):
                c["from"] = "abstract"
                cands.append(c)
        ft_used = 0
        want_ft = False   # full-text reading is paused together with Europe PMC
        if want_ft:
            ft_cands, ft_used = topicfinder.fulltext_gaps(papers, subj_terms, forms)
            cands += ft_cands
        # when the page asks for the fast version it fetches the full-text gaps in a second step (/api/topics/gaps)
        ft_pending = False
        gaps = topicfinder.pick_gaps(cands, 8)

        msg = ""
        if len(papers) < 4:
            msg = "Not many papers matched. Try a broader subject, fewer focus words, or more sources."
        elif cov and cov["all"] == 0:
            msg = ("No paper found mentions all of: " + ", ".join(extra_terms) + ". This combination may be unstudied "
                   "(a possible research gap) or worded differently. Papers matching the most words are shown first.")
        return jsonify({
            "results": [dict(_pick(p, abstract_chars=260), matched=p.get("_matched", []), kind=_paper_kind(p), methods=p.get("_methods", []),
                             abstract_full=(p.get("abstract") or "")[:1200],
                             missing=[x for x in extra_terms if x not in p.get("_matched", [])]) for p in papers[:60]],
            "size": _size_info(size_total), "fulltext_pending": ft_pending,
            "ptype": ptype, "sort": raw_sort,
            "broad": {"term": subject, "suggestions": broad} if broad else None,
            "coverage": cov, "extra_terms": extra_terms,
            "ideas": ideas, "gaps": gaps, "methods": methods, "years": years, "gap_types": [{"key": k, "label": l} for k, l in topicfinder.GAP_TYPES],
            "relaxed": len(papers) > strict_n, "analysed": len(papers), "fulltext_used": ft_used,
            "subject": subject, "query": full_query, "terms": terms, "message": msg,
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
        })
    except Exception:
        return _fail("Topic search")

@tools_bp.route("/topic-generator")
@login_required
def topic_generator():
    return render_template("topic_finder.html")
