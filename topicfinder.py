"""topicfinder.py — helpers for the Topic Finder (and a reusable open-access full-text fetcher).

No AI. Everything is rule based:
  * extract_ideas()   -> recurring sub-topics (key phrases) found across the papers, with a trend
  * extract_gaps()    -> "further research is needed / little is known / remains unclear" sentences
  * fulltext_gaps()   -> the same, but read from the Discussion / Conclusion / Limitations sections of
                         open-access papers (Europe PMC full text) instead of the abstract only
  * get_oa_fulltext() -> reusable: DOI -> {"pmcid", "license", "sections": {...}} for any tool
"""
import math
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import xml.etree.ElementTree as ET

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

# ───────────────────────── words ─────────────────────────
STOP = set("""a about above after again against all also am an and any are as at be because been before being below between both
but by can could did do does doing down during each either else few for from further had has have having he her here hers him his
how however i if in into is it its itself just may me might more most much must my neither no nor not of off on once only or other
our ours out over own per same she should so some such than that the their theirs them then there these they this those through
thus to too under until up upon us very via was we were what when where whether which while who whom whose why will with within
without would yet you your among across along around toward towards amongst versus vs et al""".split())

# Words that carry no topic by themselves. They are trimmed from the edges of a phrase and never stand alone.
GENERIC = set("""study studies analysis analyses result results effect effects impact impacts review reviews approach approaches
based using use used uses role roles new novel evidence research case cases data model models method methods development
application applications assessment evaluation overview perspective perspectives systematic meta paper article work findings
finding level levels high low higher lower different various several important significant potential current recent global
increased increasing increase decrease decreased associated association related relationship relationships factor factors
review literature investigation investigate investigated examine examined show shows showed shown found demonstrate
demonstrated suggest suggests suggested propose proposed present presents presented provide provides provided reported
report reports aim aims objective objectives background conclusion conclusions introduction methods purpose
patients patient participants sample samples group groups total number numbers rate rates year years time long short
large small major main key well good better best due need needs future available existing performance
overall scale scales needed required lack known unknown unclear remains remain specific general common recently often first second third one two three type types
including include includes included field fields system systems advance advances challenge challenges emerging emerge emerges framework frameworks
dynamic dynamics mechanism mechanisms strategy strategies opportunity opportunities trend trends progress insight insights understanding
perspective topic topics issue issues aspect aspects feature features process processes variety range approach tool tools technique techniques
knowledge science sciences technology technologies theory theories concept concepts principle principles basic fundamental modern traditional
increasing growing rapid rapidly significantly relevant related various wide broad complex multiple diverse comprehensive critical essential
alternative alternatives sustainable sustainability property properties conventional improved improvement enhanced efficient efficiency effective
potential promising novel advanced innovative industrial commercial practical future present current recent""".split())

# ───────────────────────── small text helpers ─────────────────────────
def _norm(text):
    t = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in t if not unicodedata.combining(c)).lower()

def _stem(w):
    if w.endswith("ies") and len(w) > 5:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and not w.endswith("us") and len(w) > 4:
        return w[:-1]
    return w

def _clean(text):
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", t).replace("\\", " ")
    return re.sub(r"\s+", " ", t).strip()

_ABBR = re.compile(r"\b(et al|e\.g|i\.e|vs|fig|figs|approx|cf|dr|prof|u\.s|u\.k|eq|resp|etc)\.", re.I)

def split_sentences(text):
    t = _ABBR.sub(lambda m: m.group(0)[:-1] + "\u2024", _clean(text))
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", t)
    return [p.replace("\u2024", ".").strip() for p in parts if p.strip()]

def _year(p):
    try:
        return int(p.get("year"))
    except (TypeError, ValueError):
        return None

def paper_ref(p):
    """The few fields the page needs to cite a paper."""
    return {"title": p.get("title"), "year": p.get("year"), "doi": p.get("doi"), "oa_url": p.get("oa_url"),
            "is_oa": p.get("is_oa"), "citations": p.get("citations") or 0, "journal": p.get("journal"),
            "authors": (p.get("authors") or [])[:3], "data_source": p.get("data_source"),
            "openalex_id": p.get("openalex_id"), "apa_reference": p.get("apa_reference")}


# ═════════════════════════════════════════════════════════════════════
# 1. TOPIC IDEAS  (recurring sub-topics)
# ═════════════════════════════════════════════════════════════════════
def _runs(text):
    """Split text into runs of consecutive content words (stop words / punctuation / numbers break a run)."""
    runs = []
    for seg in re.split(r"[^a-z0-9\- ]+", _norm(text)):
        cur = []
        for tok in seg.split():
            tok = tok.strip("-")
            if len(tok) < 3 or tok in STOP or tok.isdigit() or re.fullmatch(r"[\d\-]+", tok):
                if cur:
                    runs.append(cur)
                cur = []
            else:
                cur.append(tok)
        if cur:
            runs.append(cur)
    return runs

def _phrases(text):
    """-> {stem_key: surface_form} for every 1-3 word phrase in the text."""
    out = {}
    for run in _runs(text):
        while run and run[0] in GENERIC:
            run = run[1:]
        while run and run[-1] in GENERIC:
            run = run[:-1]
        if not run:
            continue
        if len(run) > 7:                                  # a very long run is usually a list; keep it bounded
            run = run[:7]
        for n in (1, 2, 3):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if g[0] in GENERIC or g[-1] in GENERIC:
                    continue
                if n == 1 and (len(g[0]) < 5):
                    continue
                key = " ".join(_stem(w) for w in g)
                out.setdefault(key, " ".join(g))
    return out

def _contains(long_key, short_key):
    L, S = long_key.split(), short_key.split()
    return len(S) < len(L) and any(L[i:i + len(S)] == S for i in range(len(L) - len(S) + 1))

def extract_ideas(papers, subject_terms, limit=12, prefer_specific=False):
    """Recurring sub-topics across `papers`. Each idea: topic, papers (count), in_titles, trend, examples."""
    N = len(papers)
    if N < 4:
        return []
    subj = {_stem(w) for t in subject_terms for w in _norm(t).split()}
    subj_pre = {w[:5] for w in subj if len(w) >= 5}            # 'biology' also hides 'biological', 'biologist'
    def _is_subj(tok):
        return tok in subj or (len(tok) >= 5 and tok[:5] in subj_pre)
    years = [y for y in (_year(p) for p in papers) if y]
    ymax = max(years) if years else None
    recent_cut = (ymax - 3) if ymax else None
    base_recent = (sum(1 for y in years if y >= recent_cut) / len(years)) if years else 0

    df, title_df, surf = Counter(), Counter(), defaultdict(Counter)
    where = defaultdict(list)                              # key -> papers containing it
    for p in papers:
        title, abstract = p.get("title") or "", p.get("abstract") or ""
        in_t = _phrases(title)
        in_all = dict(_phrases(abstract))
        in_all.update(in_t)
        for key, form in in_all.items():
            df[key] += 1
            surf[key][form] += 1
            where[key].append(p)
        for key in in_t:
            title_df[key] += 1

    min_df = 2 if N < 25 else 3
    cands = []
    for key, d in df.items():
        toks = key.split()
        if all(_is_subj(t) for t in toks):                 # just the subject again
            continue
        if len(toks) == 1 and d < min_df + 1 and title_df[key] < 2:   # one-word ideas must be clearly recurring
            continue
        if prefer_specific and len(toks) == 1 and d < 0.5 * N:          # broad subject: lone words are rarely a real topic
            continue
        if d < min_df or d > 0.8 * N:                      # too rare, or so common it is just the subject
            continue
        score = d * (1 + 0.6 * title_df[key] / d) * (1 + (0.6 if prefer_specific else 0.25) * (len(toks) - 1))
        cands.append((score, key, d))
    cands.sort(key=lambda c: (-c[0], c[1]))

    chosen, seen_sets = [], set()
    for score, key, d in cands:
        sig = frozenset(key.split())
        if sig in seen_sets:
            continue
        seen_sets.add(sig)
        chosen.append((score, key, d))
        if len(chosen) >= limit * 4:
            break
    # a shorter phrase is dropped when a longer one that contains it is nearly as frequent
    # ("coral bleaching event" covers "coral bleaching"); otherwise both stay
    final = [(sc, k, d) for sc, k, d in chosen
             if not any(_contains(k2, k) and d2 >= 0.6 * d for _, k2, d2 in chosen)]

    ideas = []
    for score, key, d in final[:limit]:
        ps = where[key]
        ys = [y for y in (_year(p) for p in ps) if y]
        rec = sum(1 for y in ys if recent_cut and y >= recent_cut)
        trend = "steady"
        if recent_cut and ys and base_recent:
            ratio = (rec / len(ys)) / base_recent
            if rec >= 2 and ratio >= 1.4:
                trend = "rising"
            elif ratio <= 0.6 and len(ys) >= 3:
                trend = "established"
        ex = sorted(ps, key=lambda p: p.get("citations") or 0, reverse=True)[:2]
        ideas.append({
            "topic": surf[key].most_common(1)[0][0],
            "papers": d, "in_titles": title_df[key],
            "trend": trend, "recent": rec,
            "examples": [paper_ref(p) for p in ex],
        })
    return ideas


# ═════════════════════════════════════════════════════════════════════
# 2. RESEARCH GAPS  (sentences)
# ═════════════════════════════════════════════════════════════════════
_GAP_RULES = [
    (5, r"\b(?:further|future|additional|more|continued)\s+(?:\w+\s+)?(?:research|studies|study|work|investigations?|experiments?|trials?|"
        r"attention|evidence)\s+(?:is|are|will\s+be|would\s+be|should|must|needs?|could|can|may|might|to|on)\b"),
    (5, r"\bremains?\s+(?:largely\s+|still\s+|poorly\s+|currently\s+|rather\s+|relatively\s+)?(?:unclear|unknown|unexplored|understudied|"
        r"unresolved|controversial|debated|uncertain|poorly\s+understood|to\s+be\s+(?:determined|explored|investigated|elucidated|established|addressed))"),
    (5, r"\b(?:little|few|scarce)\s+(?:is\s+known|studies|research|attention|evidence|data|work)\b"),
    (4, r"\b(?:has|have)\s+(?:rarely|seldom|not\s+(?:yet\s+|been\s+)?(?:been\s+)?)\s*(?:\w+ed|\w+n)\b"),
    (4, r"\b(?:lack\s+of|limited\s+(?:evidence|data|research|understanding|knowledge|information|studies)|insufficient\s+(?:evidence|data)|scarcity\s+of)\b"),
    (4, r"\b(?:understudied|under-studied|underexplored|under-explored|unexplored|knowledge\s+gaps?|research\s+gaps?|gaps?\s+in\s+(?:our|the)\s+)"),
    (3, r"\b(?:remains?|remain)\s+(?:a\s+)?(?:challenges?|open\s+questions?|open\s+problems?)\b"),
    (3, r"\b(?:it\s+is\s+(?:not\s+)?(?:yet\s+)?(?:clear|known|understood))\b|\bnot\s+(?:yet\s+)?(?:fully\s+)?understood\b"),
]
_GAP_RE = [(w, re.compile(p, re.I)) for w, p in _GAP_RULES]
_GAP_NEG = re.compile(r"\bno\s+(?:further|more|additional)\b|\bnot\s+(?:be\s+)?(?:necessary|required)\b|\bfurther\s+(?:\w+\s+)?(?:analysis|reading)\b", re.I)
_LEAD = re.compile(r"^\W*(?:however|moreover|furthermore|therefore|thus|hence|finally|overall|in\s+addition|nevertheless|nonetheless|"
                   r"in\s+conclusion|in\s+summary|additionally|also|but|and|yet),?\s+", re.I)

# a sentence that describes what THIS paper does ("The study includes a bibliometric analysis to ... identify knowledge gaps")
# is not a gap the authors found, even though it contains gap words
_SELF = re.compile(r"\b(?:this|the)\s+(?:present\s+|current\s+)?(?:study|paper|review|article|work|research|chapter)\s+(?:\w+ly\s+)?"
                   r"(?:aims?|includes?|examines?|provides?|presents?|reviews?|explores?|investigates?|identifies|highlights?|discusses|"
                   r"focuses|analy[sz]es?|seeks?|offers?|summari[sz]es?|addresses|covers?|uses?|employs?|conducts?|outlines?)\b"
                   r"|\b(?:to|and)\s+(?:identify|highlight|reveal|uncover)\s+(?:the\s+)?(?:current\s+)?(?:knowledge|research)\s+gaps\b", re.I)

# what KIND of gap a sentence states (the page offers these as filters)
GAP_TYPES = [("unknown", "Unclear / unknown"), ("lacking", "Little data / understudied"), ("future", "Needs more research")]
_GT_UNKNOWN = re.compile(r"\bremains?\s+(?:largely\s+|still\s+|poorly\s+|currently\s+|rather\s+|relatively\s+)?(?:unclear|unknown|unresolved|controversial|debated|uncertain|poorly\s+understood|to\s+be\s+(?:determined|elucidated|established))"
                         r"|\bis\s+(?:little|not)\s+(?:yet\s+)?(?:known|clear|understood)\b|\bit\s+is\s+(?:not\s+)?(?:yet\s+)?(?:clear|known|understood)\b|\bnot\s+(?:yet\s+)?(?:fully\s+)?understood\b|\bopen\s+questions?\b", re.I)
_GT_LACKING = re.compile(r"\b(?:lack\s+of|limited\s+(?:evidence|data|research|understanding|knowledge|information|studies)|insufficient|scarc\w+|"
                         r"(?:few|little|scarce)\s+(?:studies|research|attention|evidence|data|work)|understudied|under-studied|underexplored|under-explored|unexplored|"
                         r"(?:has|have)\s+(?:rarely|seldom|not\s+(?:yet\s+|been\s+)?(?:been\s+)?))", re.I)

def gap_type(text):
    if _GT_UNKNOWN.search(text): return "unknown"
    if _GT_LACKING.search(text): return "lacking"
    return "future"

# research METHODS that show up in a paper's title/abstract (plain word matching, no AI)
METHODS = [
    ("Survey / questionnaire",       r"\b(?:survey|surveys|surveyed|questionnaires?)\b"),
    ("Interviews / focus groups",    r"\binterviews?\b|\bfocus\s+groups?\b"),
    ("Case study",                   r"\bcase\s+stud(?:y|ies)\b"),
    ("Experiment / lab work",        r"\bexperiments?\b|\bexperimental(?:ly)?\b|\blaborator(?:y|ies)\b|\blab-scale\b|\bbench-scale\b|\bin\s+vitro\b"),
    ("Field study / sampling",       r"\bfield\s+(?:study|studies|survey|surveys|work|trials?|experiments?|measurements?|sampling)\b|\bsamples?\s+were\s+(?:collected|taken)\b|\bin\s+situ\b"),
    ("Computer simulation / model",  r"\bsimulat\w+|\bnumerical(?:ly)?\s+(?:model|stud|analy|simulat)\w*|\bcomputational\s+(?:model|fluid)\w*|\bfinite[-\s]element\b|\bCFD\b|\bmathematical\s+model\w*"),
    ("Machine learning",             r"\bmachine[-\s]learning\b|\bdeep[-\s]learning\b|\bneural\s+networks?\b|\brandom\s+forests?\b|\bsupport\s+vector\b|\bgradient\s+boost\w*"),
    ("Statistics / regression",      r"\bregression\b|\bANOVA\b|\bcorrelation(?:s)?\s+analys\w+|\bmultivariate\b|\bstructural\s+equation\b|\bstatistical(?:ly)?\s+(?:analys|model|test)\w*"),
    ("Systematic / literature review", r"\bsystematic\s+review\b|\bmeta-?analys[ie]s\b|\bscoping\s+review\b|\bliterature\s+review\b|\bbibliometric\b"),
    ("Clinical / cohort study",      r"\brandomi[sz]ed\b|\bclinical\s+trials?\b|\bcohort\b|\bcross-sectional\b|\bcase-control\b"),
    ("Qualitative analysis",         r"\bqualitative\b|\bthematic\s+analysis\b|\bgrounded\s+theory\b|\bethnograph\w+"),
    ("Chemical / molecular analysis",r"\bchromatograph\w*|\bGC-?MS\b|\bHPLC\b|\bspectroscop\w+|\bspectromet\w+|\bPCR\b|\bsequencing\b|\bmicroscop\w+"),
    ("Life-cycle / cost analysis",   r"\blife[-\s]cycle\s+(?:assessment|analysis)\b|\bLCA\b|\btechno-?economic\b|\bcost-benefit\b"),
    ("Remote sensing / GIS",         r"\bremote\s+sensing\b|\bGIS\b|\bsatellite\b|\bgeospatial\b"),
]
_METHOD_RE = [(n, re.compile(p, re.I)) for n, p in METHODS]

def detect_methods(p):
    t = ((p.get("title") or "") + ". " + (p.get("abstract") or ""))
    return [n for n, rx in _METHOD_RE if rx.search(t)]

def method_summary(papers, limit=10):
    """-> [{name, papers}] most common first. Methods seen in only one paper are hidden when there are enough papers."""
    cnt = {}
    for p in papers:
        for m in p.get("_methods") or []:
            cnt[m] = cnt.get(m, 0) + 1
    floor = 2 if len(papers) >= 12 else 1
    rows = sorted(((n, c) for n, c in cnt.items() if c >= floor), key=lambda x: -x[1])
    return [{"name": n, "papers": c} for n, c in rows[:limit]]


def _gap_score(sent):
    if _GAP_NEG.search(sent) or _SELF.search(sent):
        return 0
    return sum(w for w, rx in _GAP_RE if rx.search(sent))

def extract_gaps(text, paper, subject_terms, idea_forms=(), section_bonus=0):
    """Gap sentences from one block of text. -> list of {text, score, paper}"""
    from engine import term_in
    out = []
    title = paper.get("title") or ""
    on_title = any(term_in(title, t) for t in subject_terms)
    for s in split_sentences(text):
        n = len(s.split())
        if s.endswith("?") or n < 6 or n > 55:
            continue
        sc = _gap_score(s)
        if sc < 4:
            continue
        mentions = any(term_in(s, t) for t in subject_terms)
        if not mentions and not on_title:
            continue
        sc += 1 if mentions else 0
        sc += 1 if any(term_in(s, f) for f in idea_forms) else 0
        sc += section_bonus
        if re.search(r"\b(?:we|our)\s+(?:found|show|showed|observed|measured|collected|used|conducted)\b", s, re.I):
            sc -= 3                                         # reports the paper's own method/result, not a gap
        clean = _tidy(_LEAD.sub("", s))
        clean = clean[:1].upper() + clean[1:]
        out.append({"text": clean, "score": sc, "paper": paper})
    return out

def pick_gaps(cands, limit=8):
    """Best first, one per paper first so a single paper cannot fill the list, no near-duplicates."""
    def rank(c):
        p = c["paper"]
        return c["score"] + 0.8 * math.log10(1 + (p.get("citations") or 0)) + (0.3 if (_year(p) or 0) >= 2021 else 0)
    cands = sorted(cands, key=rank, reverse=True)
    first, rest, seen_p, seen_t = [], [], set(), set()
    for c in cands:
        key = re.sub(r"\W+", " ", c["text"].lower())[:70]
        if key in seen_t or c["score"] < 4:
            continue
        seen_t.add(key)
        pk = (c["paper"].get("doi") or c["paper"].get("title") or "").lower()
        (rest if pk in seen_p else first).append(c)
        seen_p.add(pk)
    out = (first + rest)[:limit]
    return [{"text": c["text"], "paper": paper_ref(c["paper"]), "from": c.get("from", "abstract"), "type": gap_type(c["text"])} for c in out]


# ═════════════════════════════════════════════════════════════════════
# 3. OPEN-ACCESS FULL TEXT  (Europe PMC)
# ═════════════════════════════════════════════════════════════════════
_CACHE, _CACHE_MAX = {}, 200
_SEC_WANT = re.compile(r"discussion|conclusion|limitation|future|perspective|outlook|strengths|implications|concluding", re.I)

def _doi(p):
    d = (p.get("doi") or "").strip().lower()
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", d)

def _epmc_pmcid(doi, timeout=8):
    r = requests.get(f"{EPMC}/search", timeout=timeout,
                     params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite", "pageSize": 1})
    if not r.ok:
        return None
    hits = (r.json().get("resultList") or {}).get("result") or []
    if hits and hits[0].get("pmcid") and hits[0].get("isOpenAccess") == "Y":
        return hits[0]["pmcid"]
    return None

def _text_of(el):
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()

_PH_X, _PH_F = "\u27e6X\u27e7", "\u27e6F\u27e7"     # placeholders: figure/table reference, formula

def _para_text(el):
    """Paragraph text without citation markers. Figure/table/formula references become a placeholder so the
    sentence can be rejected later (a sentence that says 'as shown in [figure]' means nothing alone)."""
    out = [el.text or ""]
    def walk(e):
        tag = e.tag.split("}")[-1] if isinstance(e.tag, str) else ""
        if tag == "xref":
            out.append("" if e.get("ref-type") == "bibr" else _PH_X)
        elif tag in ("fig", "table-wrap", "supplementary-material"):
            out.append(_PH_X)
        elif tag in ("inline-formula", "disp-formula"):
            out.append(_PH_F)
        elif tag == "sup" and re.fullmatch(r"[\d,\s\u2013\u2212-]*", "".join(e.itertext())):
            pass                                            # superscript reference numbers
        else:
            out.append(e.text or "")
            for c in e:
                walk(c)
        out.append(e.tail or "")
    for c in el:
        walk(c)
    return _tidy("".join(out))

def _tidy(t):
    t = re.sub(r"\(\s*(?:[,;:\u2013\u2212-]\s*)*\)|\[\s*(?:[,;:\u2013\u2212-]\s*)*\]", "", t)   # brackets left empty (or holding only ; , -) by removed citations
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    return t.strip()

def _walk_secs(parent, path, out):
    for sec in parent.findall("sec"):
        tnode = sec.find("title")
        title = _text_of(tnode) if tnode is not None else ""
        here = path + [title] if title else path
        paras = [_para_text(p) for p in sec.findall("p")]
        key = " > ".join(here)
        if key and any(paras):
            out[key] = (out.get(key, "") + " " + " ".join(paras)).strip()
        _walk_secs(sec, here, out)

def parse_jats(xml_bytes):
    """JATS XML -> {"license": url or "", "sections": {"Results > Effect of pH": text, ...}}.
    Keys are the section PATH, so a sub-section can be judged by the section it sits in."""
    root = ET.fromstring(xml_bytes)
    lic = ""
    for el in root.iter("license"):
        for k, v in el.attrib.items():
            if k.endswith("href"):
                lic = v
        if lic:
            break
    sections = {}
    body = root.find(".//body")
    if body is not None:
        _walk_secs(body, [], sections)
    return {"license": lic, "sections": sections}

def get_oa_fulltext(doi, timeout=10):
    """Open-access full text for a DOI via Europe PMC, or None. Cached in memory.
    -> {"pmcid", "license", "sections": {"Introduction": "...", "Results": "...", "Discussion": "..."}}"""
    return None   # Europe PMC full text is paused (publisher copyright)
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (doi or "").strip().lower())
    if not doi:
        return None
    if doi in _CACHE:
        return _CACHE[doi]
    res = None
    try:
        pmcid = _epmc_pmcid(doi, timeout)
        if pmcid:
            r = requests.get(f"{EPMC}/{pmcid}/fullTextXML", timeout=timeout)
            if r.ok and r.content:
                data = parse_jats(r.content)
                res = {"pmcid": pmcid, "license": data["license"], "sections": data["sections"]}
    except Exception:
        res = None
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[doi] = res
    return res

def fulltext_gaps(papers, subject_terms, idea_forms=(), max_papers=6, budget=9.0):
    """Gap sentences from the Discussion/Conclusion/Limitations of open-access papers.
    -> (candidates, number of papers whose full text was read). Any failure just means fewer results."""
    pool = [p for p in papers if p.get("is_oa") and _doi(p)]
    pool.sort(key=lambda p: p.get("citations") or 0, reverse=True)
    pool = pool[:max_papers]
    cands, used = [], 0
    if not pool:
        return cands, used
    ex = ThreadPoolExecutor(max_workers=min(6, len(pool)))
    jobs = {ex.submit(get_oa_fulltext, _doi(p)): p for p in pool}
    try:
        for fut in as_completed(jobs, timeout=budget):
            p = jobs[fut]
            try:
                ft = fut.result()
            except Exception:
                continue
            if not ft:
                continue
            used += 1
            for title, text in ft["sections"].items():
                if _SEC_WANT.search(title):
                    for c in extract_gaps(text, p, subject_terms, idea_forms, section_bonus=1):
                        c["from"] = "full text"
                        cands.append(c)
    except Exception:
        pass                                               # time budget reached: use what we have
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return cands, used


# ═════════════════════════════════════════════════════════════════════
# 4. COVERAGE OF THE USER'S EXTRA WORDS (focus / keywords)
# ═════════════════════════════════════════════════════════════════════
def _text(p):
    return f"{p.get('title') or ''} {p.get('abstract') or ''}"

def matched_terms(p, extra_terms):
    """Which of the focus/keyword words appear in this paper's title or abstract."""
    from engine import term_in
    t = _text(p)
    return [w for w in extra_terms if term_in(t, w)]

def coverage(papers, extra_terms):
    """How many of the papers mention each focus/keyword word, and how many mention ALL of them.
    All-zero means the combination is (nearly) unstudied in the sources searched: a possible research gap."""
    if not extra_terms:
        return None
    per = [{"term": w, "papers": sum(1 for p in papers if w in matched_terms(p, [w]))} for w in extra_terms]
    both = sum(1 for p in papers if len(matched_terms(p, extra_terms)) == len(extra_terms))
    return {"total": len(papers), "terms": per, "all": both}


def fetch_fulltexts(papers, budget=9.0, max_papers=8):
    """[(paper, fulltext dict)] for the open-access papers whose full text could be read within `budget` seconds."""
    pool = [p for p in papers if _doi(p)][:max_papers]
    got = []
    if not pool:
        return got
    ex = ThreadPoolExecutor(max_workers=min(8, len(pool)))
    jobs = {ex.submit(get_oa_fulltext, _doi(p)): p for p in pool}
    try:
        for fut in as_completed(jobs, timeout=budget):
            try:
                ft = fut.result()
            except Exception:
                continue
            if ft and ft.get("sections"):
                got.append((jobs[fut], ft))
    except Exception:
        pass                                               # time budget reached: use what has arrived
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return got
