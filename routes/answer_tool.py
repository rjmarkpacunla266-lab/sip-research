"""routes/answer_tool.py: Answer Finder (/answer and /api/answer*). Edit this file for answer-finder changes only."""
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
# ANSWER FINDER
# ═════════════════════════════════════════════════════════════════════
ANSWER_TEMPLATES = [
    (r"^(?:what (?:is|are) )?(?:the )?(?:benefits?|advantages?|pros)(?: and (?:advantages?|benefits?))? (?:of|for|from) (.+)$",  "benefits"),
    (r"^(?:what (?:is|are) )?(?:the )?(?:risks?|dangers?|hazards?|disadvantages?|drawbacks?|limitations?|side[- ]effects?|cons)(?: and \w+)? (?:of|from|for|with) (.+)$", "risks"),
    (r"^(?:what (?:are|is) )?(?:some )?(?:examples?|types?|kinds?) of (.+)$",                    "examples"),
    (r"^(?:what (?:is|are) )?(?:the )?(?:ways?|strategies|methods|steps|measures|solutions) (?:to|for) (.+)$", "how_to"),
    (r"^how (?:to|can (?:we |i |you |people )?|do (?:we |i |you )|should (?:we |i |you ))(.+)$", "how_to"),
    (r"^(?:is|are|does|do|can|could) (.+?) (safe|effective|harmful|dangerous|beneficial|healthy|toxic|useful|sustainable|reliable)(?: to .+| for .+)?$", "is_adj"),
    (r"^(?:which is better,? )?(.+?) (?:vs\.?|versus) (.+)$",                                    "difference"),
    (r"^(?:which is better,? )(.+?) or (.+)$",                                                    "difference"),
    (r"^(?:compare|comparison of|comparing) (.+?) (?:and|with|to|vs\.?|versus) (.+)$",           "difference"),
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
_TWO = ("difference", "how_affect")        # templates that carry a second term that must also be in the sentence
_ADJ = {"is_adj"}                         # the second group is an adjective (safe, effective...), not a topic

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
        kw2 = m.group(2) if (ttype in _TWO or ttype in _ADJ) else None
        if ttype in _TRAIL:
            kw = _re.sub(_TRAIL[ttype], "", kw.strip())
        if ttype == "how_to":                      # "how to reduce plastic waste" -> topic "plastic waste"; "how can X be reduced" -> "X"
            kw = _re.split(r"\s+(?:be|is|are)\s+", kw.strip())[0]
            w = kw.split()
            if len(w) >= 2 and not w[-1].endswith("ly") and w[0] not in ("climate", "plastic"):
                kw = " ".join(w[1:])
        kw, kw2 = _clean_kw(kw), (_clean_kw(kw2) if kw2 else None)
        if kw and (kw2 or ttype not in _TWO):
            return kw, ttype, kw2
    return _loose_keyword(q)

# A question that fits no pattern is still answered, loosely: the content words are searched and the closest sentences shown
_Q_WORDS = set("""what whats how why when where who whom whose which does do did is are was were can could should would will shall
the a an of to in on for and or with by about from at as into than then that this these those it its be been being there their
tell me explain please kindly""".split())
def _loose_keyword(q):
    words = [w for w in _re.findall(r"[a-z0-9][a-z0-9\-']*", q.lower()) if w not in _Q_WORDS and len(w) > 2]
    if not words:
        return None, None, None
    return " ".join(words[:5]), "loose", None

def _kw_regex(kw):
    parts = [_re.escape(w) for w in kw.split()]
    return r"\b" + r"\s+".join(parts) + r"(?:s|es)?\b"

def _loose_present(sent, kw):
    """Loose questions: at least half of the content words (and never fewer than two) must be in the sentence."""
    words = kw.split()
    if len(words) < 2:
        return bool(words) and term_in(sent, words[0])
    need = max(2, (len(words) + 1) // 2)
    return sum(1 for w in words if term_in(sent, w)) >= need

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

_ADJ_RE = {   # words that show a sentence is talking about the quality asked about
    "safe": r"safe|safety|unsafe|adverse|tolerated|tolerability|toxicity|side\s+effects?|well[- ]tolerated|risks?",
    "effective": r"effective|effectiveness|efficacy|efficacious|ineffective|reduced|improved|significantly",
    "harmful": r"harmful|harm|harms|damage|damaging|adverse|toxic|detrimental|negative",
    "dangerous": r"dangerous|danger|hazard\w*|risks?|harmful|toxic|lethal",
    "beneficial": r"beneficial|benefits?|advantages?|improv\w+|positive|helpful",
    "healthy": r"healthy|health|benefits?|beneficial|nutritious",
    "toxic": r"toxic|toxicity|poisonous|harmful|adverse",
    "useful": r"useful|usefulness|utility|valuable|effective|applications?",
    "sustainable": r"sustainable|sustainability|renewable|long[- ]term|environmentally",
    "reliable": r"reliable|reliability|accurate|accuracy|robust|consistent",
}
_RULES = {
    "benefits": {
        "pos": [
            (5, r"\b(?:benefits?|advantages?)\s+of\s+(?:\w+\s+){0,2}?<K>\b"),
            (5, _SUBJ + r"(?:\w+\s+){0,4}?\b(?:provides?|offers?|improves?|enhances?|increases?|reduces?|lowers?|promotes?|supports?|helps?|enables?|"
                r"boosts?|protects?|prevents?|contributes?\s+to)\s+\w+"),
            (4, r"<K>.{0,80}?\b(?:benefits?|advantages?|beneficial|advantageous)\b|\b(?:benefits?|advantages?)\b.{0,60}?<K>"),
        ],
        "boost": [(2, r"\b(?:include|includes|such\s+as|namely|including)\b"), (1, r"\b(?:significantly|improved|reduced|enhanced)\b")],
        "neg": [(3, r"\b(?:drawbacks?|disadvantages?)\b")],
    },
    "risks": {
        "pos": [
            (5, r"\b(?:risks?|dangers?|hazards?|drawbacks?|disadvantages?|limitations?|side\s+effects?|adverse\s+effects?)\s+(?:of|from|associated\s+with|related\s+to|posed\s+by)\s+(?:\w+\s+){0,2}?<K>"),
            (5, r"<K>.{0,80}?\b(?:poses?|carries|increases\s+the\s+risk|is\s+associated\s+with\s+(?:an?\s+)?(?:increased|higher|elevated)|can\s+cause|may\s+cause|"
                r"can\s+lead\s+to|may\s+lead\s+to|can\s+result\s+in|may\s+result\s+in|is\s+(?:toxic|harmful|hazardous|dangerous|unsafe))\b"),
            (4, r"\b(?:risks?|drawbacks?|limitations?|disadvantages?|hazards?)\b.{0,60}?<K>|<K>.{0,60}?\b(?:risks?|drawbacks?|limitations?|disadvantages?|hazards?)\b"),
        ],
        "boost": [(1.5, r"\b(?:include|includes|such\s+as|including)\b")],
        "neg": [(3, r"\b(?:benefits?|advantages?)\s+of\b")],
    },
    "examples": {
        "pos": [
            (6, r"<K>\s*,?\s+(?:such\s+as|including|like)\s+\w+"),
            (6, r"<K>.{0,30}?\((?:e\.g\.?|such\s+as|including|for\s+example)[^)]{3,}\)"),
            (5, r"\b(?:examples?|instances?)\s+of\s+(?:\w+\s+){0,2}?<K>"),
            (4, r"<K>\s+(?:include|includes|are\s+(?:divided|classified|grouped)\s+into)\s+\w+"),
        ],
        "boost": [(1, r"\b(?:and|or)\s+\w+\b.*,")],
        "neg": [],
    },
    "how_to": {
        "pos": [
            (6, r"\b(?:strategies|measures|approaches|methods|ways|steps|practices|interventions|solutions|options)\s+(?:to|for|of)\s+(?:\w+\s+){0,3}?<K>"),
            (7, r"<K>.{0,70}?\b(?:can|could|may|should|must)\s+be\s+(?:\w+\s+)?(?:reduced|prevented|mitigated|minimi[sz]ed|improved|achieved|controlled|addressed|managed|"
                r"treated|solved|enhanced|increased|avoided|restored|protected)\b.{0,70}?\b(?:by|through|via|using|with)\b"),
            (5, r"\b(?:by|through|via)\s+(?:\w+\s+){0,2}?\w+ing\b.{0,70}?<K>"),
            (5, _SUBJ + r"(?:\w+\s+){0,3}?\b(?:can|could|may|should)\s+be\s+\w+ed\b"),
        ],
        "boost": [(1.5, r"\b(?:such\s+as|include|includes|including)\b")],
        "neg": [],
    },
    "is_adj": {
        "pos": [(6, r"<K>.{0,100}?\b(?:<A>)\b"), (5, r"\b(?:<A>)\b.{0,60}?<K>")],
        "boost": [(1.5, r"\b(?:studies|evidence|trials?)\s+(?:show|showed|suggest|suggested|indicate|found|demonstrate)\w*\b"),
                  (1, r"\b(?:significantly|generally|widely)\b")],
        "neg": [],
    },
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
    "benefits":   ["benefits of", "benefit", "advantages", "improves", "enhances", "provides", "reduces", "supports"],
    "risks":      ["risks of", "risk", "adverse", "side effects", "harmful", "toxic", "can cause", "may lead to", "drawbacks", "limitations"],
    "examples":   ["such as", "including", "include", "examples of", "e.g."],
    "how_to":     ["strategies", "measures", "can be reduced", "can be prevented", "by", "through", "via"],
    "is_adj":     ["safe", "safety", "effective", "efficacy", "harmful", "toxic", "beneficial", "adverse", "reliable"],
    "loose":      [],
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
    if ttype == "loose":                                     # no pattern: every clean sentence that has the words, no bonus for wording
        score = 5.0
        if _GAP.search(sent): score -= 6
        if _META.search(sent): score -= 3
        n = len(sent.split())
        if n > 55: score -= 2
        if n < 8: score -= 2
        return score, 1
    rules = _RULES.get(ttype) or _RULES["what_is"]
    K, K2 = _kw_regex(kw), (_kw_regex(kw2) if kw2 else None)

    adj = _ADJ_RE.get((kw2 or "").lower(), _re.escape(kw2 or "x")) if ttype in _ADJ else ""
    def build(pat):
        if "<A>" in pat:
            return pat.replace("<A>", adj).replace("<K>", K) if adj else None
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
    sents = _split_sentences(abstract)
    for i, s in enumerate(sents):
        n = len(s.split())
        if s.endswith("?") or n < 6 or n > 60:
            continue
        if ttype in _TWO:
            if not (kw2 and _kw_present(s, kw) and _kw_present(s, kw2)):
                continue
        elif not (_loose_present(s, kw) if ttype == "loose" else _kw_present(s, kw)):
            continue
        score, hits = _score_sentence(s, kw, ttype, i, kw2)
        if hits and score >= min_score:                    # must really match the question type
            cands.append({"text": s, "score": score, "hits": hits, "paper": paper,
                          "before": sents[i - 1][:320] if i > 0 else "", "after": sents[i + 1][:320] if i + 1 < len(sents) else "",
                          "tboost": 1 if _kw_present(paper.get("title") or "", kw) else 0})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]

_BOOST = {"causes": "causes", "effect": "effects", "how_does": "mechanism", "why_is": "importance", "benefits": "benefits",
          "risks": "risks", "examples": "types", "how_to": "strategies"}

def _answer_query(kw, ttype, kw2):
    """The paper search is aimed at the question, not only at the topic."""
    if ttype in _TWO or ttype in _ADJ:
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
    "benefits": ("benefits review",),
    "risks":    ("risks review", "adverse effects"),
    "examples": ("overview", "review"),
    "how_to":   ("review", "mitigation"),
    "is_adj":   ("review", "systematic review"),
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
            "message": "I could not find a topic in that. Try: What is [topic], What causes [topic], Effect of [topic], Benefits of [topic], "
                       "Risks of [topic], Examples of [topic], How to [do something], Is [topic] safe, How does [topic] work, "
                       "Why is [topic] important, Define [topic], or Difference between [A] and [B]"
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
            "contexts": [{"before": c.get("before", ""), "after": c.get("after", "")} for c in top],
            "understood": _understood(ttype, keyword, kw2),
            "loose": ttype == "loose",
            "cues":     _CUES.get(ttype, []),
            "best_index": best,
            "best_rank": round(_rank(top[best]), 2) if best is not None else None,
            "searched": searched,
            "seen": [k for k in (_paper_key(p) for p in all_papers) if k][:800],     # papers already read: "keep searching" skips them
            "next_page": 2, "sort": f["sort"],
            "source_stats": _source_stats(all_papers, cands),
            "ft_papers": [_pick(p) for p in _ft_pool(all_papers)],
            "sources_used": [SOURCE_LABELS[s] for s in f["sources"]],
            "message":  "" if top else "No clear answer found in the top papers. Try searching for papers instead.",
        })
    except Exception:
        return _fail("Answer search")

# how the question was read, and other ways to read the same topic (the page shows these so a wrong reading is one tap away)
_READ_AS = {"what_is": "What is", "what_are": "What are", "define": "Define", "causes": "What causes", "effect": "Effect of",
            "how_does": "How does", "why_is": "Why is", "benefits": "Benefits of", "risks": "Risks of", "examples": "Examples of",
            "how_to": "How to", "is_adj": "Is", "difference": "Difference between", "how_affect": "How does", "loose": "Keywords"}
_SWITCH = ["what_is", "causes", "effect", "benefits", "risks", "examples", "how_does", "why_is"]
def _understood(ttype, kw, kw2):
    if ttype == "loose":
        text = f"keywords: {kw}"
    elif ttype == "difference":
        text = f"difference between {kw} and {kw2}"
    elif ttype == "how_affect":
        text = f"how {kw} affects {kw2}"
    elif ttype == "is_adj":
        text = f"is {kw} {kw2}?"
    else:
        text = f"{_READ_AS.get(ttype, 'What is').lower()} {kw}"
    alts = []
    if ttype not in _TWO and ttype not in _ADJ:
        alts = [{"label": _READ_AS[k], "template": _READ_AS[k], "topic": kw} for k in _SWITCH if k != ttype and k != "how_does" and k != "why_is"]
        if kw.split()[-1:] and ttype != "loose":
            alts = alts[:5]
    return {"template": ttype, "text": text, "topic": kw, "alternatives": alts}

# ── Answers from the FULL TEXT of open-access papers ─────────────────
# Abstracts are short, so many questions find nothing. For open-access papers that Europe PMC / PubMed Central hold
# in full, the same sentence rules are run over the body of the paper, with a few extra rules because body text is noisier.
FT_MIN_SCORE = MIN_SCORE + 1          # a little stricter than abstracts (body text has more look-alike sentences)
FT_MAX_PAPERS = 8
_FT_SKIP = _re.compile(r"method|material|experimental\s+(?:section|procedure|setup|design|detail)|procedure|protocol|reference|acknowledg|funding|conflict|supplement|"
                       r"appendix|availability|ethic|statistic|abbreviation|author|contribution|competing|disclosure", _re.I)
_FT_INTRO = _re.compile(r"introduc|background|overview|literature|related|state of the art|theor", _re.I)
_FT_RESULT = _re.compile(r"result|finding|discussion|conclusion|summary|implication|outcome", _re.I)
_FT_BAD = _re.compile(r"\u27e6|\b(?:Figs?\.?|Figures?|Tables?|Eq\.?|Equations?|Supplementary|Appendix)\s*[A-Z]?\d|\bet al\b|"
                      r"\bpp?\.\s*\d|\b(?:shown|presented|illustrated|depicted|listed|summari[sz]ed|reported|described|given|seen)\s+(?:in|by)\s*[.,;:)]", _re.I)
_DEF_TYPES = ("what_is", "what_are", "define", "how_does", "why_is")

def _ft_pool(papers, n=FT_MAX_PAPERS):
    """Open-access papers with a DOI, best candidates first (PubMed / Europe PMC records most often have full text)."""
    return []   # full-text reading is paused together with Europe PMC
    return []   # full-text reading is paused together with Europe PMC
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
        sents = _split_sentences(text)
        for si, s in enumerate(sents):
            n = len(s.split())
            if s.endswith("?") or n < 8 or n > 50 or _FT_BAD.search(s):
                continue
            if ttype in _TWO:
                if not (kw2 and _kw_present(s, kw) and _kw_present(s, kw2)):
                    continue
            elif not (_loose_present(s, kw) if ttype == "loose" else _kw_present(s, kw)):
                continue
            score, hits = _score_sentence(s, kw, ttype, 1, kw2)      # idx 1: no 'first sentence of an abstract' bonus
            if hits and score + bonus >= FT_MIN_SCORE:
                cands.append({"text": s, "score": score + bonus, "hits": hits, "paper": paper,
                              "before": sents[si - 1][:320] if si > 0 and not _FT_BAD.search(sents[si - 1]) else "",
                              "after": sents[si + 1][:320] if si + 1 < len(sents) and not _FT_BAD.search(sents[si + 1]) else "",
                              "section": label, "license": ft.get("license") or "",
                              "tboost": 1 if _kw_present(paper.get("title") or "", kw) else 0})
    cands.sort(key=lambda c: c["score"], reverse=True)
    return cands[:per_paper]


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
            "contexts": [{"before": c.get("before", ""), "after": c.get("after", "")} for c in top],
            "licenses": [c["license"] for c in top],
            "cues": _CUES.get(ttype, []),
            "read": len(reads), "tried": len(papers),
            "best_rank": round(_rank(top[0]), 2) if top and ttype != "difference" and top[0]["score"] >= BEST_SCORE else None,
            "message": "" if top else ("No clear answer found in the full text of these papers." if reads else
                                       "Full text was not available for these papers."),
        })
    except Exception:
        return _fail("Full-text answer search")

# ── "Keep searching": another round of up to a minute, going deeper than the first look ──────────────
# The first search reads page 1 of each source. Each extra round reads the NEXT pages (new papers only: the page sends back the
# papers it has already read), takes sentences from their abstracts, and reads the full text of the new open-access ones.
# It stops at the time limit, when it already has plenty of strong answers, or when there is nothing new left to read.
MORE_BUDGET = 55.0      # seconds: no new page of papers is started after this (a started page can finish a few seconds later)
MORE_NEED_TIME = 14.0   # a new page needs about this long (search + full text), so none is started with less time left
MORE_STRONG = 5         # this many strong answers in one round is enough: stop early instead of using the whole minute
MORE_MAX_PAGE = 30

def _sort_cands(cands, sort):
    if sort == "cited":
        cands.sort(key=lambda c: (c["paper"].get("citations") or 0, c["score"]), reverse=True)
    elif sort == "recent":
        cands.sort(key=lambda c: (sort_date(c["paper"]), c["score"]), reverse=True)
    else:
        cands.sort(key=_rank, reverse=True)

@tools_bp.route("/api/answer/more", methods=["POST"])
@login_required
def answer_more():
    """Body (JSON): q (the question), page (the next page to read, starts at 2), seen (papers already read), have (answers already shown).
    Query string: the shared filters. Returns the new answers plus where the next round should start."""
    data = request.get_json(silent=True) or {}
    query = str(data.get("q") or "").strip()[:300]
    keyword, ttype, kw2 = _extract_keyword(query)
    if not keyword:
        return jsonify({"error": "unsupported_template"}), 400
    page = max(2, min(to_int(data.get("page")) or 2, MORE_MAX_PAGE))
    seen = {str(x).lower()[:200] for x in (data.get("seen") or [])[:1500] if isinstance(x, str)}
    seen_text = {str(x)[:80] for x in (data.get("have") or [])[:300] if isinstance(x, str)}
    try:
        f = read_filters(request.args.get)
        if request.args.get("sort") not in ("match", "cited", "recent"):
            f["sort"] = "match"
        pool_f = dict(f, sort="recent" if f["sort"] == "recent" else "cited")
        queries = _answer_queries(keyword, ttype, kw2)
        started = time.time()
        deadline = started + MORE_BUDGET
        cands, new_keys = [], []
        read = ft_read = ft_tried = pages_done = empty = 0
        exhausted = False
        p = page
        while p <= MORE_MAX_PAGE:
            with ThreadPoolExecutor(max_workers=len(queries)) as ex:
                batches = list(ex.map(lambda qq: search_papers(qq, p, pool_f, per_source=ANSWER_PER_SOURCE)[0], queries))
            fresh = [x for x in _merge_keep_abstract(batches) if _paper_key(x) and _paper_key(x).lower() not in seen]
            p += 1
            pages_done += 1
            if not fresh:
                empty += 1
                if empty >= 2:                                   # two pages in a row with nothing new: the sources have run dry
                    exhausted = True
                    break
                continue
            empty = 0
            for x in fresh:
                k = _paper_key(x)
                seen.add(k.lower())
                new_keys.append(k)
            _pubmed_abstracts(fresh)
            read += len(fresh)
            n0 = len(cands)
            _collect(fresh, keyword, ttype, kw2, cands, seen_text, MIN_SCORE)
            for c in cands[n0:]:
                c["from"] = "abstract"
            left = deadline - time.time()
            if left > 6:                                         # full text of the new open-access papers, as far as time allows
                pool = _ft_pool(fresh)
                ft_tried += len(pool)
                for paper, ft in topicfinder.fetch_fulltexts(pool, budget=min(9.0, left - 2), max_papers=FT_MAX_PAPERS):
                    ft_read += 1
                    for c in _fulltext_candidates(paper, ft, keyword, ttype, kw2):
                        key = c["text"].lower()[:80]
                        if key not in seen_text:
                            seen_text.add(key)
                            c["from"] = "full text"
                            cands.append(c)
            if sum(1 for c in cands if c["score"] >= BEST_SCORE) >= MORE_STRONG:
                break                                            # plenty of strong answers: no need to use the whole minute
            if deadline - time.time() < MORE_NEED_TIME:
                break
        _sort_cands(cands, f["sort"])
        top = _spread(cands, 6)
        return jsonify({
            "answers": [{"text": c["text"], "source": _pick(c["paper"]),
                         "context": {"before": c.get("before", ""), "after": c.get("after", "")},
                         "from": c.get("from", "abstract"), "section": c.get("section", ""), "license": c.get("license", ""),
                         "score": c["score"], "rank": round(_rank(c), 2)} for c in top],
            "read": read, "ft_read": ft_read, "ft_tried": ft_tried, "pages": pages_done,
            "next_page": p, "exhausted": exhausted or p > MORE_MAX_PAGE,
            "seen": new_keys, "seconds": round(time.time() - started, 1),
            "message": "" if top else "No new clear answers in these papers.",
        })
    except Exception:
        return _fail("Deeper answer search")

@tools_bp.route("/answer")
@login_required
def answer_page():
    return render_template("answer.html")
