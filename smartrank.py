"""smartrank.py - the ONE place where an outside AI service plugs into Sturch.

The AI is used for RANKING ONLY. It turns your search and each paper's title (and abstract) into lists of
numbers (embeddings); papers whose numbers are closest to your search move up. It never writes text, so it
cannot make up answers, and every result is still a real paper from OpenAlex, arXiv or Crossref.

It works with any OpenAI-compatible embeddings API (OpenAI, Azure OpenAI, and many others).
Settings, all in Render > Environment (nothing is on until AI_BASE_URL, AI_API_KEY and AI_EMBED_MODEL are set):
  AI_BASE_URL       e.g. https://api.openai.com/v1      (Sturch calls BASE_URL + /embeddings)
  AI_API_KEY        the secret key (never put it in code or chat)
  AI_EMBED_MODEL    e.g. text-embedding-3-small
  AI_AUTH_HEADER    optional. "api-key" for Azure. Default: Authorization (Bearer)
  AI_QUERY_PARAMS   optional. e.g. api-version=2024-02-01 (Azure)
  AI_DAILY_LIMIT    smart searches per day that may use the AI (default 50)
  AI_POOL           how many top papers are re-ranked (default 30)
  AI_USE_ABSTRACTS  1 = send title + start of abstract (default), 0 = titles only
To use a different AI service later, change these settings only. No code change is needed."""
import hashlib
import math
import os
import time
from urllib.parse import urlparse

import requests

_cache = {}                         # text hash -> vector (saves calls and tokens for repeat papers)
_day = {"day": None, "n": 0}

def _env(name, default=""):
    return (os.environ.get(name, default) or "").strip()

def available():
    """True when the AI is set up and today's limit is not used up."""
    if not (_env("AI_BASE_URL") and _env("AI_API_KEY") and _env("AI_EMBED_MODEL")):
        return False
    _roll()
    return _day["n"] < _limit()

def _limit():
    try:
        return int(_env("AI_DAILY_LIMIT", "50"))
    except ValueError:
        return 50

def pool_size():
    try:
        return max(5, min(60, int(_env("AI_POOL", "30"))))
    except ValueError:
        return 30

def _roll():
    today = time.strftime("%Y-%m-%d")
    if _day["day"] != today:
        _day["day"], _day["n"] = today, 0

def paper_text(p):
    """What is sent to the AI for one paper: the title, plus the start of the abstract unless turned off."""
    t = (p.get("title") or "").strip()
    if _env("AI_USE_ABSTRACTS", "1") != "0":
        a = (p.get("abstract") or "").strip()
        if a:
            t += ". " + a[:600]
    return t[:900]

def _request(texts, timeout=15):
    """POST the texts to the embeddings API. Returns (vectors or None, http_status, error_text)."""
    url = _env("AI_BASE_URL").rstrip("/") + "/embeddings"
    qp = _env("AI_QUERY_PARAMS")
    if qp:
        url += ("&" if "?" in url else "?") + qp
    header = _env("AI_AUTH_HEADER", "Authorization")
    key = _env("AI_API_KEY")
    headers = {"Content-Type": "application/json",
               header: ("Bearer " + key) if header.lower() == "authorization" else key}
    try:
        r = requests.post(url, json={"model": _env("AI_EMBED_MODEL"), "input": texts}, headers=headers, timeout=timeout)
    except Exception as e:
        return None, 0, type(e).__name__
    if not r.ok:
        try:
            msg = str(r.json().get("error", r.text))[:200]
        except Exception:
            msg = r.text[:200]
        return None, r.status_code, msg
    try:
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in data]
    except Exception:
        return None, r.status_code, "unexpected answer"
    if len(vecs) != len(texts):
        return None, r.status_code, "wrong number of vectors"
    return vecs, r.status_code, ""

def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0

def similarities(query, papers):
    """Closeness (cosine, about 0..1) of each paper to the query, in the same order. None if the AI is off
    or fails, so the caller falls back to the normal rules. One API call per search at most."""
    if not papers or not available():
        return None
    texts = [paper_text(p) for p in papers]
    keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    qkey = "q:" + hashlib.sha1(query.encode("utf-8")).hexdigest()
    need = [i for i, k in enumerate(keys) if k not in _cache]
    send = ([query] if qkey not in _cache else []) + [texts[i] for i in need]
    if send:
        _roll(); _day["n"] += 1
        vecs, status, err = _request(send)
        if vecs is None:
            return None
        j = 0
        if qkey not in _cache:
            _cache[qkey] = vecs[0]; j = 1
        for i in need:
            _cache[keys[i]] = vecs[j]; j += 1
        while len(_cache) > 3000:                      # keep memory small
            _cache.pop(next(iter(_cache)))
    qv = _cache[qkey]
    return [_cos(qv, _cache[k]) for k in keys]

def diag():
    """For /api/smart-status. Never includes the key."""
    out = {"ai_configured": bool(_env("AI_BASE_URL") and _env("AI_API_KEY") and _env("AI_EMBED_MODEL"))}
    if not out["ai_configured"]:
        out["note"] = "Set AI_BASE_URL, AI_API_KEY and AI_EMBED_MODEL in Render to turn the AI ranking on."
        return out
    _roll()
    out.update({"host": urlparse(_env("AI_BASE_URL")).netloc, "model": _env("AI_EMBED_MODEL"),
                "uses_abstracts": _env("AI_USE_ABSTRACTS", "1") != "0",
                "ai_searches_today": _day["n"], "daily_limit": _limit(), "pool": pool_size()})
    vecs, status, err = _request(["test"], timeout=10)
    out["test_http_status"] = status
    if vecs:
        out["test_vector_size"] = len(vecs[0])
    else:
        out["test_error"] = err
    return out
