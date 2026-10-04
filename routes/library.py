"""routes/library.py — Bookmarks and Collections
Every database call reports real failures (no more fake 'saved' when the insert failed)."""
import urllib.parse
from datetime import datetime
from flask import Blueprint, request, session, jsonify
from core import login_required, sb_try

library_bp = Blueprint("library", __name__)

def _q(value):
    """URL-encode a value for a PostgREST filter (titles may contain & # , spaces)."""
    return urllib.parse.quote(str(value), safe="")

def _fail(message, detail):
    return jsonify({"error": message, "detail": detail}), 500

# Only these fields are stored with a saved paper (keeps rows small and safe)
_PAPER_KEYS = ("title", "authors", "year", "date", "journal", "abstract", "citations", "is_oa",
               "oa_url", "doi", "concepts", "openalex_id", "volume", "issue", "pages",
               "apa_reference", "data_source")

def _clean_paper(data):
    paper = {k: data.get(k) for k in _PAPER_KEYS if k in data}
    if isinstance(paper.get("authors"), list):  paper["authors"]  = [str(a)[:120] for a in paper["authors"][:30]]
    if isinstance(paper.get("concepts"), list): paper["concepts"] = [str(c)[:80] for c in paper["concepts"][:12]]
    for k in ("title", "abstract", "apa_reference"):
        if isinstance(paper.get(k), str):
            paper[k] = paper[k][:3000 if k == "abstract" else 1000]
    return paper

def _paper_id(data):
    """Must match getPaperId() in index.html / paper.html."""
    return str(data.get("openalex_id") or data.get("doi") or (data.get("title") or "")[:60]).strip()

# ─── BOOKMARKS ───────────────────────────────────────────────────────
@library_bp.route("/api/bookmarks", methods=["GET"])
@login_required
def get_bookmarks():
    ok, rows, err = sb_try("GET", "bookmarks", f"user_id=eq.{_q(session['user_id'])}&order=created_at.desc")
    if not ok:
        return _fail("Could not load saved papers", err)
    return jsonify(rows)

@library_bp.route("/api/bookmarks", methods=["POST"])
@login_required
def add_bookmark():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data.get("title"):
        return jsonify({"error": "No paper data"}), 400
    user_id  = session["user_id"]
    paper_id = _paper_id(data)
    if not paper_id:
        return jsonify({"error": "Paper has no usable id"}), 400
    ok, existing, err = sb_try("GET", "bookmarks", f"user_id=eq.{_q(user_id)}&paper_id=eq.{_q(paper_id)}&select=id")
    if not ok:
        return _fail("Could not save paper", err)
    if existing:
        return jsonify({"error": "already_bookmarked", "id": existing[0].get("id", "")}), 409
    ok, rows, err = sb_try("POST", "bookmarks", data={
        "user_id": user_id, "paper_id": paper_id,
        "paper_data": _clean_paper(data), "created_at": datetime.now().isoformat()})
    if not ok or not rows:
        return _fail("Could not save paper", err or "Database returned no row")
    row = rows[0] if isinstance(rows, list) else rows
    return jsonify({"success": True, "id": row.get("id", "")})

@library_bp.route("/api/bookmarks/<bookmark_id>", methods=["DELETE"])
@login_required
def delete_bookmark(bookmark_id):
    ok, _, err = sb_try("DELETE", "bookmarks", f"id=eq.{_q(bookmark_id)}&user_id=eq.{_q(session['user_id'])}")
    if not ok:
        return _fail("Could not remove bookmark", err)
    return jsonify({"success": True})

# ─── COLLECTIONS ─────────────────────────────────────────────────────
@library_bp.route("/api/collections", methods=["GET"])
@login_required
def get_collections():
    ok, rows, err = sb_try("GET", "collections", f"user_id=eq.{_q(session['user_id'])}&order=created_at.desc")
    if not ok:
        return _fail("Could not load collections", err)
    return jsonify(rows)

@library_bp.route("/api/collections", methods=["POST"])
@login_required
def create_collection():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:100]
    if not name:
        return jsonify({"error": "Collection name required"}), 400
    ok, rows, err = sb_try("POST", "collections", data={
        "user_id": session["user_id"], "name": name, "created_at": datetime.now().isoformat()})
    if not ok or not rows:
        return _fail("Could not create collection", err or "Database returned no row")
    return jsonify(rows[0] if isinstance(rows, list) else rows)

@library_bp.route("/api/collections/summary", methods=["GET"])
@login_required
def collections_summary():
    """{collection_id: number_of_papers} for the Collections / Citations pages."""
    ok, rows, err = sb_try("GET", "collection_papers", f"user_id=eq.{_q(session['user_id'])}&select=collection_id")
    if not ok:
        return _fail("Could not load collection counts", err)
    counts = {}
    for r in rows:
        k = str(r.get("collection_id"))
        counts[k] = counts.get(k, 0) + 1
    return jsonify(counts)

@library_bp.route("/api/collections/<col_id>", methods=["PATCH"])
@login_required
def rename_collection(col_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:100]
    if not name:
        return jsonify({"error": "Collection name required"}), 400
    ok, rows, err = sb_try("PATCH", "collections",
                           f"id=eq.{_q(col_id)}&user_id=eq.{_q(session['user_id'])}", data={"name": name})
    if not ok:
        return _fail("Could not rename collection", err)
    if not rows:
        return jsonify({"error": "Collection not found"}), 404
    return jsonify({"success": True, "name": name})

@library_bp.route("/api/collections/<col_id>", methods=["DELETE"])
@login_required
def delete_collection(col_id):
    uid = _q(session["user_id"])
    ok, _, err = sb_try("DELETE", "collection_papers", f"collection_id=eq.{_q(col_id)}&user_id=eq.{uid}")
    if not ok:
        return _fail("Could not delete collection", err)
    ok, _, err = sb_try("DELETE", "collections", f"id=eq.{_q(col_id)}&user_id=eq.{uid}")
    if not ok:
        return _fail("Could not delete collection", err)
    return jsonify({"success": True})

@library_bp.route("/api/collections/<col_id>/papers", methods=["GET"])
@login_required
def get_collection_papers(col_id):
    ok, rows, err = sb_try("GET", "collection_papers",
                           f"collection_id=eq.{_q(col_id)}&user_id=eq.{_q(session['user_id'])}&order=created_at.desc")
    if not ok:
        return _fail("Could not load collection papers", err)
    return jsonify(rows)

@library_bp.route("/api/collections/<col_id>/papers", methods=["POST"])
@login_required
def add_to_collection(col_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data.get("title"):
        return jsonify({"error": "No paper data"}), 400
    user_id  = session["user_id"]
    paper_id = _paper_id(data)
    ok, existing, err = sb_try("GET", "collection_papers",
                               f"collection_id=eq.{_q(col_id)}&user_id=eq.{_q(user_id)}&paper_id=eq.{_q(paper_id)}&select=id")
    if not ok:
        return _fail("Could not add to collection", err)
    if existing:
        return jsonify({"success": True, "already": True, "id": existing[0].get("id", "")})
    ok, rows, err = sb_try("POST", "collection_papers", data={
        "collection_id": col_id, "user_id": user_id, "paper_id": paper_id,
        "paper_data": _clean_paper(data), "created_at": datetime.now().isoformat()})
    if not ok or not rows:
        return _fail("Could not add to collection", err or "Database returned no row")
    row = rows[0] if isinstance(rows, list) else rows
    return jsonify({"success": True, "id": row.get("id", "")})

@library_bp.route("/api/collections/<col_id>/papers/<entry_id>", methods=["DELETE"])
@login_required
def remove_from_collection(col_id, entry_id):
    ok, _, err = sb_try("DELETE", "collection_papers",
                        f"id=eq.{_q(entry_id)}&collection_id=eq.{_q(col_id)}&user_id=eq.{_q(session['user_id'])}")
    if not ok:
        return _fail("Could not remove paper", err)
    return jsonify({"success": True})
