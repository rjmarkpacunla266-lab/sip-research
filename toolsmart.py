"""toolsmart.py: the same OpenAlex "meaning" search (and the optional AI ranking) for the tools
(Answer Finder, Source Tracer, Topic Finder). Lives next to engine.py.

It only finds and orders REAL papers. It never writes text. Everything is optional and fails safe: if anything goes wrong
the tools simply carry on with their normal searches.

Settings (Render > Environment, all optional):
  TOOLS_SMART              "1" (default) = on, "0" = off
  TOOLS_SMART_DAILY_LIMIT  most meaning searches per day for the tools (default 60)
  AI_BASE_URL, AI_API_KEY, AI_EMBED_MODEL ...  the optional AI ranking (smartrank.py). Nothing to change here:
                           when those are set, the tools use it by themselves.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

_pool = ThreadPoolExecutor(max_workers=4)
_day = {"day": None, "n": 0}
VIA = "similar meaning (OpenAlex AI search)"


def _env(name, default=""):
    return (os.environ.get(name, default) or "").strip()


def enabled():
    return _env("TOOLS_SMART", "1") == "1"


def _limit():
    try:
        return int(_env("TOOLS_SMART_DAILY_LIMIT", "60"))
    except ValueError:
        return 60


def _allowed():
    today = time.strftime("%Y-%m-%d")
    if _day["day"] != today:
        _day["day"], _day["n"] = today, 0
    if _day["n"] >= _limit():
        return False
    _day["n"] += 1
    return True


def _fetch(query, f):
    import core                                       # late import: avoids circular imports at start-up
    params = {"search.semantic": query[:2000], "per-page": 50}
    flt = []
    if f.get("year_from"):
        flt.append("from_publication_date:%s-01-01" % f["year_from"])
    if f.get("year_to"):
        flt.append("to_publication_date:%s-12-31" % f["year_to"])
    if f.get("oa_only"):
        flt.append("is_oa:true")
    if flt:
        params["filter"] = ",".join(flt)
    key = getattr(core, "OPENALEX_API_KEY", "")
    if key:
        params["api_key"] = key
    resp = requests.get(core.OPENALEX_URL, params=params, timeout=12)
    if not resp.ok:
        return []
    out = []
    for w in resp.json().get("results", []):
        p = core.format_paper(w)
        p["via"] = VIA
        p["smart"] = True
        out.append(p)
    return out


def start(query, f):
    """Begin the meaning search in the background so it runs while the normal searches run. Returns a handle or None."""
    try:
        query = (query or "").strip()
        if not query or not enabled():
            return None
        if "openalex" not in (f.get("sources") or ["openalex"]):
            return None                               # the person switched OpenAlex off
        if not _allowed():
            return None
        return _pool.submit(_fetch, query, f)
    except Exception:
        return None


def result(handle, timeout=14):
    """The papers from start(), or [] if it was off, slow or failed."""
    if handle is None:
        return []
    try:
        return handle.result(timeout=timeout) or []
    except Exception:
        return []


# ── optional AI ranking (uses smartrank.py when it is set up; otherwise does nothing) ──
def _sims(query, papers):
    try:
        import smartrank
        if not smartrank.available():
            return None
        pool = [p for p in papers if p.get("title")][:smartrank.pool_size()]
        s = smartrank.similarities(query, pool)
        if not s or len(s) != len(pool):
            return None
        lo, hi = min(s), max(s)
        span = (hi - lo) or 1.0
        return pool, [(x - lo) / span for x in s]
    except Exception:
        return None


def ai_mark(papers, query):
    """Give the closest papers an 'ai_bonus' between 0 and 1. No-op when the AI is off."""
    got = _sims(query, papers)
    if got:
        for p, n in zip(*got):
            p["ai_bonus"] = round(n, 3)


def ai_judge(cands, query, limit=None):
    """Answer Finder: the AI only CHECKS how well each found sentence answers the question and moves the better ones up.
    It never writes or changes a sentence. Each candidate gets 'ai_bonus' between 0 and 1. No-op when the AI is off."""
    try:
        import smartrank
        if not cands or not smartrank.available():
            return
        top = sorted(cands, key=lambda c: c.get("score") or 0, reverse=True)[:limit or smartrank.pool_size()]
        s = smartrank.similarities(query, [{"title": c.get("text") or ""} for c in top])
        if not s or len(s) != len(top):
            return
        lo, hi = min(s), max(s)
        span = (hi - lo) or 1.0
        for c, x in zip(top, s):
            c["ai_bonus"] = round((x - lo) / span, 3)
    except Exception:
        return


def ai_ranks(papers, query, ranks, keyfn, weight=0.4):
    """Add a bonus to a {paper key: 0..1} rank table. Returns the table (unchanged when the AI is off)."""
    got = _sims(query, papers)
    if got:
        for p, n in zip(*got):
            k = keyfn(p)
            ranks[k] = min(1.0, ranks.get(k, 0.0) + weight * n)
    return ranks


def ai_sort(papers, query, f):
    """For the 'best match' order only: put the papers the AI finds closest first."""
    if (f or {}).get("sort") != "match":
        return
    ai_mark(papers, query)
    if any(p.get("ai_bonus") for p in papers):
        papers.sort(key=lambda p: p.get("ai_bonus") or 0.0, reverse=True)       # stable


def diag():
    return {"tools_smart_on": enabled(), "tools_searches_today": _day["n"], "tools_daily_limit": _limit()}
