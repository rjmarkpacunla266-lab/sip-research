#!/usr/bin/env python3
"""install_smart.py - run from the project folder (~/W):  python3 install_smart.py
Adds (1) support for a free OpenAlex API key (OPENALEX_API_KEY) and (2) an optional "smart search"
that asks OpenAlex's own meaning-based search for more papers and ranks them. Smart search is OFF
until you set SMART_SEARCH=1 on the private service. Without any setting nothing changes.
Needs install_search2 to 9 and install_privateapi first. Safe to run twice."""
import json, os, re, sys

HELP = None
SPEC = json.loads('{"search.py": [["login_required, get_user, OPENALEX_URL,", "login_required, get_user, OPENALEX_URL, OPENALEX_API_KEY,"], ["    params = {\\"search\\": query, \\"per-page\\": 20, \\"page\\": 1, \\"sort\\": \\"cited_by_count:desc\\"}\\n", "    params = {\\"search\\": query, \\"per-page\\": 20, \\"page\\": 1, \\"sort\\": \\"cited_by_count:desc\\"}\\n    if OPENALEX_API_KEY:\\n        params[\\"api_key\\"] = OPENALEX_API_KEY\\n"], ["def _run_search_local(query, page, user, log=True, post=None):\\n", "# ─── Optional \\"smart\\" search: OpenAlex\'s own semantic (meaning-based) search ─────────────\\n# OFF unless SMART_SEARCH=1 is set (only on the private service). It only finds and orders REAL\\n# papers from OpenAlex; it never writes text. It stops by itself after SMART_DAILY_LIMIT searches\\n# a day (default 300) to protect the free allowance, and any error falls back to the normal rules.\\nSMART_ON = os.environ.get(\\"SMART_SEARCH\\", \\"\\") == \\"1\\"\\ntry:\\n    SMART_DAILY_LIMIT = int(os.environ.get(\\"SMART_DAILY_LIMIT\\", \\"300\\"))\\nexcept ValueError:\\n    SMART_DAILY_LIMIT = 300\\n_smart_day = {\\"day\\": None, \\"n\\": 0}\\n\\ndef _semantic_matches(query, f):\\n    \\"\\"\\"Up to 50 papers whose meaning is closest to the search, in OpenAlex\'s order. [] if off/failed.\\"\\"\\"\\n    if not SMART_ON or f.get(\\"field\\") == \\"fulltext\\":\\n        return []\\n    today = datetime.now().date().isoformat()\\n    if _smart_day[\\"day\\"] != today:\\n        _smart_day[\\"day\\"], _smart_day[\\"n\\"] = today, 0\\n    if _smart_day[\\"n\\"] >= SMART_DAILY_LIMIT:\\n        return []\\n    _smart_day[\\"n\\"] += 1\\n    base = []\\n    if f[\\"year_from\\"]: base.append(\\"from_publication_date:%s-01-01\\" % f[\\"year_from\\"])\\n    if f[\\"year_to\\"]:   base.append(\\"to_publication_date:%s-12-31\\" % f[\\"year_to\\"])\\n    if f[\\"oa_only\\"]:   base.append(\\"is_oa:true\\")\\n    params = {\\"search.semantic\\": query[:2000], \\"per-page\\": 50}\\n    if base:\\n        params[\\"filter\\"] = \\",\\".join(base)\\n    if OPENALEX_API_KEY:\\n        params[\\"api_key\\"] = OPENALEX_API_KEY\\n    try:\\n        resp = requests.get(OPENALEX_URL, params=params, timeout=15)\\n        if not resp.ok:\\n            return []\\n        return [format_paper(p) for p in resp.json().get(\\"results\\", [])]\\n    except Exception:\\n        return []\\n\\ndef _add_smart(results, query, f, sq):\\n    \\"\\"\\"Papers the rules AND the meaning search both found get a bonus; a few papers only the meaning\\n    search found are added, marked \'similar meaning\'. Returns the new list.\\"\\"\\"\\n    sem = _semantic_matches(query, f)\\n    if not sem:\\n        return results\\n    f2 = dict(f); f2[\\"field\\"] = \\"both\\"\\n    sem = apply_filters_and_sort(merge_dedupe(sem), f2, sq)      # keeps the filters (years, open access)\\n    order = {}\\n    raw_rank = 0\\n    for p in sem:\\n        order[_key(p)] = raw_rank; raw_rank += 1\\n    have = {_key(p) for p in results}\\n    for p in results:\\n        i = order.get(_key(p))\\n        if i is not None:\\n            p[\\"score\\"] = round((p.get(\\"score\\") or 0) + 3 * (1 - i / max(1, len(sem))), 2)\\n            p[\\"smart\\"] = True\\n    extra = []\\n    for p in sem[:15]:\\n        if _key(p) not in have:\\n            p[\\"via\\"] = \\"similar meaning (OpenAlex AI search)\\"\\n            p[\\"score\\"] = round((p.get(\\"score\\") or 0) + 3 * (1 - order[_key(p)] / max(1, len(sem))), 2)\\n            p[\\"smart\\"] = True\\n            extra.append(p)\\n    results = results + extra\\n    if f[\\"sort\\"] == \\"match\\":\\n        results.sort(key=lambda x: (x.get(\\"score\\") or 0, x.get(\\"citations\\") or 0), reverse=True)\\n    return results\\n\\ndef _run_search_local(query, page, user, log=True, post=None):\\n"], ["    _mark_fulltext(results, sq, f)\\n    relaxed = False\\n", "    _mark_fulltext(results, sq, f)\\n    if page == 1 and SMART_ON:\\n        results = _add_smart(results, query, f, sq)\\n    relaxed = False\\n"]], "core.py": [["OPENALEX_URL        = \\"https://api.openalex.org/works\\"\\n", "OPENALEX_URL        = \\"https://api.openalex.org/works\\"\\nOPENALEX_API_KEY    = os.getenv(\\"OPENALEX_API_KEY\\", \\"\\")     # free key from OpenAlex: raises the daily free allowance\\n"], ["        try:\\n            resp = requests.get(OPENALEX_URL, params=params, timeout=15)\\n            data = resp.json()\\n", "        if OPENALEX_API_KEY:\\n            params[\\"api_key\\"] = OPENALEX_API_KEY\\n        try:\\n            resp = requests.get(OPENALEX_URL, params=params, timeout=15)\\n            data = resp.json()\\n"]]}')

def find(name):
    for c in (name, os.path.join("routes", name), os.path.join("templates", name)):
        if os.path.isfile(c):
            return c
    return None

def read(p): return open(p, encoding="utf-8").read()
def write(p, t): open(p, "w", encoding="utf-8", newline="").write(t)

ok, warn = [], []

# --- new helper module -----------------------------------------------------

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
