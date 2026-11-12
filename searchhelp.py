"""searchhelp.py — rule-based helpers for Search Papers (no AI):
   * synonym groups for common student topics
   * "Did you mean" spelling suggestions (edit-distance match against a word list)
   * ranking score: title words count more, plus citations, plus an optional bonus for newer papers
"""
import math
import re
from datetime import datetime
from difflib import get_close_matches

# Words that mean (almost) the same thing in student research. Single words first;
# a group may also hold a short phrase.
SYNONYM_GROUPS = [
    ["student", "learner", "pupil"],
    ["teenager", "adolescent", "youth"],
    ["teacher", "educator", "instructor"],
    ["smartphone", "cellphone", "mobile phone"],
    ["children", "kids", "child"],
    ["elderly", "older adults", "seniors"],
    ["obesity", "overweight"],
    ["poverty", "low income"],
    ["exam", "examination", "assessment"],
    ["grades", "academic performance"],
    ["college", "university"],
    ["pollution", "contamination"],
    ["gadget", "device"],
    ["gaming", "video games", "video game"],
    ["covid", "covid-19", "coronavirus"],
    ["women", "female"],
    ["mathematics", "math", "maths"],
    ["cheating", "academic dishonesty"],
    ["typhoon", "tropical cyclone"],
    # common name <-> scientific name (Google Scholar treats these as the same thing)
    ["tomato", "solanum lycopersicum"],
    ["rice", "oryza sativa"],
    ["corn", "maize", "zea mays"],
    ["banana", "musa"],
    ["coconut", "cocos nucifera"],
    ["mango", "mangifera indica"],
    ["eggplant", "solanum melongena", "brinjal"],
    ["moringa", "moringa oleifera"],
    ["peanut", "groundnut", "arachis hypogaea"],
    ["cassava", "manihot esculenta"],
    ["potato", "solanum tuberosum"],
    ["sugarcane", "saccharum officinarum"],
]

def _stem(w):
    w = (w or "").lower().strip()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w

_SYN = {}
for _g in SYNONYM_GROUPS:
    for _w in _g:
        _SYN.setdefault(_stem(_w), set()).update(x for x in _g if _stem(x) != _stem(_w))

def synonym_terms(term):
    """Other words/phrases that mean about the same as `term` (most useful first)."""
    return sorted(_SYN.get(_stem(term), []), key=lambda s: (" " in s, len(s)))

# Little words that should not be highlighted, counted as "missing", or used to rank papers.
STOPWORDS = set("""a an and or nor but of on in at to for from by with without within about as is are was were be been
being this that these those its it their his her than then into onto over under between among via vs versus
using use used the their there which who whom whose what when where why how do does did can could should would
may might will shall not no""".split())

def content_terms(terms):
    """The meaningful words of a search (drops 'the', 'of', 'vs', ...). If nothing is left, keep them all."""
    keep = [t for t in terms if t.lower() not in STOPWORDS]
    return keep or list(terms)

# Generic research words (study, effect, analysis, significant ...). They still count when a paper has them,
# but they are worth less, are not required to match, and are not highlighted.
WEAK_WORDS = set("""
study studies analysis analyses effect effects impact impacts role roles
factor factors method methods approach approaches case cases review reviews
evaluation evaluations assessment assessments examination examinations investigation investigations relationship relationships
correlation correlations association associations comparison comparisons process processes strategy strategies
technique techniques practice practices principle principles concept concepts perspective perspectives
aspect aspects issue issues challenge challenges problem problems pattern patterns
trend trends level levels type types form forms variable variables
component components outcome outcomes result results survey surveys experiment experiments
sample samples sampling questionnaire questionnaires interview interviews observation observations measurement
measurements instrument instruments procedure procedures methodology quantitative qualitative descriptive correlational
experimental exploratory comparative analytical empirical theoretical statistical systematic randomized longitudinal
cross-sectional retrospective prospective observational mixed-method mixed-methods respondent respondents participant participants
indicator indicators validation reliability validity criteria meta-analysis investigate investigates investigated
examining examine examines examined evaluate evaluates evaluated evaluating determine determines
determined determining assess assesses assessed assessing analyze analyzes analyzed analyzing
explore explores explored exploring identify identifies identified identifying describe describes
described describing compare compares compared comparing measure measures measured measuring
estimate estimates estimated estimating reviewed reviewing discuss discusses discussed discussing
present presents presented presenting propose proposes proposed proposing suggest suggests
suggested suggesting indicate indicates indicated indicating demonstrate demonstrates demonstrated demonstrating
reveal reveals revealed revealing show shows showed shown found find
finds report reports reported reporting conclude concludes concluded concluding observe
observes observed observing focus focuses focused focusing aim aims aimed
aiming provide provides provided providing highlight highlights highlighted highlighting consider
considers considered considering significant significantly different differently various varied new
novel effective effectively important importantly high highly low relatively recent
recently current currently general generally specific specifically potential potentially major
mainly primary primarily secondary overall particularly especially commonly widely frequently
often mostly largely increasingly substantial substantially considerable considerably relevant similar
similarly comparable appropriate appropriately possible possibly necessary necessarily essential consistent
consistently diverse multiple several numerous limited moderately meaningful simple basic
broad individual particular initial final previous further additional alternative existing
available practical preliminary successful unsuccessful reliable valid abstract background objective
objectives methodologies conclusion conclusions introduction purpose purposes keyword keywords finding
findings discussion discussions scope summary summaries overview overviews context rationale
significance contribution contributions implication implications limitation limitations recommendation recommendations hypothesis
hypotheses research interpretation interpretations evidence question questions statement statements future
acknowledgment acknowledgments reference references using based among between towards through
during regarding within across via versus according alongside beyond despite
including excluding involving following concerning related relating associated together rather
instead therefore however moreover furthermore consequently accordingly additionally alternatively respectively
approximately nearly almost also thus hence thereby wherein whereby whereas
whether although while unless since because irrespective regardless notwithstanding subsequent
prior followed derived obtained collected conducted performed applied utilized employed
""".split())

def _is_weak(w):
    w = (w or "").lower()
    return w in WEAK_WORDS or _stem(w) in WEAK_WORDS

def strong_terms(terms):
    """The words that say what the search is about. If a search is made only of generic
    words, those are used instead so it still works."""
    terms = list(terms)
    s = [t for t in terms if t.lower() not in STOPWORDS and not _is_weak(t)]
    return s or content_terms(terms)

def query_parts(query):
    """Split a search into its words. Words inside (brackets) are OPTIONAL: they still help
    ranking and are highlighted, but a paper does not have to contain them."""
    q = query or ""
    inside = re.findall(r"\(([^)]*)\)", q)
    main = re.sub(r"\([^)]*\)", " ", q)
    clean = lambda text: [t for t in (re.sub(r"[^\w\-]", "", x) for x in text.split()) if t]
    main_terms, opt_terms = clean(main), clean(" ".join(inside))
    if not main_terms:                      # nothing outside the brackets: then they are needed after all
        return {"all": opt_terms, "optional": []}
    return {"all": main_terms + opt_terms, "optional": [t for t in opt_terms if t not in main_terms]}

def search_query(query):
    """The words that must match: little words, generic words and (bracketed words) are dropped."""
    parts = query_parts(query)
    required = [t for t in parts["all"] if t not in parts["optional"]]
    return " ".join(strong_terms(required)) if required else (query or "")

# ── "Did you mean" ──────────────────────────────────────────────────────────
VOCAB = set("""
abstract academic accuracy achievement activity adolescent adolescents agriculture algorithm analysis anxiety
application approach assessment attendance attitude awareness behavior behaviour biodiversity biology
biotechnology bullying cancer chemical chemistry children classroom climate cognitive college communication
community comparison computer conservation consumption contamination creativity critical cultural curriculum
cyberbullying dengue depression development diabetes diet digital disaster discipline discrimination
distance disease earthquake ecology economic economics ecosystem education effect effectiveness efficiency
elderly electricity emotional employment energy engagement engineering english environment environmental
epidemic evaluation evidence exam examination exercise experience experiment factors family farmers
farming fertilizer financial flood food gadget gender geography global government grades graduate
happiness health healthcare higher history hospital household human hypothesis immune impact
implementation income industry infection inflation information innovation instruction intelligence
interest internet investigation language leadership learning lecture library lifestyle literacy
literature management marketing mathematics medicine memory mental method methodology microplastics
migration mobile motivation nursing nutrition obesity occupational online parent parental participation
patients pedagogy performance personality pesticide pharmacy philippines physical physics plastic
policy pollution population poverty practice prevention primary productivity professional psychology
quality quantitative qualitative questionnaire reading recycling relationship reliability renewable
research resilience resource review sanitation satisfaction scholarship school science secondary
security self-esteem skills sleep smartphone smoking social socioeconomic software statistics strategy
stress student students study success survey sustainable teacher teachers teaching technology
teenager teenagers tourism training transportation treatment typhoon university urban validity
vaccine violence virtual vocabulary waste water wellbeing women workplace writing youth
algae antibacterial antimicrobial antioxidant bacteria banana biodegradable biogas bioplastic bioethanol
calamansi charcoal coconut compost fermentation germination herbal hydroelectric mangrove medicinal microbiome
moringa nanoparticles organic photosynthesis probiotic protein seaweed solar vitamin wastewater
""".split()) | {w for g in SYNONYM_GROUPS for x in g for w in x.split() if len(w) > 3}

def _words(text):
    return re.findall(r"[A-Za-z]{4,}", (text or "").lower())

def suggest_query(query, papers=()):
    """Return a corrected query when a word looks misspelled, otherwise ''.
    A word counts as 'known' if it is in our word list or in any returned paper title."""
    known = set(VOCAB)
    for p in papers or ():
        known.update(_words(p.get("title")))
    pool = sorted(known)
    changed, out = False, []
    for tok in re.split(r"\s+", (query or "").strip()):
        core = re.sub(r"[^A-Za-z\-]", "", tok)
        low = core.lower()
        if len(low) < 5 or "-" in low or low in known or _stem(low) in known or low + "s" in known:
            out.append(tok); continue
        close = get_close_matches(low, pool, n=1, cutoff=0.84)
        if close and close[0] != low:
            fixed = close[0]
            if core[:1].isupper():
                fixed = fixed.capitalize()
            out.append(tok.replace(core, fixed)); changed = True
        else:
            out.append(tok)
    return " ".join(out) if changed else ""

# ── Ranking ─────────────────────────────────────────────────────────────────
def recency_bonus(year, now_year=None):
    """Extra points for newer papers (used only when 'Prefer newer papers' is on)."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return 0.0
    age = (now_year or datetime.now().year) - y
    if age <= 3:  return 3.0
    if age <= 6:  return 2.0
    if age <= 10: return 1.0
    return 0.0

def citation_points(citations):
    """Gentle credit for citations: 10 cites ≈ 1, 100 ≈ 2, 1000 ≈ 3, 10000 ≈ 4."""
    return math.log10(1 + max(0, citations or 0))

# ── Search operators (Google Scholar style, rule-based) ─────────────────────
#   "exact phrase"   a OR b   -word   intitle:word   author:Name
_TOKEN = re.compile(r'(-?)(?:(intitle|author):)?(?:"([^"]+)"|(\S+))')

def _norm_text(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip() + " "

def _words_of(text):
    return [w for w in re.sub(r"[^\w\-]+", " ", text or "").split() if w]

def parse_operators(query):
    """Split a search into plain words and operators. Searches without operators come back
    with active=False and are handled exactly as before."""
    q = (query or "").strip()
    plain, phrases, intitle, authors, excludes, groups = [], [], [], [], [], []
    last_plain, or_pending = None, False
    for m in _TOKEN.finditer(q):
        neg, field, quoted, word = bool(m.group(1)), m.group(2), m.group(3), m.group(4)
        text = ((quoted if quoted is not None else word) or "").strip()
        if not re.search(r"\w", text):
            continue
        if text == "OR" and quoted is None and not neg and not field:
            or_pending = last_plain is not None
            continue
        unit = {"text": " ".join(_words_of(text)), "phrase": quoted is not None}
        if not unit["text"]:
            continue
        if neg:
            excludes.append(unit); or_pending = False; continue
        if field == "author":
            authors.append(unit); or_pending = False; continue
        if field == "intitle":
            intitle.append(unit); or_pending = False; continue
        if or_pending and last_plain is not None:
            if groups and groups[-1][-1] is last_plain:
                groups[-1].append(unit)
            else:
                if last_plain in plain: plain.remove(last_plain)
                groups.append([last_plain, unit])
        else:
            plain.append(unit)
        last_plain, or_pending = unit, False
    for u in plain + intitle:
        if u["phrase"]:
            phrases.append(u["text"]) if u in plain else None
    base_words = [w for u in plain + intitle for w in _words_of(u["text"])]
    base = " ".join(base_words)
    if groups:
        variants = [(base + " " + alt["text"]).strip() for alt in groups[0][:3]]
    else:
        variants = [base] if base else []
    summary = []
    summary += ['phrase \u201c%s\u201d' % t for t in phrases]
    summary += ['title has \u201c%s\u201d' % u["text"] for u in intitle]
    summary += ['author %s' % u["text"] for u in authors]
    summary += ['one of: ' + " / ".join(u["text"] for u in g) for g in groups]
    summary += ['without %s' % u["text"] for u in excludes]
    return {"active": bool(phrases or intitle or authors or excludes or groups),
            "variants": variants, "phrases": phrases, "intitle": intitle, "authors": authors,
            "excludes": excludes, "groups": groups, "summary": summary}

def _has(text_n, unit_text):
    t = _norm_text(unit_text).strip()
    return (not t) or (" " + t) in text_n          # whole words / phrase; "fertilizer" also finds "fertilizers"

def apply_operators(papers, ops):
    """Keep only papers that obey the operators. Needs the real abstract, so run it before abstracts are hidden."""
    out = []
    for p in papers:
        title_n = _norm_text(p.get("title"))
        text_n = title_n + _norm_text(p.get("abstract")).lstrip()
        if any(not _has(text_n, ph) for ph in ops["phrases"]):
            continue
        if any(not _has(title_n, u["text"]) for u in ops["intitle"]):
            continue
        if any(_has(text_n, u["text"]) for u in ops["excludes"]):
            continue
        if any(not any(_has(text_n, alt["text"]) for alt in g) for g in ops["groups"]):
            continue
        if ops["authors"]:
            names = p.get("authors") or []
            names = [names] if isinstance(names, str) else names
            names_n = [_norm_text(a if isinstance(a, str) else (a or {}).get("name", "")) for a in names]
            if any(not any(all(_has(nm, t) for t in u["text"].split()) for nm in names_n) for u in ops["authors"]):
                continue
        out.append(p)
    return out
