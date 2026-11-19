"""routes/cite_file_tool.py: Cite My File. Upload (or paste) a PDF / DOCX / DOC and get the four citation styles.

The file is read in memory and thrown away: it is never written to disk and never sent to the database.
Only if the person presses Save does the page send the small set of citation details (title, authors, year ...) to the
normal saved-papers route, so Supabase does not get the file or its text.
"""
import os
import time
import traceback

import requests
from flask import render_template, request, jsonify, session

import citefile
from core import login_required
from routes.tools_common import tools_bp

_WINDOW, _LIMIT = 600, 30                       # at most 30 files / pasted texts per person per 10 minutes
_hits = {}


def _too_many():
    uid = str(session.get("user_id") or request.remote_addr or "?")
    now = time.time()
    q = [t for t in _hits.get(uid, []) if now - t < _WINDOW]
    if len(q) >= _LIMIT:
        _hits[uid] = q
        return True
    q.append(now)
    _hits[uid] = q
    if len(_hits) > 5000:                       # keep the table small
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > _WINDOW]:
            _hits.pop(k, None)
    return False


def _get(url, params):
    mail = os.environ.get("CROSSREF_EMAIL", "").strip()
    ua = "Sturch/1.0" + (f" (mailto:{mail})" if mail else "")
    params = dict(params or {})
    if "api.openalex.org" in url:
        key = os.environ.get("OPENALEX_API_KEY", "").strip()
        if key:
            params["api_key"] = key
        if mail:
            params["mailto"] = mail
    return requests.get(url, params=params, timeout=8, headers={"User-Agent": ua})


def _no_store(resp, code=200):
    resp.headers["Cache-Control"] = "no-store"
    return resp, code


@tools_bp.route("/cite-file")
@login_required
def cite_file_page():
    return render_template("cite_file.html")


@tools_bp.route("/api/cite-file/read", methods=["POST"])
@login_required
def cite_file_read():
    try:
        if _too_many():
            return _no_store(jsonify({"error": "That is a lot of files in a short time. Wait a few minutes and try again."}), 429)
        if request.content_length and request.content_length > citefile.MAX_BYTES + 200_000:
            return _no_store(jsonify({"error": "That file is too big. The limit is 12 MB."}), 413)
        kind, text, meta, name = "text", "", {}, ""
        up = request.files.get("file")
        if up and up.filename:
            data = up.stream.read(citefile.MAX_BYTES + 1)            # kept in memory only
            if len(data) > citefile.MAX_BYTES:
                return _no_store(jsonify({"error": "That file is too big. The limit is 12 MB."}), 413)
            try:
                kind, text, meta = citefile.read_any(data, up.filename or "")
            except ValueError as e:
                return _no_store(jsonify({"error": str(e)}), 400)
            except RuntimeError as e:
                return _no_store(jsonify({"error": str(e)}), 503)
            except Exception:
                traceback.print_exc()
                return _no_store(jsonify({"error": "That file could not be read. It may be damaged. Try saving it again as a PDF or DOCX."}), 400)
            finally:
                data = None
            name = up.filename[:120]
        else:
            text = (request.form.get("text") or (request.get_json(silent=True) or {}).get("text") or "")[:citefile.MAX_CHARS]
            if not text.strip():
                return _no_store(jsonify({"error": "Choose a file or paste some text first."}), 400)
        if not text.strip():
            return _no_store(jsonify({"error": "No text could be read from this file. It may be a scan (a picture of pages). Type the details in instead."}), 422)
        fields, how, notes, sources = citefile.analyse(text, meta, _get, name)
        if kind == "doc":
            notes.insert(0, "Old .doc files give less reliable details. A DOCX or PDF works better.")
        return _no_store(jsonify({"fields": fields, "found_by": how, "notes": notes, "kind": kind, "sources": sources}))
    except Exception:
        traceback.print_exc()
        return _no_store(jsonify({"error": "Reading the file failed. Please try again."}), 500)
