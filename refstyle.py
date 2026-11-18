"""refstyle.py - what kind of source is this, and the APA 7 reference for it.

1. detect(paper)    -> journal-article / book / book-chapter / preprint / conference / report / thesis /
                       dataset / webpage / other   (from the source's own "type" when it has one, else a guess)
2. enrich(cites, p) -> replaces the APA citation with one built from the APA 7 "Reference Guide for Journal
                       Articles, Books, and Edited Book Chapters" and adds: apa_html (with italics),
                       ref_type, ref_label, ref_guided, ref_notes, link_kind.
                       MLA, Chicago and Harvard are not changed.
3. tag_results(list) puts ref_type / ref_label / link_kind on every search result.

Only journal articles, books and chapters in edited books follow the guide. Every other type is labelled but
keeps the old format until its guide is added. Rule-based, no AI. It never raises: on any problem the
citation is returned as it was."""
import html as _h
import re

LABELS = {"journal-article": "Journal article", "book": "Book", "book-chapter": "Book chapter",
          "preprint": "Preprint", "conference": "Conference paper", "report": "Report",
          "thesis": "Thesis / dissertation", "dataset": "Dataset", "webpage": "Web page", "other": "Other source"}
GUIDED = {"journal-article", "book", "book-chapter"}

_TYPE_MAP = {
    "journal-article": "journal-article", "article": "journal-article", "review": "journal-article",
    "editorial": "journal-article", "letter": "journal-article", "erratum": "journal-article",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book", "book-set": "book",
    "book-chapter": "book-chapter", "book-part": "book-chapter", "book-section": "book-chapter",
    "reference-entry": "book-chapter", "book-track": "book-chapter",
    "preprint": "preprint", "posted-content": "preprint",
    "proceedings-article": "conference", "proceedings": "conference", "conference-paper": "conference",
    "dissertation": "thesis", "thesis": "thesis",
    "report": "report", "report-component": "report", "report-series": "report", "standard": "report",
    "dataset": "dataset", "database": "dataset",
}

def _s(v):
    return "" if v is None else str(v).strip()

def _authors(p):
    a = p.get("authors")
    return [str(x) for x in a if x] if isinstance(a, list) else []

def link_kind(p):
    u = _s(p.get("oa_url")).lower()
    if not u:
        return "none"
    path = u.split("?")[0].split("#")[0]
    return "pdf" if path.endswith(".pdf") or "/pdf/" in path or "/pdf" == path[-4:] else "html"

def detect(p):
    """-> (type, how) where how is 'source' (the database said so) or 'guess'."""
    t = _TYPE_MAP.get(_s(p.get("type")).lower())
    if t:
        return t, "source"
    src, journal, doi = _s(p.get("data_source")).lower(), _s(p.get("journal")), _s(p.get("doi")).lower()
    if "arxiv" in src or journal.lower() == "arxiv" or "10.48550/arxiv" in doi:
        return "preprint", "source"
    has_journal = bool(journal) and journal.lower() not in ("crossref", "semantic scholar")
    if has_journal and (p.get("volume") or p.get("issue") or p.get("pages")):
        return "journal-article", "guess"
    if has_journal:
        return "journal-article", "guess"
    if not doi and _s(p.get("oa_url")):
        return "webpage", "guess"
    return "other", "guess"

def tag_results(results):
    for p in results or []:
        try:
            t, how = detect(p)
            p["ref_type"], p["ref_label"], p["link_kind"] = t, LABELS[t], link_kind(p)
            built = build_apa(p, t)             # keep the short APA line on the card in step with the guide
            if built and built.get("text"):
                p["apa_reference"] = built["text"]
        except Exception:
            pass
    return results

# ── capitalisation (APA: sentence case for titles, Title Case for the periodical name) ──────────────────
_MINOR = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of", "off",
          "on", "per", "to", "up", "via", "vs", "with", "from", "into", "onto", "over", "than", "that", "is",
          "are", "was", "were", "be", "its", "it", "do", "does", "not", "no", "can", "may", "how", "what", "why",
          "when", "where", "which", "who", "if", "etc", "using", "under", "between", "among", "through", "during"}
_COMMON_SHORT = _MINOR | {"new", "low", "high", "use", "age", "air", "map", "sea", "sun", "web", "war", "way", "day",
                          "men", "one", "two", "six", "ten", "all", "any", "out", "our", "can", "has", "had", "her",
                          "his", "she", "him", "you", "oil", "gas", "fat", "dry", "wet", "red", "big", "key", "set",
                          "run", "test", "soil", "rice", "corn", "data", "case", "role", "yield"}
_GENERA = set("""Solanum Oryza Zea Musa Cocos Mangifera Glycine Triticum Hordeum Arachis Manihot Saccharum Capsicum
Brassica Arabidopsis Escherichia Staphylococcus Bacillus Pseudomonas Salmonella Aedes Anopheles Drosophila
Caenorhabditis Homo Candida Streptococcus Lactobacillus Azolla Moringa Allium Cucumis Cucurbita Carica Ananas Persea
Citrus Coffea Theobroma Gossypium Zingiber Curcuma Vigna Phaseolus Lactuca Spinacia Daucus Helianthus Nicotiana
Lycopersicon Eichhornia Chlorella Spirulina Oreochromis Clarias Penaeus Gallus Sus Bos Mus Rattus Trichoderma
Rhizobium Azospirillum Aspergillus Penicillium Saccharomyces Mycobacterium Vibrio Klebsiella Acinetobacter""".split())
_PROPER = set("""Philippines Philippine Filipino Filipina Manila Luzon Visayas Mindanao Cebu Davao Quezon Baguio Iloilo
Asia Asian Africa African Europe European America American Australia Australian Antarctica Pacific Atlantic
China Chinese Japan Japanese Korea Korean India Indian Indonesia Indonesian Malaysia Malaysian Thailand Thai
Vietnam Vietnamese Singapore Pakistan Bangladesh Nepal Nigeria Kenya Ghana Ethiopia Egypt Brazil Brazilian Mexico
Mexican Canada Canadian Germany German France French Spain Spanish Italy Italian Russia Russian Turkey Turkish
Iran Iranian Iraq Saudi Israel Britain British England English Scotland Ireland Irish Wales Portugal Portuguese
Netherlands Dutch Sweden Swedish Norway Norwegian Denmark Danish Finland Finnish Poland Polish Greece Greek
Ukraine Peru Chile Argentina Colombia Cuba Haiti Jamaica Zimbabwe Uganda Tanzania Morocco Algeria Sudan
January February March April June July August September October November December Monday Tuesday Wednesday
Thursday Friday Saturday Sunday Mediterranean Amazon Nile Mekong Himalayan Arctic Caribbean Latin Arabic Hindi
Tagalog Cebuano Ilocano Mandarin Cantonese Swahili Darwin Mendel Newton Einstein Bayesian Gaussian Markov Poisson
Fourier Pearson Spearman Likert Google Facebook YouTube Twitter Instagram TikTok Wikipedia Zoom Python Excel
Windows Android""".split())
_PROPER_LOWER = {w.lower(): w for w in _PROPER}
_GENERA_LOWER = {w.lower(): w for w in _GENERA}
_PAIRS = [(r"\bunited states\b", "United States"), (r"\bunited kingdom\b", "United Kingdom"),
          (r"\bnew zealand\b", "New Zealand"), (r"\bsouth africa\b", "South Africa"), (r"\bsouth korea\b", "South Korea"),
          (r"\bnorth america\b", "North America"), (r"\bsouth america\b", "South America"),
          (r"\bsri lanka\b", "Sri Lanka"), (r"\bhong kong\b", "Hong Kong"), (r"\bsaudi arabia\b", "Saudi Arabia"),
          (r"\bcosta rica\b", "Costa Rica"), (r"\bsoutheast asia\b", "Southeast Asia"), (r"\bsouth asia\b", "South Asia"),
          (r"\bmiddle east\b", "Middle East"), (r"\bworld war\b", "World War"), (r"\bworld health organization\b", "World Health Organization")]
_ROMAN = re.compile(r"^(?:ii|iii|iv|vi|vii|viii|ix|xi|xii)$", re.I)

def _letters(s):
    return [c for c in s if c.isalpha()]

def _style_of(title):
    """'caps' (ALL CAPITALS), 'title' (Title Case) or 'sentence' (leave alone)."""
    let = _letters(title)
    if len(let) >= 6 and not any(c.islower() for c in let):
        return "caps"
    words = [w.strip("()[]{}'\",.;:!?") for w in title.split()]
    major = [w for w in words[1:] if len(w) >= 4 and w.isalpha() and w.lower() not in _MINOR]
    if len(major) >= 2 and sum(1 for w in major if w[0].isupper()) / len(major) >= 0.7:
        return "title"
    return "sentence"

def _split_token(tok):
    m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", tok, re.S)
    return m.group(1), m.group(2), m.group(3)

def sentence_case(title):
    """-> (new_title, changed). Capitalises only the first word, the first word after a colon, proper
    nouns from a built-in list, acronyms and Latin genus names. Titles already in sentence case are left alone."""
    title = re.sub(r"\s+", " ", _s(title))
    mode = _style_of(title)
    if mode == "sentence":
        return title, False
    out, start = [], True
    for tok in title.split(" "):
        lead, core, trail = _split_token(tok)
        parts = core.split("-")
        res = []
        for k, w in enumerate(parts):
            if not w:
                res.append(w); continue
            low = w.lower()
            keep = None
            if any(c.isdigit() for c in w): keep = w                                       # COVID-19, H2O, 2023
            elif len(w) == 1 and w.isupper() and (trail.startswith(".") or w == "I"): keep = w            # L. (Linnaeus), J., I
            elif low in _PROPER_LOWER: keep = _PROPER_LOWER[low]
            elif low in _GENERA_LOWER: keep = _GENERA_LOWER[low]
            elif _ROMAN.match(w) and mode == "title": keep = w.upper()
            elif mode == "title" and (sum(1 for c in w if c.isupper()) >= 2 or (w[0].islower() and any(c.isupper() for c in w[1:]))
                                       or (len(w) > 1 and w[0].isupper() and any(c.isupper() for c in w[1:]))):
                keep = w                                                                    # DNA, pH, iPhone, OpenAlex
            elif mode == "caps" and len(w) <= 3 and low not in _COMMON_SHORT and w.isalpha():
                keep = w                                                                    # short acronym in a CAPS title
            w2 = keep if keep is not None else low
            if start and k == 0 and keep is None:
                w2 = w2[:1].upper() + w2[1:]
            res.append(w2)
        word = "-".join(res)
        out.append(lead + word + trail)
        start = tok.endswith((":", "?", "!", "\u2014"))
    s = " ".join(out)
    for pat, canon in _PAIRS:
        s = re.sub(pat, canon, s, flags=re.I)
    return s, s != title

_ACRONYM_J = {"IEEE", "ACM", "BMC", "PNAS", "JAMA", "AIP", "SPIE", "IOP", "MDPI", "RSC", "ASME", "ASCE", "ACS", "BMJ",
              "NPJ", "PLOS", "AMB", "FEBS", "EMBO", "RNA", "DNA", "AI", "ICT", "IT", "UK", "US", "USA", "ISPRS", "SAE", "AIChE"}

def periodical_case(name):
    """APA: capitalise all major words of the periodical name. Only ALL-CAPS names are changed."""
    name = re.sub(r"\s+", " ", _s(name))
    let = _letters(name)
    if len(let) < 4 or any(c.islower() for c in let):
        return name, False
    out = []
    for i, w in enumerate(name.split(" ")):
        lead, core, trail = _split_token(w)
        if core.upper() in _ACRONYM_J or (len(core) <= 2 and core.isalpha() and core.lower() not in _MINOR):
            c = core.upper()
        elif i > 0 and core.lower() in _MINOR:
            c = core.lower()
        else:
            c = "-".join(x[:1].upper() + x[1:].lower() for x in core.split("-"))
        out.append(lead + c + trail)
    s = " ".join(out)
    return s, s != name

# ── APA 7 builders ────────────────────────────────────────────────────────────────────────────────────
def _core():
    import core
    return core

def _author_text(authors):
    c = _core()
    ppl = c._people(authors)
    names = [c._inverted(p) for p in ppl]
    if not names: return "Unknown", 0
    if len(names) == 1: return names[0], 1
    if len(names) <= 20: return ", ".join(names[:-1]) + ", & " + names[-1], len(names)
    return ", ".join(names[:19]) + ", … " + names[-1], len(names)

def _editor_text(editors):
    c = _core()
    ppl = c._people([e for e in editors if e])
    if not ppl: return ""
    names = []
    for p in ppl:
        fam, given, ini, suf = p
        ini_txt = c._given_full(p) if ini else c._initials(given, False)
        names.append((ini_txt + " " + fam).strip())
    if len(names) == 1: return names[0] + " (Ed.),"
    if len(names) == 2: return names[0] + " & " + names[1] + " (Eds.),"
    return ", ".join(names[:-1]) + ", & " + names[-1] + " (Eds.),"

def _e(s): return _h.escape(s, quote=False)

def _edition(v):
    v = _s(v)
    if not v: return ""
    if v.isdigit():
        n = int(v)
        suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf} ed."
    return v if v.lower().endswith("ed.") else v + " ed."

def build_apa(p, t):
    """-> {'text','html','notes'} for the guided types, else None."""
    if t not in GUIDED:
        return None
    c = _core()
    authors = _authors(p)
    a_txt, n_auth = _author_text(authors)
    year = _s(p.get("year")) or "n.d."
    notes = []
    if not n_auth:
        notes.append("No authors found. APA puts the title first when there is no author; this is not done automatically.")
    title, ch = sentence_case(p.get("title") or "")
    title = title or "Untitled"
    if ch:
        notes.append("The title was changed to sentence case (APA). Check proper nouns such as names and places.")
    doi = c._doi_url(p.get("doi"))
    publisher = _s(p.get("publisher"))
    pages = c._en_dash(_s(p.get("pages")))
    head_t = f"{c._dot(a_txt)} ({year}). "
    head_h = f"{_e(c._dot(a_txt))} ({_e(year)}). "
    if t == "journal-article":
        jn = _s(p.get("journal"))
        if jn.lower() in ("crossref", "semantic scholar"): jn = ""
        jn, jch = periodical_case(jn)
        if jch: notes.append("The journal name was changed from all capitals to normal capitals.")
        vol, iss = _s(p.get("volume")), _s(p.get("issue"))
        text = head_t + c._dot(title)
        htm = head_h + _e(c._dot(title))
        if jn:
            vi_t = f", {vol}({iss})" if vol and iss else (f", {vol}" if vol else "")
            vi_h = f", <i>{_e(vol)}</i>({_e(iss)})" if vol and iss else (f", <i>{_e(vol)}</i>" if vol else "")
            pg = f", {pages}" if pages else ""
            text += f" {jn}{vi_t}{pg}."
            htm += f" <i>{_e(jn)}</i>{vi_h}{_e(pg)}."
            if not vol: notes.append("Volume number not found. Add it if the article has one.")
            if not pages: notes.append("Page range not found. Add it if the article has one (APA uses an en dash).")
        else:
            notes.append("Journal name not found.")
        if doi: text += f" {doi}"; htm += f" {_e(doi)}"
        else: notes.append("No DOI found. Add the DOI if the article has one.")
        return {"text": text, "html": htm, "notes": notes}
    if t == "book":
        ed = _edition(p.get("edition"))
        ed_t = f" ({ed})" if ed else ""
        title_dot = title.rstrip(".")
        text = head_t + title_dot + ed_t + "."
        htm = head_h + f"<i>{_e(title_dot)}</i>" + _e(ed_t) + "."
        if publisher:
            text += " " + c._dot(publisher); htm += " " + _e(c._dot(publisher))
        else:
            notes.append("Publisher not found. Add the publisher name (no location).")
        if doi: text += f" {doi}"; htm += f" {_e(doi)}"
        else: notes.append("No DOI found. Add a DOI, or a URL if it is an ebook from a website.")
        return {"text": text, "html": htm, "notes": notes}
    # book-chapter
    book = _s(p.get("journal"))
    if book.lower() in ("crossref", "semantic scholar"): book = ""
    book, _ = sentence_case(book)
    eds = p.get("editors") if isinstance(p.get("editors"), list) else []
    ed_txt = _editor_text([str(x) for x in eds])
    ed = _edition(p.get("edition"))
    paren = ", ".join(x for x in (ed, f"pp. {pages}" if pages else "") if x)
    text = head_t + c._dot(title)
    htm = head_h + _e(c._dot(title))
    if book:
        text += f" In {ed_txt + ' ' if ed_txt else ''}{book}" + (f" ({paren})" if paren else "") + "."
        htm += f" In {_e(ed_txt) + ' ' if ed_txt else ''}<i>{_e(book)}</i>" + (f" ({_e(paren)})" if paren else "") + "."
        if not ed_txt:
            notes.append("Editors not found. APA needs them: write In A. A. Editor (Ed.), before the book title.")
    else:
        notes.append("The title of the book that holds this chapter was not found.")
    if not pages: notes.append("Chapter page range not found. Add it if there is one.")
    if publisher:
        text += " " + c._dot(publisher); htm += " " + _e(c._dot(publisher))
    else:
        notes.append("Publisher not found. Add the publisher name (no location).")
    if doi: text += f" {doi}"; htm += f" {_e(doi)}"
    else: notes.append("No DOI found. Add a DOI or URL if there is one.")
    return {"text": text, "html": htm, "notes": notes}


# ── MLA 7, Chicago 16 (notes-bibliography) and Harvard (EUCLID): the school guides ───────────────────────────────
import datetime as _dt

_HEAD_MINOR = {"a", "an", "the", "and", "but", "or", "nor", "for", "so", "yet", "as", "at", "by", "in", "of", "off",
               "on", "per", "to", "up", "via", "vs", "with", "from", "into", "onto", "over", "upon", "under", "between",
               "among", "through", "during", "within", "without", "across", "around", "before", "after", "against",
               "along", "about", "above", "below", "beyond", "toward", "towards", "near", "amid", "beside"}

def title_case(title):
    """Headline style: capitalise the important words; keep acronyms, names with inner capitals and numbers;
    keep a species word after a Latin genus lower-case. Returns (title, changed)."""
    t = _s(title)
    if not t:
        return t, False
    base = t.lower() if (t.isupper() or t.islower()) else t
    parts = re.split(r"(\s+)", base)
    out, first, prev = [], True, ""
    for tok in parts:
        if not tok or tok.isspace():
            out.append(tok); continue
        core = tok.strip("\"'“”‘’()[]{}.,;:!?")
        low = core.lower()
        keep = (len(core) > 1 and core.isupper()) or any(ch.isdigit() for ch in core) \
            or (any(ch.isupper() for ch in core[1:]) and not core.isupper()) \
            or low in _PROPER_LOWER or low in _GENERA_LOWER
        def cap_word(w):
            if not w:
                return w
            i0 = next((k for k, ch in enumerate(w) if ch.isalpha()), None)
            return w if i0 is None else w[:i0] + w[i0].upper() + w[i0 + 1:]
        if prev in _GENERA_LOWER and low.isalpha() and low not in _HEAD_MINOR and not first:
            word = tok                                     # species name after a genus: leave as it is
        elif keep:
            word = _PROPER_LOWER.get(low) or _GENERA_LOWER.get(low) or tok if (low in _PROPER_LOWER or low in _GENERA_LOWER) else tok
        elif low in _HEAD_MINOR and not first:
            word = tok.lower() if tok == tok.lower() or t.isupper() else tok
            if tok != tok.lower() and not (t.isupper() or t.islower()):
                word = tok
        else:
            word = "-".join(cap_word(x) if (x.lower() not in _HEAD_MINOR or k == 0) else x for k, x in enumerate(tok.split("-")))
        out.append(word)
        first = tok.rstrip().endswith(":")                # first word after a colon is capitalised too
        prev = low
    res = "".join(out)
    # the last word is always capitalised
    m = re.search(r"([A-Za-z][A-Za-z'’-]*)([^A-Za-z]*)$", res)
    if m and m.group(1).lower() in _HEAD_MINOR and len(res.split()) > 1:
        res = res[:m.start(1)] + m.group(1)[0].upper() + m.group(1)[1:] + res[m.end(1):]
    return res, res != t

_MON_MLA = ["Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]

def _today():
    return _dt.datetime.now()

def _access_mla():
    d = _today(); return f"{d.day} {_MON_MLA[d.month - 1]} {d.year}"

def _access_long():
    d = _today(); return f"{d.day} {d.strftime('%B')} {d.year}"

def _hy(pages):
    """Page range with a plain hyphen (the three school guides use 27-45, not an en dash)."""
    return re.sub(r"[–—−]", "-", _s(pages))

def _mla_pages(pages):
    pg = _hy(pages)
    m = re.fullmatch(r"(\d+)-(\d+)", pg)
    if not m:
        return pg
    a, b = m.group(1), m.group(2)
    if len(a) > 2 and len(a) == len(b) and int(b) > int(a):
        k = 0
        while k < len(a) and a[k] == b[k]:
            k += 1
        keep = max(2, len(b) - k)
        b = b[-keep:]
    return f"{a}-{b}"

def _ppl(authors):
    return _core()._people(authors)

def _full_list(names, comma_before_and):
    if len(names) == 1: return names[0]
    if len(names) == 2: return f"{names[0]}, and {names[1]}" if comma_before_and else f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + (", and " if comma_before_and else " and ") + names[-1]

def _mla_authors(authors, mode):
    c = _core(); ppl = _ppl(authors); n = len(ppl)
    if not n: return ""
    first = c._inverted(ppl[0], full=True)
    if (mode == "etal" and n >= 3) or (mode != "all" and n > 3):
        return c._dot(f"{first}, et al.")
    rest = [c._natural(x) for x in ppl[1:]]
    if n == 1: return c._dot(first)
    if n == 2: return c._dot(f"{first}, and {rest[0]}")
    return c._dot(f"{first}, " + ", ".join(rest[:-1]) + f", and {rest[-1]}")

def _chi_authors(authors, mode):
    c = _core(); ppl = _ppl(authors); n = len(ppl)
    if not n: return ""
    first = c._inverted(ppl[0], full=True)
    rest = [c._natural(x) for x in ppl[1:]]
    if mode == "etal" and n >= 3:
        return c._dot(f"{first}, et al.")
    if mode != "all" and n >= 11:
        return c._dot(", ".join([first] + rest[:6]) + " et al.")
    if n == 1: return c._dot(first)
    if n == 2: return c._dot(f"{first}, and {rest[0]}")
    return c._dot(f"{first}, " + ", ".join(rest[:-1]) + f" and {rest[-1]}")

def _har_authors(authors, mode):
    c = _core(); ppl = _ppl(authors); n = len(ppl)
    if not n: return ""
    names = [c._inverted(x) for x in ppl]
    if mode == "etal" and n >= 3:
        return f"{names[0]} et al."
    if n == 1: return names[0]
    if n == 2: return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f" and {names[-1]}"

def _nat_list(names, serial=True):
    if not names: return ""
    if len(names) == 1: return names[0]
    if len(names) == 2: return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + (", and " if serial else " and ") + names[-1]

def _editors_natural(editors):
    c = _core(); ppl = c._people([e for e in editors if e])
    return [c._natural(x) for x in ppl]

def _editors_initials(editors):
    c = _core(); ppl = c._people([e for e in editors if e])
    return [c._inverted(x) for x in ppl]

def _journal_name(p, notes):
    jn = _s(p.get("journal"))
    if jn.lower() in ("crossref", "semantic scholar"): jn = ""
    jn, _ = periodical_case(jn)
    return jn

def _clean_title_period(t):
    return t.rstrip(".") if t.endswith("..") else t

def _put(parts, text, html=None):
    parts[0] += text; parts[1] += (html if html is not None else _e(text))

def build_mla(p, t, mode=None):
    """MLA 7th edition (the school's quick guide): medium 'Web.' and access date, no URL/DOI, place 'N.p.' when unknown."""
    if t not in GUIDED: return None
    c = _core(); notes = []
    au = _mla_authors(_authors(p), mode)
    title, ch = title_case(p.get("title") or ""); title = title or "Untitled"
    if ch: notes.append("The title was put in Title Case (MLA). Check names and Latin species names.")
    year = _s(p.get("year")) or "n.d."
    pages = _mla_pages(p.get("pages")); publisher = _s(p.get("publisher"))
    web = f"Web. {_access_mla()}."
    T = ["", ""]
    if au: _put(T, au + " ")
    else: notes.append("No authors found. MLA starts with the title when there is no author.")
    if t == "journal-article":
        jn = _journal_name(p, notes); vol, iss = _s(p.get("volume")), _s(p.get("issue"))
        _put(T, f"“{c._dot(title)}” ")
        if jn:
            vi = f" {vol}.{iss}" if vol and iss else (f" {vol}" if vol else "")
            _put(T, f"{jn}{vi} ({year})" + (f": {pages}." if pages else ".") + " ", f"<i>{_e(jn)}</i>{_e(vi)} ({_e(year)})" + (f": {_e(pages)}." if pages else ".") + " ")
            if not vol: notes.append("Volume number not found. Add it if the article has one.")
            if not pages: notes.append("Page range not found. Add it if the article has one.")
        else:
            notes.append("Journal name not found.")
        _put(T, web)
    elif t == "book":
        ed = _edition(p.get("edition"))
        _put(T, f"{c._dot(title.rstrip('.'))} ", f"<i>{_e(title.rstrip('.'))}</i>. ")
        if ed: _put(T, ed + " ")
        _put(T, f"N.p.: {publisher}, {year}. " if publisher else f"N.p.: n.p., {year}. ")
        if not publisher: notes.append("Publisher not found. Add it (MLA: Place: Publisher, Year).")
        notes.append("Place of publication is not in the data (shown as N.p.). Add the city if you know it.")
        _put(T, web)
    else:
        book = _s(p.get("journal"))
        if book.lower() in ("crossref", "semantic scholar"): book = ""
        book, _ = title_case(book)
        eds = _editors_natural([str(x) for x in (p.get("editors") or [])])
        _put(T, f"“{c._dot(title)}” ")
        if book:
            _put(T, f"{book}. ", f"<i>{_e(book)}</i>. ")
            if eds: _put(T, "Ed. " + _nat_list(eds) + ". ")
            else: notes.append("Editors not found. MLA lists them after the book title: Ed. First Last.")
        else:
            notes.append("The title of the book that holds this chapter was not found.")
        ed = _edition(p.get("edition"))
        if ed: _put(T, ed + " ")
        _put(T, f"N.p.: {publisher}, {year}. " if publisher else f"N.p.: n.p., {year}. ")
        if not publisher: notes.append("Publisher not found.")
        notes.append("Place of publication is not in the data (shown as N.p.). Add the city if you know it.")
        if pages: _put(T, pages + ". ")
        else: notes.append("Chapter page range not found.")
        _put(T, web)
    notes.append("MLA 7 needs no URL or DOI. It needs the access date, which is today's date.")
    return {"text": T[0].strip(), "html": T[1].strip(), "notes": notes}

def build_chicago(p, t, mode=None):
    """Chicago 16th edition, notes-bibliography style (bibliography entry), as in the school's quick guide."""
    if t not in GUIDED: return None
    c = _core(); notes = []
    au = _chi_authors(_authors(p), mode)
    title, ch = title_case(p.get("title") or ""); title = title or "Untitled"
    if ch: notes.append("The title was put in headline style (Chicago). Check names and Latin species names.")
    year = _s(p.get("year")) or "n.d."
    pages = _hy(p.get("pages")); publisher = _s(p.get("publisher"))
    doi = _s(p.get("doi")); doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    T = ["", ""]
    if au: _put(T, au + " ")
    else: notes.append("No authors found. Chicago starts with the title when there is no author.")
    if t == "journal-article":
        jn = _journal_name(p, notes); vol, iss = _s(p.get("volume")), _s(p.get("issue"))
        _put(T, f"“{c._dot(title)}” ")
        if jn:
            v = (f" {vol}" if vol else "") + (f", no. {iss}" if iss else "")
            _put(T, f"{jn}{v} ({year})" + (f": {pages}." if pages else ".") , f"<i>{_e(jn)}</i>{_e(v)} ({_e(year)})" + (f": {_e(pages)}." if pages else "."))
            if not vol: notes.append("Volume number not found. Add it if the article has one.")
            if not pages: notes.append("Page range not found. Add it if the article has one.")
        else:
            notes.append("Journal name not found.")
        if doi: _put(T, f" doi: {doi}.")
        else: notes.append("No DOI found. Chicago wants a DOI or URL for online articles.")
        notes.append("Chicago shows the month or season when known (for example December 2009). Only the year is in the data.")
    elif t == "book":
        ed = _edition(p.get("edition"))
        _put(T, f"{c._dot(title.rstrip('.'))} ", f"<i>{_e(title.rstrip('.'))}</i>. ")
        if ed: _put(T, ed + " ")
        if publisher: _put(T, f"{publisher}, {year}.")
        else: _put(T, f"{year}."); notes.append("Publisher not found. Add it (Chicago: Place: Publisher, Year).")
        notes.append("Place of publication is not in the data. Chicago puts the city before the publisher; add it if you know it.")
        if doi: _put(T, f" doi: {doi}.")
        else: notes.append("No DOI found. Chicago wants a DOI or URL for online books.")
    else:
        book = _s(p.get("journal"))
        if book.lower() in ("crossref", "semantic scholar"): book = ""
        book, _ = title_case(book)
        eds = _editors_natural([str(x) for x in (p.get("editors") or [])])
        _put(T, f"“{c._dot(title)}” ")
        if book:
            _put(T, f"In {book}", f"In <i>{_e(book)}</i>")
            if eds: _put(T, ", edited by " + _nat_list(eds) + ". ")
            else: _put(T, ". "); notes.append("Editors not found. Chicago writes: In Book Title, edited by First Last.")
        else:
            notes.append("The title of the book that holds this chapter was not found.")
        ed = _edition(p.get("edition"))
        if ed: _put(T, ed + " ")
        if publisher: _put(T, f"{publisher}, {year}.")
        else: _put(T, f"{year}."); notes.append("Publisher not found.")
        notes.append("Place of publication is not in the data. Chicago puts the city before the publisher; add it if you know it.")
        if doi: _put(T, f" doi: {doi}.")
        else: notes.append("No DOI found.")
    notes.append("This is the Chicago bibliography entry. A footnote has the first name first and different commas.")
    return {"text": T[0].strip(), "html": T[1].strip(), "notes": notes}

def build_harvard(p, t, mode=None):
    """Harvard, as in the EUCLID guide: Surname, I. (Year) 'Article title', Journal, vol(issue), pages, available: URL [accessed date]."""
    if t not in GUIDED: return None
    c = _core(); notes = []
    au = _har_authors(_authors(p), mode)
    title, ch = sentence_case(p.get("title") or ""); title = title or "Untitled"
    if ch: notes.append("The title was changed to sentence case. Check proper nouns such as names and places.")
    year = _s(p.get("year")) or "n.d."
    pages = _hy(p.get("pages")); publisher = _s(p.get("publisher"))
    link = c._doi_url(p.get("doi")) or _s(p.get("oa_url"))
    avail = f"available: {link} [accessed {_access_long()}]" if link else ""
    T = ["", ""]
    _put(T, (au if au else "Unknown") + f" ({year}) ")
    if not au: notes.append("No authors found. Harvard starts with the title when there is no author.")
    tt = title.rstrip(".")
    if t == "journal-article":
        jn = _journal_name(p, notes); vol, iss = _s(p.get("volume")), _s(p.get("issue"))
        _put(T, f"'{tt}'")
        if jn:
            vi = f"{vol}({iss})" if vol and iss else vol
            _put(T, f", {jn}", f", <i>{_e(jn)}</i>")
            if vi: _put(T, f", {vi}")
            if pages: _put(T, f", {pages}")
            if not vol: notes.append("Volume number not found. Add it if the article has one.")
            if not pages: notes.append("Page range not found. Add it if the article has one.")
        else:
            notes.append("Journal name not found.")
        _put(T, (", " + avail if avail else "") + ".")
        if not link: notes.append("No DOI or link found. Harvard wants available: and the access date for online articles.")
    elif t == "book":
        ed = _edition(p.get("edition"))
        _put(T, tt, f"<i>{_e(tt)}</i>")
        if ed: _put(T, f", {ed}")
        if publisher: _put(T, f", {publisher}")
        else: notes.append("Publisher not found. Add it (Harvard: Place: Publisher).")
        if link: _put(T, f" [online], {avail}")
        _put(T, ".")
        notes.append("Place of publication is not in the data. Harvard puts the city before the publisher; add it if you know it.")
    else:
        book = _s(p.get("journal"))
        if book.lower() in ("crossref", "semantic scholar"): book = ""
        book, _ = sentence_case(book)
        eds = _editors_initials([str(x) for x in (p.get("editors") or [])])
        _put(T, f"'{tt}'")
        if book:
            ed_txt = ""
            if eds:
                ed_txt = " " + (", ".join(eds[:-1]) + " and " + eds[-1] if len(eds) > 1 else eds[0]) + (", eds." if len(eds) > 1 else ", ed.")
            _put(T, f", in{ed_txt} {book}", f", in{_e(ed_txt)} <i>{_e(book)}</i>")
            if not eds: notes.append("Editors not found. Harvard writes: in Surname, I., ed. Book title.")
        else:
            notes.append("The title of the book that holds this chapter was not found.")
        if publisher: _put(T, f", {publisher}")
        else: notes.append("Publisher not found.")
        if pages: _put(T, f", {pages}")
        else: notes.append("Chapter page range not found.")
        if link: _put(T, f", {avail}")
        _put(T, ".")
        notes.append("Place of publication is not in the data. Harvard puts the city before the publisher; add it if you know it.")
    return {"text": T[0].strip(), "html": T[1].strip(), "notes": notes}

_STYLE_GUIDE = {"apa": "APA 7 guide", "mla": "MLA 7 school guide", "chicago": "Chicago 16 school guide",
                "harvard": "Harvard (EUCLID) school guide"}

def enrich(cites, p, mode=None):
    """cites = {'apa','mla','chicago','harvard'} from the normal builders. Returns an extended copy.
    mode = 'auto' | 'all' | 'etal' (the Authors option); the guided MLA/Chicago/Harvard entries use it themselves."""
    try:
        if not isinstance(cites, dict) or not isinstance(p, dict):
            return cites
        mode = mode if mode in ("all", "etal") else "auto"
        t, how = detect(p)
        out = dict(cites)
        by, done, guides = {}, [], {}
        built = build_apa(p, t)
        if built:
            out["apa"], out["apa_html"] = built["text"], built["html"]
            by["apa"] = built["notes"]; guides["apa"] = _STYLE_GUIDE["apa"]
        else:
            by["apa"] = ["APA format for this kind of source has not been checked against a guide yet. It is shown like a journal article."]
        for st, fn in (("mla", build_mla), ("chicago", build_chicago), ("harvard", build_harvard)):
            try:
                b = fn(p, t, mode)
            except Exception:
                b = None
            if b and b.get("text"):
                out[st], out[st + "_html"] = b["text"], b["html"]
                by[st] = b["notes"]; guides[st] = _STYLE_GUIDE[st]; done.append(st)
            else:
                by[st] = ["This style has not been checked against a guide for this kind of source yet."]
        if how == "guess":
            for k in by: by[k] = ["The source type was guessed from the record. Check it."] + by[k]
        out.update({"ref_type": t, "ref_label": LABELS[t], "ref_guided": bool(built), "ref_how": how,
                    "ref_notes": by["apa"], "ref_notes_by": by, "ref_guides": guides, "ref_done": done,
                    "link_kind": link_kind(p)})
        return out
    except Exception:
        return cites
