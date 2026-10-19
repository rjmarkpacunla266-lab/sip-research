"""routes/tools.py — Answer Finder, Source Tracer, Topic Finder.
All three use the shared engine (engine.py), so they support the same filters as Search Papers:
sources, years, Open Access and sort."""
import re as _re
import difflib
import math
import time
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor, wait
import requests
import xml.etree.ElementTree as ET
from flask import Blueprint, render_template, request, jsonify
from core import login_required, _safe_terms, OPENALEX_URL, format_paper
import topicfinder
from engine import (read_filters, search_papers, apply_filters_and_sort, term_in, relevance,
                    sort_date, SOURCE_LABELS, search_sources, merge_dedupe, to_int)

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
    papers = apply_filters_and_sort(merge_dedupe(allraw), dict(f, sort="cited"), " ".join(keywords))
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

_PH = "(Philippines OR Philippine OR Filipino OR Filipinos)"

@tools_bp.route("/api/topics/local")
@login_required
def topics_local():
    """Local check: how many papers on this topic mention the Philippines or have authors at Philippine institutions.
    Uses OpenAlex only (it is the one source that can filter by author country)."""
    subject = request.args.get("subject", "").strip()[:200]
    focus   = request.args.get("focus", "").strip()[:200]
    kw      = request.args.get("kw", "").strip()[:200]
    if not subject:
        return jsonify({"error": "Enter a subject first"}), 400
    try:
        f = read_filters(request.args.get)
        subj_terms = _dedupe_terms(_specific(_safe_terms(subject)) or _safe_terms(subject))[:6]
        terms = _dedupe_terms(subj_terms + _specific(_safe_terms(focus)) + _specific(_safe_terms(kw)))[:10]
        expr, subj_expr = " AND ".join(terms), " AND ".join(subj_terms)
        jobs = {
            "mentions": (_oa_filter(f, f"{expr} AND {_PH}"), 5, "cited_by_count:desc"),
            "authors":  (_oa_filter(f, expr, "authorships.countries:PH"), 3, "cited_by_count:desc"),
            "topic":    (_oa_filter(f, expr), 1, None),
        }
        if terms != subj_terms:
            jobs["subject_mentions"] = (_oa_filter(f, f"{subj_expr} AND {_PH}"), 1, None)
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
        want_ft = request.args.get("fulltext", "1") != "0"
        if want_ft:
            ft_cands, ft_used = topicfinder.fulltext_gaps(papers, subj_terms, forms)
            cands += ft_cands
        # when the page asks for the fast version it fetches the full-text gaps in a second step (/api/topics/gaps)
        ft_pending = (not want_ft) and any(p.get("is_oa") and p.get("doi") for p in papers)
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
