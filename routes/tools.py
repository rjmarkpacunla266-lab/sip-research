"""routes/tools.py — Answer Finder, Source Tracer, Topic Finder.
All three use the shared engine (engine.py), so they support the same filters as Search Papers:
sources, years, Open Access and sort."""
import re as _re
import difflib
import math
import traceback
from concurrent.futures import ThreadPoolExecutor
import requests
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, jsonify
from core import login_required, _safe_terms
import topicfinder
from engine import (read_filters, search_papers, apply_filters_and_sort, term_in, relevance,
                    sort_date, SOURCE_LABELS, search_sources, merge_dedupe)

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
    (r"^(?:what (?:is|are) )?(?:the )?differences? between (.+?) and (.+)$",                    "difference"),
    (r"^(?:what (?:is|are) )?the (?:effects?|impacts?|consequences?) of (.+)$",                 "effect"),
    (r"^(?:effects?|impacts?|consequences?) of (.+)$",                                          "effect"),
    (r"^(?:what (?:is|are) )?the (?:causes?(?: of)?|reasons? (?:for|behind)|drivers? of) (.+)$", "causes"),
    (r"^what causes (.+)$",                             "causes"),
    (r"^how (?:does|do) (.+?) (?:affect|impact|influence|reduce|increase|improve|damage|harm|alter|promote|"
     r"prevent|inhibit|regulate|worsen|threaten|shape|cause)s? (.+)$",                          "how_affect"),
    (r"^what are (.+)$",                                "what_are"),
    (r"^what is (.+)$",                                 "what_is"),
    (r"^how does (.+) work$",                           "how_does"),
    (r"^how does (.+)$",                                "how_does"),
    (r"^why is (.+)$",                                  "why_is"),
    (r"^define (.+)$",                                  "define"),
]
_TWO = ("difference", "how_affect")        # templates that carry a second term

# words the user may have typed after the topic ("why is X important", "how does X works")
_TRAIL = {
    "how_does": r"\s+works?$",
    "why_is":   r"\s+(?:important|essential|critical|crucial|necessary|significant|useful|needed)$",
}

ANSWER_PER_SOURCE = 20   # papers asked from each source (more candidates for the weaker sources)
MIN_SCORE  = 5      # below this a sentence is not shown (better fewer answers than wrong ones)
BEST_SCORE = 7      # needed for the "Best answer" badge

def _clean_kw(kw):
    return _re.sub(r"^(?:the|a|an)\s+", "", (kw or "").strip())

def _extract_keyword(query):
    """-> (keyword, template, second keyword or None). The second keyword is used by 'difference' and 'how_affect'."""
    q = query.strip().lower().rstrip("?.! ")
    for pattern, ttype in ANSWER_TEMPLATES:
        m = _re.match(pattern, q)
        if not m:
            continue
        kw  = m.group(1)
        kw2 = m.group(2) if ttype in _TWO else None
        if ttype in _TRAIL:
            kw = _re.sub(_TRAIL[ttype], "", kw.strip())
        kw, kw2 = _clean_kw(kw), (_clean_kw(kw2) if kw2 else None)
        if kw and (kw2 or ttype not in _TWO):
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
# Each rule is (weight, regex). <K> is the topic, <K2> the second term. "pos" rules say the sentence really
# answers THIS kind of question (direction matters). "neg" rules catch look-alikes that answer something else.
# "boost" rules only add points when a "pos" rule already matched (generic cue words are never enough alone).
_HEAD = r"^\W*(?:(?:the|a|an)\s+)?<K>(?:\s*\([^)]{1,30}\))?\s*,?\s+"
# the topic must be the subject of the sentence (at most 2 modifier words before it)
_SUBJ = r"^\W*(?:(?:the|a|an)\s+)?(?:[\w\-]+\s+){0,2}?<K>(?:\s+(?:use|usage|exposure|consumption|intake|deficiency|loss))?(?:\s*\([^)]{1,30}\))?\s*,?\s+"
_DEF_POS = [
    (7, _HEAD + r"(?:is|are)\s+(?:a|an|the|any|one|those|defined|known|considered|characteri[sz]ed|referred|said|"
                r"often|typically|generally|commonly|usually|simply|essentially|basically)\b"),
    (6, _HEAD + r"(?:refers?\s+to|means?|denotes?|describes?)\b"),
    (5, r"<K>.{0,30}?\b(?:is|are)\s+(?:defined|known|described|considered|characteri[sz]ed)\s+as\b"),
    (5, r"<K>.{0,30}?\b(?:can\s+be\s+defined\s+as|is\s+defined\s+by)\b"),
    # "Antibiotics are drugs that ..." - a noun phrase that is immediately explained (that / which / of / used ...)
    (5, _HEAD + r"(?:is|are)\s+(?!(?:a|an|the|any|one|those|defined|known|considered|referred|said|not|also|still|currently|"
                r"increasingly|likely|unlikely|expected|projected|predicted|being|been|\w+ly)\b)"
                r"(?:[\w\-]+\s+){0,5}?(?:that|which|of|used|made|composed|consisting|containing|produced|derived|capable|"
                r"responsible|involved|formed|found)\b"),
]
_DEF_BOOST = [
    (1.5, r"\b(?:process|mechanism|phenomenon|movement|branch|type|form|class|group|ability|capacity)\s+(?:by\s+which|in\s+which|that|which|of|to)\b"),
]
_DEF_NEG = [
    (6, r"<K>\s+(?:is|are)\s+(?:likely|expected|projected|predicted|unlikely|thought|believed|estimated|set|going|able|not|still|currently|increasingly)\b"),
    (4, r"\b(?:topic|buzzword|debate|discussion)\b"),
    (5, r"\b(?:is|are)\s+(?:an?\s+)?(?:\w+\s+)?(?:ongoing|growing|pressing|serious|major|significant|important)\s+(?:\w+\s+)?(?:topic|issue|challenge|concern|problem|threat|area|field)\b"),
    (3, r"\b(?:universal|common|widespread|ubiquitous)\s+phenomenon\b|\bin\s+a\s+(?:broad|wide)\s+(?:variety|range)\b"),
    # property lists: "X is a, it is b, it is c"
    (6, r"\b(?:it|they)\s+(?:is|are)\b.*\b(?:it|they)\s+(?:is|are)\b"),
]
_NOT_BE = r"\b(?!\s+(?:be|been)\b)"

_OUT = (r"(?:causes?|leads?\s+to|results?\s+in|increases?|decreases?|reduces?|improves?|impairs?|affects?|impacts?|threatens?|"
        r"exacerbates?|worsens?|enhances?|induces?|promotes?|inhibits?|damages?|harms?|lowers?|raises?|contributes?\s+to|"
        r"(?:is|are)\s+associated\s+with)")

_RULES = {
    "what_is":  {"pos": _DEF_POS, "neg": _DEF_NEG, "boost": _DEF_BOOST},
    "define":   {"pos": _DEF_POS, "neg": _DEF_NEG, "boost": _DEF_BOOST},
    "what_are": {"pos": _DEF_POS + [
                    (4, r"<K>\s+(?:include|includes|comprise|comprises|consist\s+of|encompass)\b"),
                    (3, r"\b(?:types?|kinds?|classes|categories|examples)\s+of\s+<K>")],
                 "neg": _DEF_NEG, "boost": _DEF_BOOST},
    "causes": {
        "pos": [
            # "K is mainly caused by X" - the sentence must name the cause (something after "by")
            (6, r"<K>.{0,60}?\b(?:caused|driven|triggered|fuell?ed|attributed|exacerbated|explained)\s+"
                r"(?:(?:mainly|primarily|largely|mostly|partly|chiefly)\s+)?(?:by|to)\s+\w+"),
            # "the main cause of K is X"
            (6, r"\b(?:cause|causes|driver|drivers|reason|reasons|source|sources|contributor|contributors|factor|factors)\s+"
                r"(?:of|for|behind)\s+(?:\w+\s+){0,2}?<K>\s+(?:is|are|was|were|include|includes|being|lies|can\s+be|may\s+be)\s+\w+"),
            (5, r"<K>.{0,60}?\b(?:results?\s+from|stems?\s+from|arises?\s+from|originates?\s+from|due\s+to|because\s+of|"
                r"owing\s+to|attributable\s+to|emerges?\s+(?:from|due\s+to))\s+\w+"),
            # "X causes K"
            (6, r"\b\w+(?:\s+\w+){0,5}?\s+(?:causes?(?!\s+of\b)|drives?|triggers?|fuels?|contributes?\s+to|leads?\s+to|"
                r"is\s+responsible\s+for|are\s+responsible\s+for|accounts?\s+for)\s+(?:\w+\s+){0,3}?<K>"),
        ],
        "neg": [
            # the topic is the thing doing the causing / being discussed for its effects, not the thing being caused
            (6, r"\b(?:effects?|impacts?|consequences?|threats?|risks?|implications?)\s+(?:of|from|on)\s+(?:\w+\s+){0,2}?<K>"),
            (6, r"^\W*(?:the\s+)?<K>\s+(?:is\s+(?:likely|expected|projected|predicted)\s+to|will|may|might|can|could|has|have|"
                r"leads?\s+to|causes?|results?\s+in|affects?|impacts?|threatens?|exacerbates?|increases?|reduces?)" + _NOT_BE),
        ],
    },
    "effect": {
        "pos": [
            # "<K> reduces / leads to / is associated with <something>" - the topic is the subject, an outcome verb follows
            (6, _SUBJ + r"(?:\w+\s+){0,12}?\b" + _OUT + r"\s+\w+"),
            # "the (negative) effects of <K> on <something>" / "the effects of <K> include ..."
            (5, r"\b(?:effects?|impacts?|consequences?)\s+of\s+(?:\w+\s+){0,2}?<K>\s+on\s+\w+"),
            (5, r"\b(?:effects?|consequences?)\s+of\s+(?:\w+\s+){0,2}?<K>\s+(?:include|includes)\b"),
            (4, r"\b(?:as\s+a\s+result\s+of|consequence\s+of)\s+(?:\w+\s+){0,2}?<K>"),
        ],
        "neg": [
            (3, r"<K>.{0,40}?\b(?:is|are)\s+(?:mainly\s+|primarily\s+)?caused\s+by\b"),
            # "has become a critical area of research" - says the topic is studied, not what it does
            (5, r"\barea\s+of\s+(?:research|study|interest|concern)\b|\b(?:has|have)\s+(?:become|surged|emerged|gained|attracted|received)\b"
                r"|\b(?:has|have)\s+been\s+(?:\w+\s+)?(?:studied|investigated|examined|debated|explored)\b"),
        ],
    },
    "how_affect": {
        "pos": [
            (6, r"<K>.{0,80}?\b(?:affect|impact|influenc|alter|increas|reduc|improv|damag|harm|promot|prevent|inhibit|regulat|"
                r"lead|contribut|caus|threat|worsen|exacerbat|mitigat|shap)\w*\b.{0,80}?<K2>"),
            (6, r"\b(?:effects?|impacts?)\s+of\s+(?:\w+\s+){0,2}?<K>\s+on\s+(?:\w+\s+){0,3}?<K2>"),
            (5, r"<K2>.{0,60}?\b(?:affected|impacted|influenced|threatened|harmed|damaged)\s+by\s+(?:\w+\s+){0,2}?<K>"),
        ],
        "neg": [],
    },
    "how_does": {
        # the topic must be the subject AND do/perform something; generic words like "process" only add to a real match
        "pos": [
            (5, _SUBJ + r"(?:\w+\s+){0,3}?\b(?:works?|functions?|operates?|occurs?|proceeds?|happens?|takes?\s+place|detects?|"
                r"recogni[sz]es?|responds?|converts?|transforms?|produces?|synthesi[sz]es?|binds?|uses?|captures?|activates?|"
                r"cuts?|repairs?|fights?|identifies?|destroys?|protects?)\b"),
            (5, r"\b(?:mechanisms?|process(?:es)?|pathways?)\s+(?:by\s+which|through\s+which|in\s+which|of)\s+(?:\w+\s+){0,2}?<K>"),
            (5, _SUBJ + r"(?:\w+\s+){0,6}?\b(?:is|are)\s+(?:a|an|the)\s+(?:\w+\s+){0,2}?(?:mechanism|process|pathway)\b"),
        ],
        "boost": [
            (1.5, r"\b(?:by|through|via)\s+(?:\w+\s+){0,2}\w+ing\b"),
            (1.5, r"\b(?:mechanisms?|steps?|stages?|pathways?)\b"),
        ],
        "neg": [],
    },
    "why_is": {
        # needs the topic itself to be called important / essential, or its benefits / importance / role to be named
        "pos": [
            (5, _SUBJ + r"(?:\w+\s+){0,3}?\b(?:is|are|plays?|provides?)\s+(?:\w+\s+){0,2}?(?:essential|crucial|vital|critical|important|"
                r"necessary|indispensable|key|central|fundamental)\b"),
            (5, r"\b(?:importance|benefits?|role|significance)\s+of\s+(?:\w+\s+){0,2}?<K>\b"),
        ],
        "boost": [
            (2, r"\bbecause\b|\bdue\s+to\b|\bso\s+that\b|\bsince\s+(?!\d{4}\b|then\b|the\s+\d)"),
            (1, r"\bprotects?\b|\bsupports?\b|\bprevents?\b|\bregulates?\b|\breduces?\b"),
        ],
        "neg": [],
    },
    "difference": {
        # the two terms must be on opposite sides of a real contrast word, in either order
        "pos": [
            (7, r"<K>.{0,80}?\b(?:whereas|while|unlike|versus|vs\.?|compared\s+(?:to|with)|differs?\s+from|differ\s+in|in\s+contrast\s+(?:to|with)|"
                r"rather\s+than)\b.{0,80}?<K2>"),
            (7, r"<K2>.{0,80}?\b(?:whereas|while|unlike|versus|vs\.?|compared\s+(?:to|with)|differs?\s+from|differ\s+in|in\s+contrast\s+(?:to|with)|"
                r"rather\s+than)\b.{0,80}?<K>"),
            (7, r"\bunlike\b.{0,40}?<K>.{0,70}?<K2>|\bunlike\b.{0,40}?<K2>.{0,70}?<K>"),
            (7, r"\b(?:differences?|distinctions?|contrasts?)\s+(?:between|among)\s+(?:\w+\s+){0,3}?(?:<K>.{0,25}?<K2>|<K2>.{0,25}?<K>)"),
        ],
        "neg": [],
        "need": 7,
    },
}

# a sentence that says the answer is NOT known is never an answer
_GAP = _re.compile(r"\b(?:has|have)\s+not\s+been\s+(?:documented|studied|established|determined|investigated|clarified|elucidated)"
                   r"|\bremains?\s+(?:elusive|unclear|unknown|poorly\s+understood|controversial|largely\s+unknown|to\s+be\s+(?:determined|elucidated|established))"
                   r"|\blittle\s+is\s+known|\b(?:not|poorly)\s+(?:well\s+|fully\s+|yet\s+)?(?:understood|known|established|clear)\b"
                   r"|\bis\s+not\s+straightforward|\bunclear\b|\bunknown\b|\bremains?\s+a\s+(?:challenge|mystery|puzzle)", _re.I)

# words shown highlighted on the page so the reader sees WHY a sentence was picked
_CUES = {
    "causes":     ["caused by", "driven by", "triggered by", "due to", "results from", "stems from", "because of",
                   "causes of", "cause of", "drivers of", "contributes to", "leads to"],
    "effect":     ["effects of", "effect of", "impacts of", "impact of", "consequences of", "results in", "leads to",
                   "increases", "reduces", "affects"],
    "how_affect": ["affects", "affect", "impacts", "influences", "leads to", "increases", "reduces", "due to"],
    "what_is":    ["refers to", "defined as", "known as", "consists of", "means"],
    "what_are":   ["refers to", "defined as", "known as", "consists of", "include", "such as"],
    "define":     ["refers to", "defined as", "known as", "consists of", "means"],
    "how_does":   ["mechanism", "process", "through", "via"],
    "why_is":     ["because", "important", "essential", "crucial", "critical", "vital", "due to"],
    "difference": ["differ", "differs", "compared to", "compared with", "whereas", "in contrast", "unlike", "rather than"],
}

# Rejected outright: the sentence is about the paper itself, or only makes sense with the sentence before it.
_HARD = _re.compile(r"\b(?:we|our|ours)\b|\bhere,?\s+we\b"
                    r"|\b(?:this|the\s+present|the\s+current|the\s+proposed)\s+(?:study|paper|article|review|work|chapter|thesis|research|survey|approach|method)\b"
                    r"|\b(?:framework|questionnaire|simulations?|methodology)\b"
                    # methods / purpose-of-the-paper sentences
                    r"|purpose\s+of\s+this|position\s+stand|systematic\s+search|\bt[\s-]?tests?\b|chi[\s-]?square|data\s+analysis"
                    r"|\b(?:was|were)\s+(?:conducted|performed|carried\s+out|searched|recruited|assigned|randomi[sz]ed|collected|included|excluded|measured|analy[sz]ed)\b"
                    r"|\bwere\s+compared\s+with\s+(?:matched|control|healthy|placebo)"
                    r"|\b(?:participants?|databases?|cohorts?)\b"
                    r"|\b(?:is|are)\s+(?:described|discussed|presented|reviewed|outlined|examined|explored|summari[sz]ed|addressed)\b(?!\s+as\b)", _re.I)
_LEAD = _re.compile(r"^\W*(?:finally|in\s+addition|additionally|moreover|furthermore|also|specifically|in\s+particular|importantly|notably)\s*,?\s+", _re.I)
_LABEL = _re.compile(r"^\W*(BACKGROUND|CONCLUSIONS?|RESULTS?|FINDINGS|INTRODUCTION|CONTEXT|DISCUSSION|SIGNIFICANCE|IMPORTANCE|SUMMARY|"
                     r"METHODS?|METHODOLOGY|OBJECTIVES?|PURPOSE|AIMS?|DESIGN|SETTING|PARTICIPANTS|MATERIALS(?:\s+AND\s+METHODS)?)\s*:\s*", _re.I)
_BAD_LABELS = {"METHOD", "METHODS", "METHODOLOGY", "OBJECTIVE", "OBJECTIVES", "PURPOSE", "AIM", "AIMS", "DESIGN", "SETTING",
               "PARTICIPANTS", "MATERIALS", "MATERIALS AND METHODS"}
_ANAPH = _re.compile(r"^\W*(?:these|this|those|such|it|they|their|its|however|moreover|furthermore|therefore|thus|also|"
                     r"additionally|consequently|hence|nevertheless|meanwhile|overall|but|and|yet)\b"
                     r"|\bthese\b|\bsuch\s+(?!as\b)\w+", _re.I)
_META = _re.compile(r"\b(?:literature|researchers?|authors?|studies|empirical(?:ly)?|systematic review|meta-analysis|relevant|review)\b", _re.I)

_ABBR = _re.compile(r"\b(et al|e\.g|i\.e|vs|fig|figs|approx|cf|dr|prof|u\.s|u\.k|eq|resp|etc)\.", _re.I)

def _clean_abstract(text):
    """Remove HTML/JATS tags and LaTeX leftovers such as \\latin{et al.}."""
    t = _re.sub(r"<[^>]+>", " ", text or "")
    t = _re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", t)
    t = t.replace("\\", " ")
    return _re.sub(r"\s+", " ", t).strip()

def _split_sentences(text):
    """Split an abstract into sentences without breaking on 'et al.', 'e.g.', 'U.S.' and the like.
    Section labels (BACKGROUND:, RESULTS: ...) are removed, and everything under METHODS / OBJECTIVE / AIM labels is skipped."""
    t = _ABBR.sub(lambda m: m.group(0)[:-1] + "\u2024", _clean_abstract(text))
    parts = _re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", t)
    out, skip = [], False
    for p in parts:
        p = p.replace("\u2024", ".").strip()
        if not p:
            continue
        m = _LABEL.match(p)
        if m:
            skip = _re.sub(r"\s+", " ", m.group(1).upper()) in _BAD_LABELS
            p = p[m.end():].strip()
        if skip or not p:
            continue
        out.append(p)
    return out

def _score_sentence(sent, kw, ttype, idx, kw2=None):
    """-> (score, number of direction-aware patterns that matched). A rejected sentence scores -99."""
    lead = bool(_LEAD.match(sent))
    if lead:
        sent = _LEAD.sub("", sent, count=1)          # "Finally, X is ..." -> "X is ..." (a small penalty below)
    m = _LABEL.match(sent)
    if m:
        if _re.sub(r"\s+", " ", m.group(1).upper()) in _BAD_LABELS:
            return -99, 0
        sent = sent[m.end():]
    if _HARD.search(sent) or _ANAPH.search(sent):
        return -99, 0
    rules = _RULES.get(ttype) or _RULES["what_is"]
    K, K2 = _kw_regex(kw), (_kw_regex(kw2) if kw2 else None)

    def build(pat):
        if "<K2>" in pat:
            return pat.replace("<K>", K).replace("<K2>", K2) if K2 else None
        return pat.replace("<K>", K)

    score = hits = 0
    for w, pat in rules["pos"]:
        p = build(pat)
        if p and _re.search(p, sent, _re.I):
            score += w
            hits += 1
    if rules.get("need") and score < rules["need"]:        # e.g. 'difference' needs a real contrast word
        return -99, 0
    for w, pat in rules.get("neg", []):
        p = build(pat)
        if p and _re.search(p, sent, _re.I):
            score -= w
    if hits:
        score += min(3, sum(w for w, pat in rules.get("boost", []) if (build(pat) and _re.search(build(pat), sent, _re.I))))
    if _GAP.search(sent):
        score -= 6
    if lead:
        score -= 1
    if _META.search(sent):
        score -= 3
    n = len(sent.split())
    if n > 55: score -= 2
    if n < 7: score -= 2
    if idx == 0:
        score += 1
    return score, hits

def _answer_candidates(paper, kw, ttype, kw2=None, per_paper=2, min_score=MIN_SCORE):
    abstract = paper.get("abstract") or ""
    if not abstract:
        return []
    cands = []
    for i, s in enumerate(_split_sentences(abstract)):
        n = len(s.split())
        if s.endswith("?") or n < 6 or n > 60:
            continue
        if ttype in _TWO:
            if not (kw2 and _kw_present(s, kw) and _kw_present(s, kw2)):
                continue
        elif not _kw_present(s, kw):
            continue
        score, hits = _score_sentence(s, kw, ttype, i, kw2)
        if hits and score >= min_score:                    # must really match the question type
            cands.append({"text": s, "score": score, "hits": hits, "paper": paper,
                          "tboost": 1 if _kw_present(paper.get("title") or "", kw) else 0})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]

_BOOST = {"causes": "causes", "effect": "effects", "how_does": "mechanism", "why_is": "importance"}

def _answer_query(kw, ttype, kw2):
    """The paper search is aimed at the question, not only at the topic."""
    if ttype in _TWO:
        return f"{kw} {kw2}"
    return f"{kw} {_BOOST[ttype]}" if ttype in _BOOST else kw

_EXTRA_QUERIES = {                       # extra wordings: review / overview papers usually state the definition outright
    "what_is":  ("overview", "review"),
    "what_are": ("overview", "review"),
    "define":   ("definition", "review"),
    "how_does": ("mechanism review", "pathway"),
    "why_is":   ("importance review", "role"),
    "causes":   ("causes review",),
    "effect":   ("effects review",),
}

def _answer_queries(kw, ttype, kw2):
    """The question's own search plus a couple of wordings that reach review / overview papers (max 3 searches)."""
    qs = [_answer_query(kw, ttype, kw2)]
    if ttype not in _TWO:
        for extra in _EXTRA_QUERIES.get(ttype, ()):
            if f"{kw} {extra}" not in qs:
                qs.append(f"{kw} {extra}")
    return qs[:3]

def _merge_keep_abstract(batches):
    """One entry per paper across all searches. The most-cited copy is kept, but it borrows an abstract from another copy
    when it has none (otherwise a paper would be thrown away for having no sentences to read)."""
    best = {}
    for p in (p for b in batches for p in b):
        k = _paper_key(p)
        if not k:
            continue
        old = best.get(k)
        if old is None:
            best[k] = p
            continue
        keep, other = (p, old) if (p.get("citations") or 0) > (old.get("citations") or 0) else (old, p)
        if not keep.get("abstract") and other.get("abstract"):
            keep["abstract"] = other["abstract"]
        if other.get("is_oa") and not keep.get("is_oa"):
            keep["is_oa"], keep["oa_url"] = True, keep.get("oa_url") or other.get("oa_url")
        best[k] = keep
    return list(best.values())

def _collect(papers, kw, ttype, kw2, cands, seen, min_score):
    for p in papers:
        for c in _answer_candidates(p, kw, ttype, kw2, min_score=min_score):
            key = c["text"].lower()[:80]
            if key not in seen:
                seen.add(key)
                cands.append(c)

def _rank(c):
    """Sentence quality first; citations add a gentle (log-scaled) boost instead of only breaking ties."""
    return c["score"] + 0.5 * c.get("tboost", 0) + 1.2 * math.log10(1 + (c["paper"].get("citations") or 0))


def _paper_key(p):
    return (p.get("doi") or p.get("openalex_id") or p.get("title") or "").lower()

def _diversify(cands, n=5):
    """Best sentence of each paper first, so one paper cannot fill the list; second sentences only fill gaps."""
    first, rest, seen = [], [], set()
    for c in cands:
        k = _paper_key(c["paper"])
        (rest if k in seen else first).append(c)
        seen.add(k)
    return (first + rest)[:n]

def _src_key(p):
    ds = (p.get("data_source") or "").lower()
    for k in ("europe", "arxiv", "pubmed", "crossref"):
        if k in ds:
            return "europepmc" if k == "europe" else k
    return "openalex"

def _spread(cands, n=5):
    """Best answer from each source first, so arXiv / PubMed / Crossref / Europe PMC are not crowded out by one source.
    The chosen answers keep the order the list was already sorted in."""
    first, rest, seen_src = [], [], set()
    for c in _diversify(cands, len(cands)):
        k = _src_key(c["paper"])
        (rest if k in seen_src else first).append(c)
        seen_src.add(k)
    order = {id(c): i for i, c in enumerate(cands)}
    return sorted((first + rest)[:n], key=lambda c: order[id(c)])

def _source_stats(papers, cands):
    """Per source: papers found, papers that have an abstract, and usable answer sentences (for the page's info line)."""
    st = {}
    blank = lambda: {"papers": 0, "abstracts": 0, "answers": 0}
    for p in papers:
        x = st.setdefault(_src_key(p), blank())
        x["papers"] += 1
        x["abstracts"] += 1 if p.get("abstract") else 0
    for c in cands:
        st.setdefault(_src_key(c["paper"]), blank())["answers"] += 1
    return st

def _pubmed_abstracts(papers):
    """PubMed search results carry no abstract, so they could never give an answer sentence.
    Fetch the missing abstracts in ONE request and fill them in. Any failure is ignored (those papers just stay empty)."""
    need = {}
    for p in papers:
        oid = str(p.get("openalex_id") or "")
        if not p.get("abstract") and oid.startswith("pmid:") and oid[5:].isdigit():
            need[oid[5:]] = p
    ids = list(need)[:60]
    if not ids:
        return
    try:
        r = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", timeout=12,
                         params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"})
        if not r.ok:
            return
        for art in ET.fromstring(r.content).iter("PubmedArticle"):
            pmid = (art.findtext("MedlineCitation/PMID") or "").strip()
            text = " ".join(t for t in ("".join(a.itertext()).strip() for a in art.iter("AbstractText")) if t)
            if pmid in need and text:
                need[pmid]["abstract"] = text
    except Exception:
        return

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
            "message": "Try: What is [topic], What are [topic], What causes [topic], Effect of [topic], How does [topic] work, "
                       "How does [A] affect [B], Why is [topic] important, Define [topic], or Difference between [A] and [B]"
        }), 400
    try:
        f = read_filters(request.args.get)
        if request.args.get("sort") not in ("match", "cited", "recent"):
            f["sort"] = "match"
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")

        q = _answer_query(keyword, ttype, kw2)
        queries = _answer_queries(keyword, ttype, kw2)
        with ThreadPoolExecutor(max_workers=len(queries)) as ex:                 # several wordings at once: reviews and overviews
            batches = list(ex.map(lambda qq: search_papers(qq, 1, pool_f, per_source=ANSWER_PER_SOURCE)[0], queries))
        papers = _merge_keep_abstract(batches)
        _pubmed_abstracts(papers)
        all_papers = list(papers)
        cands, seen = [], set()
        _collect(papers, keyword, ttype, kw2, cands, seen, MIN_SCORE)
        searched = len(papers)
        if len(cands) < 3 and keyword not in queries and ttype not in _TWO:
            more, _ = search_papers(keyword, 1, pool_f, per_source=ANSWER_PER_SOURCE)      # fall back to the plain topic
            more = _merge_keep_abstract([more, all_papers])
            have = {_paper_key(p) for p in all_papers}
            more = [p for p in more if _paper_key(p) not in have]
            _pubmed_abstracts(more)
            _collect(more, keyword, ttype, kw2, cands, seen, MIN_SCORE)
            searched += len(more)
            all_papers += more

        if f["sort"] == "cited":
            cands.sort(key=lambda c: (c["paper"].get("citations") or 0, c["score"]), reverse=True)
        elif f["sort"] == "recent":
            cands.sort(key=lambda c: (sort_date(c["paper"]), c["score"]), reverse=True)
        else:
            cands.sort(key=_rank, reverse=True)
        top = _spread(cands, 5)
        best = 0 if (f["sort"] == "match" and top and ttype != "difference" and top[0]["score"] >= BEST_SCORE) else None
        return jsonify({
            "keyword":  keyword, "keyword2": kw2, "template": ttype, "query": query,
            "answers":  [c["text"] for c in top],
            "sources":  [_pick(c["paper"]) for c in top],          # same order as answers
            "cues":     _CUES.get(ttype, []),
            "best_index": best,
            "best_rank": round(_rank(top[best]), 2) if best is not None else None,
            "searched": searched,
            "source_stats": _source_stats(all_papers, cands),
            "ft_papers": [_pick(p) for p in _ft_pool(all_papers)],
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "message":  "" if top else "No clear answer found in the top papers. Try searching for papers instead.",
        })
    except Exception:
        return _fail("Answer search")

# ── Answers from the FULL TEXT of open-access papers ─────────────────
# Abstracts are short, so many questions find nothing. For open-access papers that Europe PMC / PubMed Central hold
# in full, the same sentence rules are run over the body of the paper, with a few extra rules because body text is noisier.
FT_MIN_SCORE = MIN_SCORE + 1          # a little stricter than abstracts (body text has more look-alike sentences)
FT_MAX_PAPERS = 8
_DOI_OK = _re.compile(r"^10\.\d{4,9}/[^\s\"<>]+$")
_FT_SKIP = _re.compile(r"method|material|experimental\s+(?:section|procedure|setup|design|detail)|procedure|protocol|reference|acknowledg|funding|conflict|supplement|"
                       r"appendix|availability|ethic|statistic|abbreviation|author|contribution|competing|disclosure", _re.I)
_FT_INTRO = _re.compile(r"introduc|background|overview|literature|related|state of the art|theor", _re.I)
_FT_RESULT = _re.compile(r"result|finding|discussion|conclusion|summary|implication|outcome", _re.I)
_FT_BAD = _re.compile(r"\u27e6|\b(?:Figs?\.?|Figures?|Tables?|Eq\.?|Equations?|Supplementary|Appendix)\s*[A-Z]?\d|\bet al\b|"
                      r"\bpp?\.\s*\d|\b(?:shown|presented|illustrated|depicted|listed|summari[sz]ed|reported|described|given|seen)\s+(?:in|by)\s*[.,;:)]", _re.I)
_DEF_TYPES = ("what_is", "what_are", "define", "how_does", "why_is")

def _ft_pool(papers, n=FT_MAX_PAPERS):
    """Open-access papers with a DOI, best candidates first (PubMed / Europe PMC records most often have full text)."""
    seen, out = set(), []
    for p in papers:
        doi = (p.get("doi") or "").strip().lower()
        if not (p.get("is_oa") and doi) or doi in seen:
            continue
        seen.add(doi)
        out.append(p)
    pref = lambda p: 0 if _src_key(p) in ("europepmc", "pubmed") else 1
    out.sort(key=lambda p: (pref(p), -(p.get("citations") or 0)))
    return out[:n]

def _ft_section(path, ttype):
    """-> (label shown to the reader, score bonus) or None when the section should not be used (methods, references...)."""
    parts = [x.strip() for x in path.split(">")]
    if any(_FT_SKIP.search(x) for x in parts):
        return None
    top = parts[0]
    bonus = 0
    if ttype in _DEF_TYPES and _FT_INTRO.search(top):
        bonus = 1
    elif ttype not in _DEF_TYPES and _FT_RESULT.search(top):
        bonus = 1
    label = _re.sub(r"^[\dIVXivx]+(?:\.\d+)*[.)]?\s+", "", top).strip() or top
    return label[:60], bonus

def _fulltext_candidates(paper, ft, kw, ttype, kw2=None, per_paper=2):
    cands = []
    for path, text in (ft.get("sections") or {}).items():
        info = _ft_section(path, ttype)
        if not info:
            continue
        label, bonus = info
        for s in _split_sentences(text):
            n = len(s.split())
            if s.endswith("?") or n < 8 or n > 50 or _FT_BAD.search(s):
                continue
            if ttype in _TWO:
                if not (kw2 and _kw_present(s, kw) and _kw_present(s, kw2)):
                    continue
            elif not _kw_present(s, kw):
                continue
            score, hits = _score_sentence(s, kw, ttype, 1, kw2)      # idx 1: no 'first sentence of an abstract' bonus
            if hits and score + bonus >= FT_MIN_SCORE:
                cands.append({"text": s, "score": score + bonus, "hits": hits, "paper": paper,
                              "section": label, "license": ft.get("license") or "",
                              "tboost": 1 if _kw_present(paper.get("title") or "", kw) else 0})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]

def _client_paper(p):
    """The page sends back the papers it was given; keep only plain, expected fields."""
    out = {}
    for k in ("title", "journal", "doi", "oa_url", "data_source", "apa_reference", "openalex_id", "date"):
        v = p.get(k)
        out[k] = v[:400] if isinstance(v, str) else None
    y = p.get("year")
    out["year"] = y if isinstance(y, (int, str)) and not isinstance(y, bool) else None
    c = p.get("citations")
    out["citations"] = c if isinstance(c, int) and not isinstance(c, bool) else 0
    out["authors"] = [a[:80] for a in (p.get("authors") or []) if isinstance(a, str)][:6]
    out["is_oa"] = True
    return out

@tools_bp.route("/api/answer/fulltext", methods=["POST"])
@login_required
def answer_fulltext():
    """Second step of the Answer Finder. Body (JSON): q (the question) and papers (the open-access papers the first step returned).
    Reads their full text (Europe PMC) and returns extra answer sentences with the section they came from."""
    data = request.get_json(silent=True) or {}
    query = str(data.get("q") or "").strip()[:300]
    keyword, ttype, kw2 = _extract_keyword(query)
    if not keyword:
        return jsonify({"error": "unsupported_template"}), 400
    papers = []
    for p in (data.get("papers") or [])[:FT_MAX_PAPERS]:
        if not isinstance(p, dict):
            continue
        cp = _client_paper(p)
        doi = _re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (cp.get("doi") or "").strip())
        if _DOI_OK.match(doi):
            papers.append(cp)
    try:
        reads = topicfinder.fetch_fulltexts(papers, budget=9.0, max_papers=FT_MAX_PAPERS)
        cands, seen = [], set()
        for paper, ft in reads:
            for c in _fulltext_candidates(paper, ft, keyword, ttype, kw2):
                key = c["text"].lower()[:80]
                if key not in seen:
                    seen.add(key)
                    cands.append(c)
        cands.sort(key=_rank, reverse=True)
        top = _diversify(cands, 5)
        return jsonify({
            "answers": [c["text"] for c in top],
            "sources": [_pick(c["paper"]) for c in top],
            "sections": [c["section"] for c in top],
            "licenses": [c["license"] for c in top],
            "cues": _CUES.get(ttype, []),
            "read": len(reads), "tried": len(papers),
            "best_rank": round(_rank(top[0]), 2) if top and ttype != "difference" and top[0]["score"] >= BEST_SCORE else None,
            "message": "" if top else ("No clear answer found in the full text of these papers." if reads else
                                       "Full text was not available for these papers."),
        })
    except Exception:
        return _fail("Full-text answer search")

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

def _trace_pool(quote, keywords, f):
    """Find candidate papers with THREE searches, not one: the distinctive words, the opening words of the quote
    (as a phrase) and the whole quote. Each source ranks its own results by relevance; a paper that comes out near the
    top of several searches is rewarded (rank bonus). Returns (papers, {paper key: rank bonus 0..1})."""
    words = quote.split()
    queries = []
    for q in (" ".join(keywords[:6]), " ".join(words[:14]), " ".join(words)[:220]):
        q = q.strip()
        if q and q not in queries:
            queries.append(q)
    pool_f = dict(f, sort="match")                       # relevance from each source, not "most cited first"
    def run(q):
        try:
            return search_sources(q, 1, pool_f, per_source=15, loose=True)[0]
        except Exception:
            return []
    with ThreadPoolExecutor(max_workers=3) as ex:
        batches = list(ex.map(run, queries))
    rank, allraw = {}, []
    for raw in batches:
        pos = {}
        for p in raw:
            k, src = _pkey(p), (p.get("data_source") or "")
            i = pos.get(src, 0); pos[src] = i + 1
            rank[k] = rank.get(k, 0.0) + 1.0 / (1 + i)   # 1st result of a source = 1, 2nd = 0.5, 3rd = 0.33 ...
        allraw.extend(raw)
    papers = apply_filters_and_sort(merge_dedupe(allraw), pool_f, "")
    return papers, {k: min(1.0, v / 1.5) for k, v in rank.items()}

def _title_overlap(title, quote):
    tw = [w for w in _re.findall(r"\b\w{4,}\b", (title or "").lower()) if w not in _STOPWORDS]
    return sum(1 for w in tw if term_in(quote, w)) / len(tw) if tw else 0.0

def _score_paper(paper, quote, keywords, rank_bonus=0.0):
    """-> (score 0..1, best sentence, wording similarity, share of keywords found in title/abstract, rank bonus)."""
    title, abstract = paper.get("title") or "", paper.get("abstract") or ""
    sim, sentence = _best_match(abstract, quote) if abstract else (0.0, "")
    text = title + " " + abstract
    cov = sum(1 for k in keywords if term_in(text, k)) / max(len(keywords), 1)
    tov = _title_overlap(title, quote)
    score = 0.30 * sim + 0.30 * cov + 0.10 * tov + 0.30 * rank_bonus
    return score, sentence, sim, cov, rank_bonus

def _confidence(score, sim, cov, rb=0.0):
    """A match only counts as 'found' when the words really overlap (or several searches agree), not just by chance."""
    if (score >= 0.7 and sim >= 0.6) or (score >= 0.65 and rb >= 0.9 and cov >= 0.6):
        return "high", True
    if score >= 0.45 and (cov >= 0.5 or rb >= 0.7):
        return "medium", True
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
        papers, ranks = _trace_pool(quote, keywords, f)

        scored = []
        for p in papers:
            score, sentence, sim, cov, rb = _score_paper(p, quote, keywords, ranks.get(_pkey(p), 0.0))
            row = _pick(p, abstract_chars=300)
            row["score"] = round(score, 4)
            row["sim"], row["cov"], row["rb"] = round(sim, 3), round(cov, 3), round(rb, 3)
            row["snippet"] = sentence if sim >= 0.25 and sentence else row.get("abstract", "")
            scored.append(row)
        scored.sort(key=lambda r: r["score"], reverse=True)

        top_score = scored[0]["score"] if scored else 0
        if scored:
            confidence, found = _confidence(top_score, scored[0]["sim"], scored[0]["cov"], scored[0]["rb"])
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

@tools_bp.route("/api/topics")
@login_required
def topics():
    """Params: subject (required), focus, kw + shared filters (sources, year_from, year_to, oa, sort),
    fulltext = 1 | 0 (read the Discussion/Conclusion of open-access papers for research gaps; default 1).
    Returns: results (papers), ideas (recurring sub-topics + trend), gaps (research-gap sentences)."""
    subject = request.args.get("subject", "").strip()[:200]
    focus   = request.args.get("focus", "").strip()[:200]
    kw      = request.args.get("kw", "").strip()[:200]
    if not subject:
        return jsonify({"error": "Enter a field or subject area first"}), 400
    try:
        f = read_filters(request.args.get)
        subj_terms = _dedupe_terms(_safe_terms(subject))[:6]
        terms = _dedupe_terms(_safe_terms(f"{subject} {focus} {kw}"))[:10]
        full_query = " ".join(terms)
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")

        low = {x.lower() for x in subj_terms}
        focus_terms = [x for x in _dedupe_terms(_safe_terms(focus)) if x.lower() not in low][:4]
        kw_terms    = [x for x in _dedupe_terms(_safe_terms(kw)) if x.lower() not in low
                       and x.lower() not in {y.lower() for y in focus_terms}][:4]
        extra_terms = focus_terms + kw_terms

        papers, _ = search_papers(full_query, 1, pool_f, per_source=25)
        papers = [p for p in papers if _on_topic(p, subj_terms)]
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
        papers = papers[:120]
        _pubmed_abstracts(papers[:60])                              # PubMed sends no abstract; ideas need text
        for p in papers:
            p["_matched"] = topicfinder.matched_terms(p, extra_terms)
        if extra_terms and f["sort"] == "match":
            papers.sort(key=lambda p: len(p["_matched"]), reverse=True)   # stable: keeps the earlier order inside a group
        cov = topicfinder.coverage(papers, extra_terms)
        papers = papers[:60]

        ideas = topicfinder.extract_ideas(papers, subj_terms)
        forms = [i["topic"] for i in ideas]
        cands = []
        for p in papers:
            for c in topicfinder.extract_gaps(p.get("abstract") or "", p, subj_terms, forms):
                c["from"] = "abstract"
                cands.append(c)
        ft_used = 0
        if request.args.get("fulltext", "1") != "0":
            ft_cands, ft_used = topicfinder.fulltext_gaps(papers, subj_terms, forms)
            cands += ft_cands
        gaps = topicfinder.pick_gaps(cands, 8)

        msg = ""
        if len(papers) < 4:
            msg = "Not many papers matched. Try a broader subject, fewer focus words, or more sources."
        elif cov and cov["all"] == 0:
            msg = ("No paper found mentions all of: " + ", ".join(extra_terms) + ". This combination may be unstudied "
                   "(a possible research gap) or worded differently. Papers matching the most words are shown first.")
        return jsonify({
            "results": [dict(_pick(p, abstract_chars=260), matched=p.get("_matched", []),
                             missing=[x for x in extra_terms if x not in p.get("_matched", [])]) for p in papers[:30]],
            "coverage": cov, "extra_terms": extra_terms,
            "ideas": ideas, "gaps": gaps,
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
