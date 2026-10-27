"""papercount.py — how many papers Sturch can search, counted live from each database's own API.

Each source is asked for its own total (and its open-access total where it reports one). The numbers are cached
for a few hours so the landing page never waits on, or hammers, the databases.

The same paper often sits in several databases (a PubMed paper is usually in OpenAlex, Crossref and Europe PMC too), and
nothing here can tell exactly how many are duplicates. So two honest numbers are given:
  unique_min = the biggest single source (there are at least this many different papers)
  sum_total  = every source added together (there are at most this many records, counting repeats)
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

TTL = 6 * 3600            # a fresh result is reused for 6 hours
RETRY_AFTER = 300         # after a failed refresh, try again in 5 minutes
DEADLINE = 14.0           # the first (uncached) count may take this long at most
UA = {"User-Agent": "Sturch/3.0 (academic research tool; paper counter)"}

_lock = threading.Lock()
_cache = {"data": None, "at": 0.0, "next_try": 0.0, "busy": False}


def _get(url, params=None, timeout=10):
    r = requests.get(url, params=params, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r


def _int(v):
    try:
        n = int(v)
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


# ── one function per source: -> {"total": int|None, "oa": int|None} (None = the source did not answer / does not report it) ──
def _openalex():
    base = "https://api.openalex.org/works"
    total = _int(_get(base, {"per-page": 1}).json().get("meta", {}).get("count"))
    oa = _int(_get(base, {"filter": "is_oa:true", "per-page": 1}).json().get("meta", {}).get("count"))
    return {"total": total, "oa": oa}


def _crossref():
    # Crossref says how many records it holds but does not say which are open access, so no OA number is given for it
    d = _get("https://api.crossref.org/works", {"rows": 0}).json()
    return {"total": _int((d.get("message") or {}).get("total-results")), "oa": None}


def _arxiv():
    # everything on arXiv is free to read, so its open-access number is its total
    total = None
    for q in ("submittedDate:[190001010000 TO 210012312359]", "all:*"):
        try:
            xml = _get("https://export.arxiv.org/api/query", {"search_query": q, "max_results": 0}, timeout=12).text
            m = re.search(r"<opensearch:totalResults[^>]*>\s*(\d+)\s*<", xml)
            total = _int(m.group(1)) if m else None
        except Exception:
            total = None
        if total:
            break
    return {"total": total, "oa": total}


SOURCES = [
    ("openalex", "OpenAlex", _openalex),
    ("crossref", "Crossref", _crossref),
    ("arxiv", "arXiv", _arxiv),
]


def _count_all():
    """Ask every source at the same time. A source that fails is reported as unavailable, never guessed."""
    ex = ThreadPoolExecutor(max_workers=len(SOURCES))
    futs = {key: ex.submit(fn) for key, _, fn in SOURCES}
    end = time.time() + DEADLINE
    rows = []
    for key, name, _ in SOURCES:
        try:
            r = futs[key].result(timeout=max(0.5, end - time.time()))
        except Exception:
            r = {"total": None, "oa": None}
        total, oa = r.get("total"), r.get("oa")
        if oa is not None and total is not None and oa > total:      # a source answered inconsistently: do not trust the OA part
            oa = None
        rows.append({"key": key, "name": name, "total": total, "oa": oa,
                     "closed": (total - oa) if (total is not None and oa is not None) else None,
                     "oa_reported": oa is not None})
    ex.shutdown(wait=False)
    return _summarise(rows)


def _summarise(rows):
    ok = [r for r in rows if r["total"]]
    oa_ok = [r for r in rows if r["oa"]]
    return {
        "sources": rows,
        "sum_total": sum(r["total"] for r in ok),
        "unique_min": max((r["total"] for r in ok), default=0),
        "sum_oa": sum(r["oa"] for r in oa_ok),
        "oa_min": max((r["oa"] for r in oa_ok), default=0),
        "complete": len(ok) == len(rows),
        "answered": len(ok),
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    }


def _refresh():
    try:
        data = _count_all()
        with _lock:
            old = _cache["data"]
            if data["answered"] == 0:                               # nothing answered: keep the last good numbers (if any), try again soon
                _cache["next_try"] = time.time() + RETRY_AFTER
            else:
                if old and data["answered"] < old["answered"]:      # keep a source's last good number if it fails this time
                    last = {r["key"]: r for r in old["sources"]}
                    rows = [r if r["total"] else (last.get(r["key"]) or r) for r in data["sources"]]
                    data = _summarise(rows)
                _cache.update(data=data, at=time.time(), next_try=time.time() + (TTL if data["complete"] else RETRY_AFTER))
    finally:
        with _lock:
            _cache["busy"] = False


def get_counts():
    """-> the numbers (a dict) or None when nothing has ever been counted. Returns the cached copy at once when there is one."""
    now = time.time()
    with _lock:
        data = _cache["data"]
        due = now >= _cache["next_try"] and not _cache["busy"]      # next_try = when these numbers go stale (or when to retry)
        if due:
            _cache["busy"] = True
    if due:
        if data is None:
            _refresh()                                               # very first count: wait for it (at most DEADLINE seconds)
        else:
            threading.Thread(target=_refresh, daemon=True).start()   # old numbers are shown while new ones are counted
    with _lock:
        return _cache["data"]
