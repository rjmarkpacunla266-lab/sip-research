#!/usr/bin/env python3
"""install_smartcheck.py - run from the project folder (~/W):  python3 install_smartcheck.py
Finds out why smart search shows no change. Adds:
  * /api/smart-status  (open it in your browser) - asks OpenAlex and the private service and prints the real answer
  * "engine" and "smart_hits" in every search answer: which service did the search, and how many results smart search touched
Needs install_smart first. Do not re-run install_privateapi afterwards (it would remove the check route). Safe to run twice."""
import json, os, re, sys

HELP = None
SPEC = json.loads('{"search.py": [["    data = None\\n    if SEARCH_API_URL and SEARCH_API_KEY and post is None:\\n        try:\\n            data = _run_search_remote(query, page)\\n        except Exception:\\n            data = None\\n    if data is None:\\n        return _run_search_local(query, page, user, log, post)\\n", "    data, why = None, \\"private service not set up\\"\\n    if SEARCH_API_URL and SEARCH_API_KEY and post is None:\\n        why = \\"private service did not give a usable answer (wrong key, asleep, or error)\\"\\n        try:\\n            data = _run_search_remote(query, page)\\n        except Exception:\\n            data = None\\n    elif post is not None:\\n        why = \\"search with operators runs on this site\\"\\n    if data is None:\\n        out = _run_search_local(query, page, user, log, post)\\n        out[\\"engine\\"] = \\"this site (\\" + why + \\")\\"\\n        return out\\n    data[\\"engine\\"] = \\"private service\\"\\n"], ["def _add_smart(", "def _smart_diag(query):\\n    \\"\\"\\"What would happen if smart search ran now? Shows the real answer from OpenAlex. No secrets.\\"\\"\\"\\n    out = {\\"smart_search_switch_on\\": SMART_ON, \\"openalex_key_set\\": bool(OPENALEX_API_KEY),\\n           \\"searches_today\\": _smart_day[\\"n\\"], \\"daily_limit\\": SMART_DAILY_LIMIT}\\n    params = {\\"search.semantic\\": query[:2000], \\"per-page\\": 5}\\n    if OPENALEX_API_KEY:\\n        params[\\"api_key\\"] = OPENALEX_API_KEY\\n    try:\\n        resp = requests.get(OPENALEX_URL, params=params, timeout=15)\\n        out[\\"openalex_http_status\\"] = resp.status_code\\n        try:\\n            body = resp.json()\\n        except Exception:\\n            body = {}\\n        if resp.ok:\\n            out[\\"papers_returned\\"] = len(body.get(\\"results\\", []))\\n            out[\\"first_titles\\"] = [(p.get(\\"title\\") or p.get(\\"display_name\\") or \\"\\")[:80] for p in body.get(\\"results\\", [])[:3]]\\n        else:\\n            out[\\"openalex_message\\"] = str(body.get(\\"message\\") or body.get(\\"error\\") or resp.text)[:300]\\n    except Exception as e:\\n        out[\\"error\\"] = type(e).__name__ + \\": \\" + str(e)[:150]\\n    return out\\n\\ndef _add_smart("], ["@search_bp.route(\\"/api/search\\")", "@search_bp.route(\\"/api/smart-status\\")\\n@login_required\\ndef smart_status():\\n    \\"\\"\\"Open /api/smart-status in your browser while logged in (guest is fine) to see why smart search is or is not working.\\"\\"\\"\\n    q = request.args.get(\\"q\\", \\"tomato fertilizer growth\\")[:200]\\n    out = {\\"private_service_set\\": bool(SEARCH_API_URL and SEARCH_API_KEY)}\\n    if SEARCH_API_URL and SEARCH_API_KEY:\\n        try:\\n            r = requests.get(SEARCH_API_URL + \\"/v1/smart-check\\", params={\\"q\\": q}, timeout=45,\\n                             headers={\\"X-Api-Key\\": SEARCH_API_KEY})\\n            out[\\"service_http_status\\"] = r.status_code\\n            try:\\n                out[\\"service\\"] = r.json()\\n            except Exception:\\n                out[\\"service\\"] = r.text[:200]\\n        except Exception as e:\\n            out[\\"service_error\\"] = type(e).__name__\\n    else:\\n        out[\\"this_site\\"] = _smart_diag(q)\\n    return jsonify(out)\\n\\n@search_bp.route(\\"/api/search\\")"], ["            \\"did_you_mean\\": suggestion,\\n", "            \\"did_you_mean\\": suggestion,\\n            \\"smart_hits\\": sum(1 for p in results if p.get(\\"smart\\")),\\n"]], "search_api.py": [["@app.route(\\"/v1/health\\")", "@app.route(\\"/v1/smart-check\\")\\ndef v1_smart_check():\\n    return jsonify(S._smart_diag(request.args.get(\\"q\\", \\"tomato fertilizer growth\\")[:200]))\\n\\n@app.route(\\"/v1/health\\")"]]}')

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
