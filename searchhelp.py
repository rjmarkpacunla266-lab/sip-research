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
