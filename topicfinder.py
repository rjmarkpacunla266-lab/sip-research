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
overall scale scales specific general common recently often first second third one two three type types""".split())

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
            "authors": (p.get("authors") or [])[:3], "data_source": p.get("data_source")}


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

def extract_ideas(papers, subject_terms, limit=12):
    """Recurring sub-topics across `papers`. Each idea: topic, papers (count), in_titles, trend, examples."""
    N = len(papers)
    if N < 4:
        return []
    subj = {_stem(w) for t in subject_terms for w in _norm(t).split()}
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
        if all(t in subj for t in toks):                   # just the subject again
            continue
        if len(toks) == 1 and d < min_df + 1 and title_df[key] < 2:   # one-word ideas must be clearly recurring
            continue
        if d < min_df or d > 0.8 * N:                      # too rare, or so common it is just the subject
            continue
        score = d * (1 + 0.6 * title_df[key] / d) * (1 + 0.25 * (len(toks) - 1))
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

def _gap_score(sent):
    if _GAP_NEG.search(sent):
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
        if s.endswith("?") or n < 8 or n > 55:
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
        clean = _LEAD.sub("", s)
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
    return [{"text": c["text"], "paper": paper_ref(c["paper"]), "from": c.get("from", "abstract")} for c in out]


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

def parse_jats(xml_bytes):
    """JATS XML -> {"license": url or "", "sections": {section title: text}} (only the sections we care about
    plus Abstract/Results so other tools can use them too)."""
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
        for sec in body.iter("sec"):
            title = _text_of(sec.find("title")) if sec.find("title") is not None else ""
            paras = [_text_of(p) for p in sec.findall("p")]
            if title and paras:
                sections.setdefault(title, "")
                sections[title] += " " + " ".join(paras)
    return {"license": lic, "sections": {k: v.strip() for k, v in sections.items()}}

def get_oa_fulltext(doi, timeout=10):
    """Open-access full text for a DOI via Europe PMC, or None. Cached in memory.
    -> {"pmcid", "license", "sections": {"Introduction": "...", "Results": "...", "Discussion": "..."}}"""
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
