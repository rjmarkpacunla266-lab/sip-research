#!/usr/bin/env python3
"""install_search3.py - run from the project folder (~/W):  python3 install_search3.py
Fix for Search Papers: little words (the, and, of, on, vs ...) are no longer highlighted,
no longer shown as "Missing", and no longer help rank papers. Needs install_search2.py first.
Python files are patched all-or-nothing. Safe to run twice."""
import json, os, re, sys

HELP = '"""searchhelp.py — rule-based helpers for Search Papers (no AI):\n   * synonym groups for common student topics\n   * "Did you mean" spelling suggestions (edit-distance match against a word list)\n   * ranking score: title words count more, plus citations, plus an optional bonus for newer papers\n"""\nimport math\nimport re\nfrom datetime import datetime\nfrom difflib import get_close_matches\n\n# Words that mean (almost) the same thing in student research. Single words first;\n# a group may also hold a short phrase.\nSYNONYM_GROUPS = [\n    ["student", "learner", "pupil"],\n    ["teenager", "adolescent", "youth"],\n    ["teacher", "educator", "instructor"],\n    ["smartphone", "cellphone", "mobile phone"],\n    ["children", "kids", "child"],\n    ["elderly", "older adults", "seniors"],\n    ["obesity", "overweight"],\n    ["poverty", "low income"],\n    ["exam", "examination", "assessment"],\n    ["grades", "academic performance"],\n    ["college", "university"],\n    ["pollution", "contamination"],\n    ["gadget", "device"],\n    ["gaming", "video games", "video game"],\n    ["covid", "covid-19", "coronavirus"],\n    ["women", "female"],\n    ["mathematics", "math", "maths"],\n    ["cheating", "academic dishonesty"],\n    ["typhoon", "tropical cyclone"],\n]\n\ndef _stem(w):\n    w = (w or "").lower().strip()\n    if len(w) > 4 and w.endswith("ies"):\n        return w[:-3] + "y"\n    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):\n        return w[:-1]\n    return w\n\n_SYN = {}\nfor _g in SYNONYM_GROUPS:\n    for _w in _g:\n        _SYN.setdefault(_stem(_w), set()).update(x for x in _g if _stem(x) != _stem(_w))\n\ndef synonym_terms(term):\n    """Other words/phrases that mean about the same as `term` (most useful first)."""\n    return sorted(_SYN.get(_stem(term), []), key=lambda s: (" " in s, len(s)))\n\n# Little words that should not be highlighted, counted as "missing", or used to rank papers.\nSTOPWORDS = set("""a an and or nor but of on in at to for from by with without within about as is are was were be been\nbeing this that these those its it their his her than then into onto over under between among via vs versus\nusing use used the their there which who whom whose what when where why how do does did can could should would\nmay might will shall not no""".split())\n\ndef content_terms(terms):\n    """The meaningful words of a search (drops \'the\', \'of\', \'vs\', ...). If nothing is left, keep them all."""\n    keep = [t for t in terms if t.lower() not in STOPWORDS]\n    return keep or list(terms)\n\n# ── "Did you mean" ──────────────────────────────────────────────────────────\nVOCAB = set("""\nabstract academic accuracy achievement activity adolescent adolescents agriculture algorithm analysis anxiety\napplication approach assessment attendance attitude awareness behavior behaviour biodiversity biology\nbiotechnology bullying cancer chemical chemistry children classroom climate cognitive college communication\ncommunity comparison computer conservation consumption contamination creativity critical cultural curriculum\ncyberbullying dengue depression development diabetes diet digital disaster discipline discrimination\ndistance disease earthquake ecology economic economics ecosystem education effect effectiveness efficiency\nelderly electricity emotional employment energy engagement engineering english environment environmental\nepidemic evaluation evidence exam examination exercise experience experiment factors family farmers\nfarming fertilizer financial flood food gadget gender geography global government grades graduate\nhappiness health healthcare higher history hospital household human hypothesis immune impact\nimplementation income industry infection inflation information innovation instruction intelligence\ninterest internet investigation language leadership learning lecture library lifestyle literacy\nliterature management marketing mathematics medicine memory mental method methodology microplastics\nmigration mobile motivation nursing nutrition obesity occupational online parent parental participation\npatients pedagogy performance personality pesticide pharmacy philippines physical physics plastic\npolicy pollution population poverty practice prevention primary productivity professional psychology\nquality quantitative qualitative questionnaire reading recycling relationship reliability renewable\nresearch resilience resource review sanitation satisfaction scholarship school science secondary\nsecurity self-esteem skills sleep smartphone smoking social socioeconomic software statistics strategy\nstress student students study success survey sustainable teacher teachers teaching technology\nteenager teenagers tourism training transportation treatment typhoon university urban validity\nvaccine violence virtual vocabulary waste water wellbeing women workplace writing youth\nalgae antibacterial antimicrobial antioxidant bacteria banana biodegradable biogas bioplastic bioethanol\ncalamansi charcoal coconut compost fermentation germination herbal hydroelectric mangrove medicinal microbiome\nmoringa nanoparticles organic photosynthesis probiotic protein seaweed solar vitamin wastewater\n""".split()) | {w for g in SYNONYM_GROUPS for x in g for w in x.split() if len(w) > 3}\n\ndef _words(text):\n    return re.findall(r"[A-Za-z]{4,}", (text or "").lower())\n\ndef suggest_query(query, papers=()):\n    """Return a corrected query when a word looks misspelled, otherwise \'\'.\n    A word counts as \'known\' if it is in our word list or in any returned paper title."""\n    known = set(VOCAB)\n    for p in papers or ():\n        known.update(_words(p.get("title")))\n    pool = sorted(known)\n    changed, out = False, []\n    for tok in re.split(r"\\s+", (query or "").strip()):\n        core = re.sub(r"[^A-Za-z\\-]", "", tok)\n        low = core.lower()\n        if len(low) < 5 or "-" in low or low in known or _stem(low) in known or low + "s" in known:\n            out.append(tok); continue\n        close = get_close_matches(low, pool, n=1, cutoff=0.84)\n        if close and close[0] != low:\n            fixed = close[0]\n            if core[:1].isupper():\n                fixed = fixed.capitalize()\n            out.append(tok.replace(core, fixed)); changed = True\n        else:\n            out.append(tok)\n    return " ".join(out) if changed else ""\n\n# ── Ranking ─────────────────────────────────────────────────────────────────\ndef recency_bonus(year, now_year=None):\n    """Extra points for newer papers (used only when \'Prefer newer papers\' is on)."""\n    try:\n        y = int(year)\n    except (TypeError, ValueError):\n        return 0.0\n    age = (now_year or datetime.now().year) - y\n    if age <= 3:  return 3.0\n    if age <= 6:  return 2.0\n    if age <= 10: return 1.0\n    return 0.0\n\ndef citation_points(citations):\n    """Gentle credit for citations: 10 cites ≈ 1, 100 ≈ 2, 1000 ≈ 3, 10000 ≈ 4."""\n    return math.log10(1 + max(0, citations or 0))\n'
SPEC = json.loads('{"engine.py": [["from searchhelp import synonym_terms, recency_bonus, citation_points\\n", "from searchhelp import synonym_terms, recency_bonus, citation_points, content_terms\\n"], ["    title, abstract = p.get(\\"title\\") or \\"\\", p.get(\\"abstract\\") or \\"\\"\\n    score, in_title = 0.0, 0\\n", "    terms = content_terms(terms)                 # ignore \\"the\\", \\"of\\", \\"vs\\", ...\\n    title, abstract = p.get(\\"title\\") or \\"\\", p.get(\\"abstract\\") or \\"\\"\\n    score, in_title = 0.0, 0\\n"]], "search.py": [["from searchhelp import synonym_terms, suggest_query\\n", "from searchhelp import synonym_terms, suggest_query, content_terms\\n"], ["    terms = _safe_terms(query)\\n    if len(terms) < 2:\\n        return []\\n    f2 = dict(f); f2[\\"field\\"] = \\"both\\"", "    terms = content_terms(_safe_terms(query))       # little words (the, of, vs) are never \'missing\'\\n    if len(terms) < 2:\\n        return []\\n    f2 = dict(f); f2[\\"field\\"] = \\"both\\""], ["    words = [w.lower() for w in _safe_terms(query)]\\n    for p in results:", "    words = [w.lower() for w in content_terms(_safe_terms(query))]\\n    for p in results:"]], "index.html": [["  const terms = (hlTerms || []).map(t => String(t).toLowerCase()).filter(t => t.length > 1);", "  const STOP = [\'the\',\'and\',\'of\',\'on\',\'in\',\'at\',\'to\',\'for\',\'from\',\'by\',\'with\',\'vs\',\'versus\',\'a\',\'an\',\'or\',\'as\',\'is\',\'are\',\'via\'];\\n  const terms = (hlTerms || []).map(t => String(t).toLowerCase()).filter(t => t.length > 1 && !STOP.includes(t));"]]}')

def find(name):
    for c in (name, os.path.join("routes", name), os.path.join("templates", name)):
        if os.path.isfile(c):
            return c
    return None

def read(p): return open(p, encoding="utf-8").read()
def write(p, t): open(p, "w", encoding="utf-8", newline="").write(t)

ok, warn = [], []

# --- new helper module -----------------------------------------------------
target = os.path.join(os.path.dirname(find("engine.py") or "engine.py") or ".", "searchhelp.py")
write(target, HELP)
ok.append(target)

# --- python + page patches -------------------------------------------------
for name, pairs in SPEC.items():
    p = find(name)
    if not p:
        warn.append(name + ": file not found (skipped)"); continue
    t = read(p); applied = 0; missing = []
    is_py = name.endswith(".py")
    for old, new in pairs:
        if new in t:
            continue                                    # already done
        elif old in t and t.count(old) == 1:
            t = t.replace(old, new); applied += 1
        else:
            missing.append(old[:70].replace("\n", " "))
    if missing and is_py:
        warn.append(name + ": left alone, did not match: " + " | ".join(missing)); continue
    if missing:
        warn.append(name + ": some parts did not match (" + str(len(missing)) + "): " + " | ".join(missing))
    if applied:
        if is_py:
            tmp = p + ".new"; write(tmp, t); os.replace(tmp, p)
        else:
            write(p, t)
        ok.append(p + " (" + str(applied) + " changes)")

print("Updated:", ", ".join(ok) or "nothing")
for w in warn: print("  warning:", w)
if not warn: print("Done. Run git status --short, then commit and push.")
