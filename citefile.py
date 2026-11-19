"""citefile.py: read a PDF / Word file (or pasted text) IN MEMORY and work out the details needed for a citation.

Nothing here writes to disk or to the database. The caller passes bytes in and gets a small dict of details back.
Everything is rule-based (no AI). When a DOI or a matching title is found, the details come from Crossref (real records);
otherwise they are a guess from the first page, and the page tells the person to check them.
"""
import difflib
import io
import re
import zipfile
from datetime import datetime

MAX_BYTES = 12 * 1024 * 1024
MAX_PAGES = 3
MAX_CHARS = 20000

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\]\[{}|\\^`]+)", re.I)
_BAD_TITLE = re.compile(r"(^microsoft word|^untitled|\.docx?$|\.pdf$|^document\d*$|^slide|^abstract$|^title$)", re.I)
_NOT_TITLE_LINE = re.compile(r"(journal|university|college|department|faculty|school of|institute|proceedings|issn|isbn|vol\.|doi|http|www\.|@|received|accepted|"
                             r"copyright|©|all rights reserved|volume|research article|original article|review article|abstract|key ?words?|review paper|research paper|open access|et al\b)", re.I)


def kind_of(data, filename=""):
    """'pdf' | 'docx' | 'doc' | '' from the first bytes (the file name alone is not trusted)."""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except Exception:
            return ""
        return ""
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    return ""


# ── readers ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def read_pdf(data):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("PDF reading is not installed on the server yet (pypdf).")
    r = PdfReader(io.BytesIO(data))
    if getattr(r, "is_encrypted", False):
        try:
            if not r.decrypt(""):
                raise ValueError("This PDF is password-protected, so it cannot be read.")
        except ValueError:
            raise
        except Exception:
            raise ValueError("This PDF is password-protected, so it cannot be read.")
    meta = {}
    try:
        m = r.metadata or {}
        meta["title"] = str(m.get("/Title") or "").strip()
        meta["author"] = str(m.get("/Author") or "").strip()
        cd = str(m.get("/CreationDate") or "")
        mm = re.search(r"(19|20)\d{2}", cd)
        meta["year"] = mm.group(0) if mm else ""
    except Exception:
        pass
    text = []
    for page in list(r.pages)[:MAX_PAGES]:
        try:
            text.append(page.extract_text() or "")
        except Exception:
            pass
    return "\n".join(text)[:MAX_CHARS], meta


def _xml_text(s):
    s = re.sub(r"</w:p>|<w:br[^>]*/>|<w:tab/>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))


def read_docx(data):
    meta = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        def get(name, cap):
            info = z.getinfo(name)
            if info.file_size > cap:
                raise ValueError("This Word file is too large to read here.")
            return z.read(name).decode("utf-8", "ignore")
        body = get("word/document.xml", 25 * 1024 * 1024)
        try:
            core = get("docProps/core.xml", 1024 * 1024)
            t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", core, re.S)
            a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", core, re.S)
            c = re.search(r"<dcterms:created[^>]*>(.*?)</dcterms:created>", core, re.S)
            meta["title"] = _xml_text(t.group(1)).strip() if t else ""
            meta["author"] = _xml_text(a.group(1)).strip() if a else ""
            meta["year"] = (re.search(r"(19|20)\d{2}", c.group(1)).group(0) if c and re.search(r"(19|20)\d{2}", c.group(1)) else "")
        except Exception:
            pass
    return re.sub(r"\n{3,}", "\n\n", _xml_text(body))[:MAX_CHARS], meta


def read_doc(data):
    """Old binary .doc: no clean reader without extra software, so pull out the readable text runs. Less reliable."""
    runs = []
    for m in re.finditer(rb"(?:[\x20-\x7e\xa0-\xff]\x00){24,}", data[:3_000_000]):
        runs.append(m.group(0).decode("utf-16le", "ignore"))
    for m in re.finditer(rb"[\x20-\x7e]{40,}", data[:3_000_000]):
        runs.append(m.group(0).decode("latin-1", "ignore"))
    text = "\n".join(dict.fromkeys(r.strip() for r in runs if r.strip()))
    return text[:MAX_CHARS], {}


def read_any(data, filename=""):
    k = kind_of(data, filename)
    if not k:
        raise ValueError("This does not look like a PDF, DOCX or DOC file.")
    text, meta = {"pdf": read_pdf, "docx": read_docx, "doc": read_doc}[k](data)
    return k, text, meta


# ── guessing the details ───────────────────────────────────────────────────────────────────────────────────────────
def find_doi(text):
    for m in DOI_RE.finditer(text or ""):
        d = m.group(1).rstrip(".,;:)”’")
        if len(d) > 8:
            return d
    return ""


def _name_ok(n):
    n = n.strip(" ,;*†‡§0123456789¹²³⁴⁵⁶⁷⁸⁹⁰")
    w = n.split()
    if not 2 <= len(w) <= 5 or len(n) > 60:
        return False
    return all(x[0].isupper() or x.lower() in ("de", "van", "von", "da", "del", "la", "le", "dela", "dos", "bin") for x in w if x[0].isalpha()) \
        and not re.search(r"\d|@|university|college|institute|department|journal", n, re.I)


def split_names(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"(?<=[A-Za-z.])\s*[*†‡§]*\s*\d{1,2}(?:\s*,\s*\d{1,2})*\s*[*†‡§]*(?=\s*(?:,|;|&|\band\b|$))", "", s)   # affiliation numbers such as "Hossain *1" or "Sakil 5,6"
    s = re.sub(r"\s*[*†‡§]+", "", s)
    if not s:
        return []
    parts = [p for p in re.split(r"\s*(?:;|\band\b|&|\n)\s*", s) if p.strip()]
    if len(parts) == 1 and s.count(",") >= 1 and " and " not in s:
        cand = [p.strip() for p in s.split(",")]
        if all(_name_ok(c) for c in cand if c):
            parts = cand
    out = []
    for p in parts:
        pieces = [x.strip() for x in p.split(",")] if p.count(",") >= 1 else [p]
        if len(pieces) > 1 and all(_name_ok(x) for x in pieces if x):
            out.extend(x.strip(" ,;*†‡§0123456789¹²³⁴⁵⁶⁷⁸⁹⁰") for x in pieces if x)
            continue
        p = p.strip(" ,;*†‡§0123456789¹²³⁴⁵⁶⁷⁸⁹⁰")
        if _name_ok(p):
            out.append(p)
    return out[:30]


def guess_title(text, meta, filename=""):
    mt = (meta or {}).get("title", "")
    if mt and len(mt) >= 8 and not _BAD_TITLE.search(mt) and len(mt.split()) >= 3:
        return re.sub(r"\s+", " ", mt)
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").split("\n")]
    lines = [l for l in lines if l][:14]
    best, bi = "", -1
    for i, l in enumerate(lines):
        if _NOT_TITLE_LINE.search(l) or len(l) < 12 or len(l) > 220 or l.endswith("."):
            continue
        if len(l.split()) < 3 or sum(c.isdigit() for c in l) > 4:
            continue
        if len(split_names(l)) >= 2 or re.match(r"(?i)(vol\b|issn|http|review paper|research paper|original|open access)", l):
            continue
        if len(l) > len(best):
            best, bi = l, i
    if best and bi + 1 < len(lines):
        nxt = lines[bi + 1]
        if 12 <= len(nxt) <= 120 and not nxt.endswith(".") and not _NOT_TITLE_LINE.search(nxt) and not split_names(nxt) and len(best) < 150:
            best = best + " " + nxt                    # a title that wraps onto a second line
    return best


def guess_authors(text, meta, title=""):
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").split("\n")]
    lines = [l for l in lines if l][:25]
    joined = []
    for l in lines:
        if joined and joined[-1].endswith(",") and not _NOT_TITLE_LINE.search(l):
            joined[-1] = joined[-1] + " " + l
        else:
            joined.append(l)
    lines = joined
    start = 0
    if title:
        for i, l in enumerate(lines):
            if l and l in title or title.startswith(l[:25]):
                start = i + 1
    for l in lines[start:start + 8]:
        names = split_names(l)
        if len(names) >= 1 and len(l) < 220 and not _NOT_TITLE_LINE.search(l):
            if len(names) >= 2 or len(l.split()) <= 5:
                return names
    names = split_names((meta or {}).get("author", ""))
    return names


def guess_year(text, meta):
    now = datetime.now().year
    head = (text or "")[:4000]
    for pat in (r"(?:©|copyright|published|received|accepted|online)\D{0,40}((?:19|20)\d{2})", r"\((?:19|20)\d{2}\)"):
        m = re.search(pat, head, re.I)
        if m:
            y = re.search(r"(?:19|20)\d{2}", m.group(0)).group(0)
            if 1900 < int(y) <= now + 1:
                return y
    ys = [y for y in re.findall(r"\b((?:19|20)\d{2})\b", head) if 1950 < int(y) <= now + 1]
    if ys:
        return max(set(ys), key=ys.count)
    return (meta or {}).get("year", "")


def guess_vip(text):
    head = (text or "")[:5000]
    m = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d{1,4})\s*[,;|]?\s*(?:no\.?|issue|num(?:ber)?\.?)\s*(\d{1,3})\s*[,;|]?\s*(?:pp?\.?|pages?)?\s*(\d{1,5})\s*[-–—]\s*(\d{1,5})", head)
    if m:
        return m.group(1), m.group(2), m.group(3) + "–" + m.group(4)
    m = re.search(r"(?i)\bvol(?:ume)?\.?\s*(\d{1,4})\s*[,;|]?\s*(?:no\.?|issue|num(?:ber)?\.?)\s*(\d{1,3})", head)
    if m:
        return m.group(1), m.group(2), ""
    return "", "", ""


def guess(text, meta, filename=""):
    title = guess_title(text, meta, filename)
    vol, iss, pg = guess_vip(text)
    return {"title": title, "authors": guess_authors(text, meta, title), "year": guess_year(text, meta), "doi": find_doi(text),
            "journal": "", "volume": vol, "issue": iss, "pages": pg, "publisher": "", "edition": "", "editors": [], "type": ""}


# ── real records from Crossref ────────────────────────────────────────────────────────────────────────────────────
def _from_crossref(it):
    def one(x):
        return (x or [""])[0] if isinstance(x, list) else (x or "")
    au = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (it.get("author") or []) if isinstance(a, dict)]
    ed = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in (it.get("editor") or []) if isinstance(a, dict)]
    year = ""
    for k in ("published", "published-print", "published-online", "issued"):
        dp = (it.get(k) or {}).get("date-parts") or [[]]
        if dp and dp[0]:
            year = str(dp[0][0]); break
    pages = re.sub(r"(?<=\d)-(?=\d)", "–", str(it.get("page") or ""))
    return {"title": re.sub(r"<[^>]+>", "", one(it.get("title"))).strip(), "authors": au, "year": year,
            "journal": re.sub(r"<[^>]+>", "", one(it.get("container-title"))).strip(), "volume": str(it.get("volume") or ""),
            "issue": str(it.get("issue") or ""), "pages": pages, "doi": it.get("DOI") or "", "publisher": it.get("publisher") or "",
            "edition": str(it.get("edition-number") or ""), "editors": ed, "type": it.get("type") or ""}


def lookup_doi(doi, get):
    """get(url, params) -> requests-like response. Returns a details dict or None."""
    try:
        r = get("https://api.crossref.org/works/" + doi, {})
        if r.ok:
            rec = _from_crossref((r.json() or {}).get("message") or {})
            return rec if rec.get("title") else None
    except Exception:
        pass
    return None


def lookup_title(title, authors, year, get):
    """Search Crossref by title. Accept only a very close title (so a wrong paper is never filled in)."""
    if len(title or "") < 15:
        return None
    try:
        r = get("https://api.crossref.org/works", {"query.bibliographic": title[:250], "rows": 3})
        if not r.ok:
            return None
        for it in (r.json() or {}).get("message", {}).get("items", []):
            rec = _from_crossref(it)
            ratio = difflib.SequenceMatcher(None, re.sub(r"\W+", " ", title.lower()).strip(), re.sub(r"\W+", " ", rec["title"].lower()).strip()).ratio()
            if ratio < 0.9:
                continue
            if year and rec["year"] and abs(int(year) - int(rec["year"])) > 1:
                continue
            return rec
    except Exception:
        pass
    return None


# ── OpenAlex and arXiv (the same databases Search Papers uses) ────────────────────────────────────────────────────
_OA_TYPES = {"article": "journal-article", "review": "journal-article", "book": "book", "book-chapter": "book-chapter",
             "preprint": "preprint", "dissertation": "thesis", "report": "report", "dataset": "dataset"}


def _from_openalex(w):
    src = ((w.get("primary_location") or {}).get("source") or {})
    bib = w.get("biblio") or {}
    fp, lp = str(bib.get("first_page") or ""), str(bib.get("last_page") or "")
    pages = (fp + "–" + lp) if fp and lp and fp != lp else fp
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(w.get("doi") or ""), flags=re.I)
    au = [((a.get("author") or {}).get("display_name") or "").strip() for a in (w.get("authorships") or []) if isinstance(a, dict)]
    return {"title": re.sub(r"<[^>]+>", "", str(w.get("title") or "")).strip(), "authors": [a for a in au if a],
            "year": str(w.get("publication_year") or ""), "journal": src.get("display_name") or "", "volume": str(bib.get("volume") or ""),
            "issue": str(bib.get("issue") or ""), "pages": pages, "doi": doi, "publisher": src.get("host_organization_name") or "",
            "edition": "", "editors": [], "type": _OA_TYPES.get(str(w.get("type") or ""), ""),
            "oa_url": ((w.get("open_access") or {}).get("oa_url") or "")}


def _close(a, b):
    n = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()
    return difflib.SequenceMatcher(None, n(a), n(b)).ratio()


def _year_ok(year, rec_year):
    try:
        return not year or not rec_year or abs(int(year) - int(rec_year)) <= 1
    except ValueError:
        return True


def lookup_openalex_doi(doi, get):
    try:
        r = get("https://api.openalex.org/works/https://doi.org/" + doi, {})
        if r.ok:
            rec = _from_openalex(r.json() or {})
            return rec if rec.get("title") else None
    except Exception:
        pass
    return None


def lookup_openalex_title(title, year, get):
    if len(title or "") < 15:
        return None
    try:
        r = get("https://api.openalex.org/works", {"search": title[:250], "per-page": 3})
        if r.ok:
            for w in (r.json() or {}).get("results", []):
                rec = _from_openalex(w)
                if rec["title"] and _close(title, rec["title"]) >= 0.9 and _year_ok(year, rec["year"]):
                    return rec
    except Exception:
        pass
    return None


_ARXIV_ID = re.compile(r"(?i)arxiv\s*:?\s*(\d{4}\.\d{4,5})(?:v\d+)?")


def find_arxiv(text):
    m = _ARXIV_ID.search((text or "")[:6000])
    return m.group(1) if m else ""


def _from_arxiv(xml_text):
    import xml.etree.ElementTree as ET
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", ns):
        title = re.sub(r"\s+", " ", (e.findtext("a:title", "", ns) or "")).strip()
        if not title or title.lower() == "error":
            continue
        aid = re.sub(r"v\d+$", "", (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1])
        out.append({"title": title, "authors": [re.sub(r"\s+", " ", a.findtext("a:name", "", ns) or "").strip() for a in e.findall("a:author", ns)],
                    "year": (e.findtext("a:published", "", ns) or "")[:4], "journal": "arXiv", "volume": "", "issue": "", "pages": "",
                    "doi": (e.findtext("x:doi", "", ns) or "").strip(), "publisher": "arXiv", "edition": "", "editors": [], "type": "preprint",
                    "oa_url": "https://arxiv.org/abs/" + aid if aid else ""})
    return out


def lookup_arxiv(arxiv_id, title, year, get):
    try:
        if arxiv_id:
            r = get("https://export.arxiv.org/api/query", {"id_list": arxiv_id})
            recs = _from_arxiv(r.text) if r.ok else []
            return recs[0] if recs else None
        if len(title or "") >= 15:
            r = get("https://export.arxiv.org/api/query", {"search_query": 'ti:"' + title[:200].replace('"', "") + '"', "max_results": 3})
            for rec in (_from_arxiv(r.text) if r.ok else []):
                if _close(title, rec["title"]) >= 0.9 and _year_ok(year, rec["year"]):
                    return rec
    except Exception:
        pass
    return None


_SRC_NAMES = {"crossref": "Crossref", "openalex": "OpenAlex", "arxiv": "arXiv"}


def merge(recs):
    """recs = [(source, record)] in priority order (Crossref, OpenAlex, arXiv). The first real value for each field wins."""
    out = {"title": "", "authors": [], "year": "", "journal": "", "volume": "", "issue": "", "pages": "", "doi": "", "publisher": "",
           "edition": "", "editors": [], "type": "", "oa_url": ""}
    used = []
    for src, rec in recs:
        gave = False
        for k, v in rec.items():
            if k in out and v and not out[k]:
                out[k] = v; gave = True
        if gave:
            used.append(src)
    return out, used


def _parallel(jobs):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = [(name, ex.submit(fn)) for name, fn in jobs]
        res = []
        for name, f in futs:
            try:
                r = f.result(timeout=20)
            except Exception:
                r = None
            if r:
                res.append((name, r))
    return res


def analyse(text, meta, get, filename=""):
    """The whole job: guess, then replace the guess with real records from Crossref, OpenAlex and arXiv.
    Returns (fields, found_by, notes, sources)."""
    g = guess(text, meta, filename)
    notes, recs, how = [], [], "guess"
    aid = find_arxiv(text)
    doi = g["doi"]
    if doi or aid:
        jobs = []
        if doi:
            jobs += [("crossref", lambda: lookup_doi(doi, get)), ("openalex", lambda: lookup_openalex_doi(doi, get))]
        if aid:
            jobs.append(("arxiv", lambda: lookup_arxiv(aid, "", g["year"], get)))
        recs = _parallel(jobs)
        if recs:
            how = "doi"
        elif doi:
            notes.append("A DOI was found in the file but no database returned a record for it.")
    if not recs and g["title"]:
        t, y = g["title"], g["year"]
        recs = _parallel([("crossref", lambda: lookup_title(t, g["authors"], y, get)),
                          ("openalex", lambda: lookup_openalex_title(t, y, get)),
                          ("arxiv", lambda: lookup_arxiv("", t, y, get))])
        if recs:
            how = "title"
    if recs:
        order = {"crossref": 0, "openalex": 1, "arxiv": 2}
        recs.sort(key=lambda x: order[x[0]])
        rec, used = merge(recs)
        fields = dict(g); fields.update({k: v for k, v in rec.items() if v})
        if not fields.get("doi"): fields["doi"] = g["doi"]
        filled = []
        if not rec.get("authors"):
            a2 = guess_authors(text, meta, rec.get("title") or g["title"])
            fields["authors"] = a2 or []
            if a2: filled.append("authors")
        for k in ("volume", "issue", "pages"):
            if rec.get(k):
                fields[k] = rec[k]
            elif g.get(k):
                filled.append(k)
        if filled:
            notes.append("The databases did not have the " + ", ".join(filled) + " for this paper, so they were read from the file. Please check them.")
        if len(used) > 1:
            notes.append("Details were combined from " + " and ".join(_SRC_NAMES[u] for u in used) + ".")
        if used and used[0] != "crossref":
            notes.append("These details come from " + _SRC_NAMES[used[0]] + ", not Crossref. Check them against the paper.")
        return fields, how, notes, [_SRC_NAMES[u] for u in used]
    if not g["title"]:
        notes.append("A title could not be found. Type it in.")
    if not g["authors"]:
        notes.append("Authors could not be found. Type them in.")
    notes.append("No matching record was found in Crossref, OpenAlex or arXiv, so these details are a guess from the file. Check each one before you use the citation.")
    g.setdefault("oa_url", "")
    return g, "guess", notes, []
