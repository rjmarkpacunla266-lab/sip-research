"""search_api.py - the PRIVATE search service (second Render web service, same repo).
Start command on Render:  gunicorn search_api:app
Environment variable it needs:  SEARCH_API_KEY  (a long random secret, the same value the main site has)
It has no pages, no login and no database. It only answers GET /v1/search for the main site,
and only when the request carries the right key in the X-Api-Key header."""
import hmac, os
from flask import Flask, jsonify, request
try:
    import search as S
except ImportError:
    from routes import search as S

S.SEARCH_API_URL = ""                       # this service always searches by itself, never asks another service
app = Flask(__name__)
KEY = os.environ.get("SEARCH_API_KEY", "")

@app.before_request
def _check_key():
    sent = request.headers.get("X-Api-Key", "")
    if not KEY or not hmac.compare_digest(sent.encode(), KEY.encode()):
        return ("Not found", 404)            # same answer as any other address, so it tells strangers nothing

@app.after_request
def _headers(r):
    r.headers["X-Robots-Tag"] = "noindex, nofollow"
    r.headers["Cache-Control"] = "no-store"
    return r

@app.route("/v1/search")
def v1_search():
    query = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    if not query:
        return jsonify({"error": "q required"}), 400
    return jsonify(S._run_search_plain(query, page, None, log=False))

@app.route("/v1/smart-check")
def v1_smart_check():
    return jsonify(S._smart_diag(request.args.get("q", "tomato fertilizer growth")[:200]))

@app.route("/v1/health")
def v1_health():
    return jsonify({"ok": True})
