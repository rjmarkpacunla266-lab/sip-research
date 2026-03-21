"""routes/library.py — Bookmarks and Collections"""
from datetime import datetime
from flask import Blueprint, request, session, jsonify
from core import login_required, sb_get, sb_post, sb_delete

library_bp = Blueprint("library", __name__)

# ─── BOOKMARKS ───────────────────────────────────────────────────────
@library_bp.route("/api/bookmarks", methods=["GET"])
@login_required
def get_bookmarks():
    result = sb_get("bookmarks", f"user_id=eq.{session['user_id']}&order=created_at.desc")
    return jsonify(result or [])

@library_bp.route("/api/bookmarks", methods=["POST"])
@login_required
def add_bookmark():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No paper data"}), 400
    user_id  = session["user_id"]
    paper_id = (data.get("openalex_id") or data.get("doi") or (data.get("title", "")[:60])).strip()
    if sb_get("bookmarks", f"user_id=eq.{user_id}&paper_id=eq.{paper_id}"):
        return jsonify({"error": "already_bookmarked"}), 409
    result = sb_post("bookmarks", {"user_id": user_id, "paper_id": paper_id,
                                   "paper_data": data, "created_at": datetime.now().isoformat()})
    item = (result[0] if isinstance(result, list) else result) or {}
    return jsonify({"success": True, "id": item.get("id", "")})

@library_bp.route("/api/bookmarks/<bookmark_id>", methods=["DELETE"])
@login_required
def delete_bookmark(bookmark_id):
    ok = sb_delete("bookmarks", f"id=eq.{bookmark_id}&user_id=eq.{session['user_id']}")
    return jsonify({"success": ok})

# ─── COLLECTIONS ─────────────────────────────────────────────────────
@library_bp.route("/api/collections", methods=["GET"])
@login_required
def get_collections():
    result = sb_get("collections", f"user_id=eq.{session['user_id']}&order=created_at.desc")
    return jsonify(result or [])

@library_bp.route("/api/collections", methods=["POST"])
@login_required
def create_collection():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Collection name required"}), 400
    result = sb_post("collections", {"user_id": session["user_id"], "name": name,
                                     "created_at": datetime.now().isoformat()})
    return jsonify((result[0] if isinstance(result, list) else result) or {})

@library_bp.route("/api/collections/<col_id>", methods=["DELETE"])
@login_required
def delete_collection(col_id):
    ok = sb_delete("collections", f"id=eq.{col_id}&user_id=eq.{session['user_id']}")
    return jsonify({"success": ok})

@library_bp.route("/api/collections/<col_id>/papers", methods=["GET"])
@login_required
def get_collection_papers(col_id):
    result = sb_get("collection_papers",
                    f"collection_id=eq.{col_id}&user_id=eq.{session['user_id']}&order=created_at.desc")
    return jsonify(result or [])

@library_bp.route("/api/collections/<col_id>/papers", methods=["POST"])
@login_required
def add_to_collection(col_id):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No paper data"}), 400
    user_id  = session["user_id"]
    paper_id = (data.get("openalex_id") or data.get("doi") or (data.get("title", "")[:60])).strip()
    result   = sb_post("collection_papers", {"collection_id": col_id, "user_id": user_id,
                                             "paper_id": paper_id, "paper_data": data,
                                             "created_at": datetime.now().isoformat()})
    item = (result[0] if isinstance(result, list) else result) or {}
    return jsonify({"success": True, "id": item.get("id", "")})

@library_bp.route("/api/collections/<col_id>/papers/<entry_id>", methods=["DELETE"])
@login_required
def remove_from_collection(col_id, entry_id):
    ok = sb_delete("collection_papers",
                   f"id=eq.{entry_id}&collection_id=eq.{col_id}&user_id=eq.{session['user_id']}")
    return jsonify({"success": ok})
