"""engine.py — ONE shared paper-search engine.
Search Papers, Answer Finder, Source Tracer and Topic Finder all use these functions, so the
filters (sources, years, Open Access, sort, word-in) behave the same everywhere."""
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from core import (RESULTS_PER_SOURCE, search_arxiv, search_pubmed, search_crossref,
                  search_europe_pmc, search_openalex, _safe_terms)

ALL_SOURCES = ["openalex", "arxiv", "pubmed", "crossref", "europepmc"]
SOURCE_LABELS = {"openalex": "OpenAlex", "arxiv": "arXiv", "pubmed": "PubMed",
                 "crossref": "Crossref", "europepmc": "Europe PMC"}

def to_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return None

def read_filters(get):
    """Read the shared filters. `get` is request.args.get, or a dict's .get (JSON body)."""
    src = get("sources")
    if isinstance(src, str):
        src = src.split(",")
    chosen = [s for s in (src if isinstance(src, (list, tuple)) else []) if s in ALL_SOURCES]
    oa = get("oa")
    sort = get("sort")
    field = get("field")
    yf, yt = to_int(get("year_from")), to_int(get("year_to"))
    if yf and yt and yf > yt:
        yf, yt = yt, yf                                   # forgiving: swap reversed years
    return {
        "sources":   chosen or list(ALL_SOURCES),
        "year_from": yf,
        "year_to":   yt,
        "oa_only":   oa is True or str(oa).lower() in ("1", "true"),
        "sort":      sort if sort in ("cited", "recent", "match") else "cited",
        "field":     field if field in ("title", "abstract", "both") else "both",
    }

def merge_dedupe(papers):
    """One entry per paper. Same DOI/title in several sources -> keep the most-cited copy."""
    best = {}
    for p in papers:
        doi = (p.get("doi") or "").strip().lower().replace("https://doi.org/", "")
        key = doi or (p.get("title") or "").strip().lower()[:60]
        if not key:
            continue
        old = best.get(key)
        if old is None:
            best[key] = p
            continue
        keep, other = (p, old) if (p.get("citations") or 0) > (old.get("citations") or 0) else (old, p)
        if other.get("is_oa") and not keep.get("is_oa"):
            keep["is_oa"] = True
            keep["oa_url"] = keep.get("oa_url") or other.get("oa_url")
        best[key] = keep
    return list(best.values())

def sort_date(p):
    """ISO date for ordering; year-only papers sort as 'YYYY-00-00' (before dated ones that year)."""
    d = (p.get("date") or "").strip()
    if d:
        return d
    y = to_int(p.get("year"))
    return f"{y:04d}-00-00" if y else ""

def norm(text):
    """Lowercase and strip accents (é -> e)."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()

def compact(text):
    """Letters+digits only (spaces, - _ / . etc. removed), plus a flag per character
    saying whether it begins a word in the original text."""
    chars, starts, new_word = [], [], True
    for c in norm(text):
        if c.isalnum():
            chars.append(c); starts.append(new_word); new_word = False
        else:
            new_word = True
    return "".join(chars), starts

def term_in(text, term):
    """Is the word in the text? Handles plurals, accents and spelling variants such as
    'bio-fuel', 'bio fuel', 'bio_fuel' for 'biofuel'. A compact match must begin at the
    start of a word, so 'heart' does not match 'the art'."""
    t = norm(term)
    if t and t in norm(text):
        return True
    ct = "".join(c for c in t if c.isalnum())
    if not ct:
        return False
    comp, starts = compact(text)
    i = comp.find(ct)
    while i != -1:
        if starts[i]:
            return True
        i = comp.find(ct, i + 1)
    return False

def matches_field(p, terms, field):
    """Check that every search word really sits in the chosen part of the paper."""
    title    = p.get("title") or ""
    abstract = p.get("abstract") or ""
    if field == "title":
        return all(term_in(title, t) for t in terms)
    if abstract:
        if field == "abstract":
            return all(term_in(abstract, t) for t in terms)
        return all(term_in(title, t) or term_in(abstract, t) for t in terms)
    if "pubmed" in (p.get("data_source") or "").lower():
        return True                    # PubMed matched in its own index; it sends no abstract
    return field == "both" and all(term_in(title, t) for t in terms)

def relevance(p, terms):
    """How well a paper matches the search words: title hits count double."""
    title, abstract = p.get("title") or "", p.get("abstract") or ""
    return sum((2 if term_in(title, t) else 0) + (1 if term_in(abstract, t) else 0) for t in terms)

def apply_filters_and_sort(papers, f, query=""):
    """Safety net: sources that ignore year/OA/field options are filtered here."""
    terms = _safe_terms(query)
    out = []
    for p in papers:
        if f.get("field") in ("title", "abstract") and terms and not matches_field(p, terms, f["field"]):
            continue
        y = to_int(p.get("year"))
        if f["year_from"] and (y is None or y < f["year_from"]): continue
        if f["year_to"]   and (y is None or y > f["year_to"]):   continue
        if f["oa_only"] and not p.get("is_oa"):                  continue
        out.append(p)
    if f["sort"] == "match" and terms:
        out.sort(key=lambda x: (relevance(x, terms), x.get("citations") or 0), reverse=True)
    elif f["sort"] == "recent":
        out.sort(key=lambda x: (sort_date(x), x.get("citations") or 0), reverse=True)
    else:
        out.sort(key=lambda x: x.get("citations") or 0, reverse=True)
    return out

def search_sources(query, page, f, per_source=RESULTS_PER_SOURCE, loose=False):
    """Ask every selected source in parallel. Returns (raw_papers, openalex_total)."""
    kw = dict(year_from=f["year_from"], year_to=f["year_to"], oa_only=f["oa_only"],
              sort=f["sort"], field=f["field"], loose=loose)
    fns = {"openalex": search_openalex, "arxiv": search_arxiv, "pubmed": search_pubmed,
           "crossref": search_crossref, "europepmc": search_europe_pmc}
    raw, total = [], 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        jobs = {ex.submit(fns[s], query, page, per_source, **kw): s for s in f["sources"] if s in fns}
        for fut in as_completed(jobs):
            try:
                res = fut.result()
                if jobs[fut] == "openalex":
                    papers, count = res
                    total += count
                    raw.extend(papers)
                else:
                    raw.extend(res)
            except Exception:
                pass
    return raw, total

def search_papers(query, page, f, per_source=RESULTS_PER_SOURCE, loose=False):
    """Search + merge duplicates + apply filters + sort. Returns (papers, openalex_total)."""
    raw, total = search_sources(query, page, f, per_source, loose)
    return apply_filters_and_sort(merge_dedupe(raw), f, query), total
