"""routes/auth.py — Authentication with USERNAME + password (no email), recovery code, and guest accounts.

Design notes
  * users.email is still NOT NULL UNIQUE in the database, so new accounts get a harmless placeholder
    ("<username>@sturch.invalid"). Nothing is ever sent to it. Old accounts keep their real email and can still log in with it.
  * A guest is a real row in `users` (is_guest = true, random username, random password nobody knows), so search,
    history, bookmarks and collections work with no other code changes. The browser cookie is the only key to it.
    Guests are deleted after GUEST_DAYS days. A guest can turn the same row into a real account (history is kept).
  * Password reset = username + the recovery code shown once at signup.
"""
import re
import time
import secrets
import urllib.parse
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from core import (hash_password, check_password, get_client_ip, is_ip_allowed_to_signup, record_ip_signup,
                  get_user_by_email, sb_patch, sb_try, MAX_ACCOUNTS_PER_IP)

auth_bp = Blueprint("auth", __name__)

GUEST_DAYS   = 30
MIN_PASSWORD = 8
_USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
_RESERVED    = {"admin", "administrator", "root", "support", "sturch", "moderator", "mod", "staff", "system",
                "null", "undefined", "guest", "official", "help", "owner"}
_CODE_CHARS  = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I so a code is easy to copy by hand


def _q(v):
    return urllib.parse.quote(str(v), safe="")

def _data():
    return request.get_json(silent=True) or request.form or {}

def _username_error(u):
    if not _USERNAME_RE.match(u):
        return "Username must be 3–20 characters: letters, numbers and underscore only"
    if u in _RESERVED or u.startswith("guest_"):
        return "That username is not available"
    return None

def _get_by_username(u):
    ok, rows, _ = sb_try("GET", "users", f"username=eq.{_q(u)}&limit=1")
    return rows[0] if ok and rows else None

def _new_recovery_code():
    raw = "".join(secrets.choice(_CODE_CHARS) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))

def _norm_code(c):
    return re.sub(r"[^A-Z0-9]", "", (c or "").upper())

# ── gentle brute-force protection (per worker; good enough for a small site) ──
_FAILS = {}
def _blocked(key, limit=8, window=600):
    now = time.time()
    hits = [t for t in _FAILS.get(key, []) if now - t < window]
    _FAILS[key] = hits
    return len(hits) >= limit
def _fail(key):
    _FAILS.setdefault(key, []).append(time.time())
    if len(_FAILS) > 5000:                                  # keep memory small
        _FAILS.clear()

def _start_session(user, guest=False):
    session.clear()
    session["user_id"]    = user["id"]
    session["user_email"] = user.get("username") or user.get("email") or ""     # shown in the UI wherever the email used to be
    session["username"]   = user.get("username") or ""
    session["is_guest"]   = bool(guest)
    session.permanent     = bool(guest)                     # guests keep their cookie for GUEST_DAYS (Flask default is 31)


# ─── SIGNUP ──────────────────────────────────────────────────────────
@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session and not session.get("is_guest"):
        return redirect(url_for("pages.home"))
    if request.method == "POST":
        data     = _data()
        username = (data.get("username") or "").strip().lower()
        password = data.get("password") or ""
        err = _username_error(username)
        if err:
            return jsonify({"error": err}), 400
        if len(password) < MIN_PASSWORD:
            return jsonify({"error": f"Password must be at least {MIN_PASSWORD} characters"}), 400
        if password.lower() == username:
            return jsonify({"error": "Password must be different from your username"}), 400
        if _get_by_username(username):
            return jsonify({"error": "That username is already taken"}), 400

        code   = _new_recovery_code()
        fields = {"username": username, "email": f"{username}@sturch.invalid", "is_guest": False,
                  "password_hash": hash_password(password), "recovery_hash": hash_password(_norm_code(code))}

        if session.get("is_guest") and session.get("user_id"):
            # a guest becomes a real account: same row, so searches / bookmarks are kept
            ok, rows, e = sb_try("PATCH", "users", f"id=eq.{_q(session['user_id'])}&is_guest=eq.true", data=fields)
            if not ok or not rows:
                return jsonify({"error": "Could not create account. Try again."}), 500
            user = rows[0]
        else:
            ip = get_client_ip()
            if not is_ip_allowed_to_signup(ip):
                return jsonify({"error": f"Too many accounts from your device today. Max is {MAX_ACCOUNTS_PER_IP} per day."}), 429
            fields.update({"search_count": 0, "created_at": datetime.now().isoformat()})
            ok, rows, e = sb_try("POST", "users", data=fields)
            if not ok or not rows:
                return jsonify({"error": "Could not create account. Try again."}), 500
            record_ip_signup(ip)
            user = rows[0] if isinstance(rows, list) else rows
        _start_session(user)
        return jsonify({"success": True, "redirect": "/", "recovery_code": code})
    return render_template("signup.html")


# ─── LOGIN ───────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session and not session.get("is_guest"):
        return redirect(url_for("pages.home"))
    if request.method == "POST":
        data       = _data()
        identifier = (data.get("username") or data.get("email") or "").strip().lower()
        password   = (data.get("password") or "").strip()
        if not identifier or not password:
            return jsonify({"error": "Username and password required"}), 400
        key = f"{get_client_ip()}|{identifier}"
        if _blocked(key):
            return jsonify({"error": "Too many wrong attempts. Wait 10 minutes and try again."}), 429
        # usernames for new accounts, real emails for accounts made before this change
        user = get_user_by_email(identifier) if "@" in identifier else _get_by_username(identifier)
        if not user or user.get("is_guest") or not check_password(password, user["password_hash"]):
            _fail(key)
            return jsonify({"error": "Invalid username or password"}), 401
        if not user["password_hash"].startswith("scrypt:"):
            sb_patch("users", f"id=eq.{user['id']}", {"password_hash": hash_password(password)})
        sb_patch("users", f"id=eq.{user['id']}", {"last_login": datetime.now().isoformat()})
        _start_session(user)
        return jsonify({"success": True, "redirect": "/"})
    return render_template("login.html")


# ─── GUEST ───────────────────────────────────────────────────────────
def _cleanup_old_guests():
    """Delete guests older than GUEST_DAYS together with their saved data (runs now and then, never blocks sign-in)."""
    try:
        cutoff = (datetime.now() - timedelta(days=GUEST_DAYS)).isoformat()
        ok, rows, _ = sb_try("GET", "users", f"is_guest=eq.true&created_at=lt.{_q(cutoff)}&select=id&limit=20")
        for r in (rows if ok and rows else []):
            uid = _q(r["id"])
            for table in ("collection_papers", "collections", "bookmarks"):
                sb_try("DELETE", table, f"user_id=eq.{uid}")
            sb_try("DELETE", "users", f"id=eq.{uid}")           # search_logs / payments cascade
    except Exception:
        pass

@auth_bp.route("/guest", methods=["POST"])
def guest():
    if "user_id" in session:
        return jsonify({"success": True, "redirect": "/"})
    ip = get_client_ip()
    if not is_ip_allowed_to_signup(ip):
        return jsonify({"error": "Too many accounts from your device today. Please try again tomorrow."}), 429
    name = "guest_" + "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8))
    ok, rows, _err = sb_try("POST", "users", data={
        "username": name, "email": f"{name}@sturch.invalid", "is_guest": True, "search_count": 0,
        "password_hash": hash_password(secrets.token_urlsafe(24)),           # nobody knows it: the cookie is the only key
        "created_at": datetime.now().isoformat()})
    if not ok or not rows:
        return jsonify({"error": "Could not start a guest session. Try again. [" + str(_err)[:200] + "]"}), 500
    record_ip_signup(ip)
    _start_session(rows[0] if isinstance(rows, list) else rows, guest=True)
    if secrets.randbelow(20) == 0:
        _cleanup_old_guests()
    return jsonify({"success": True, "redirect": "/"})

@auth_bp.route("/api/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "username": session.get("username", ""),
                    "is_guest": bool(session.get("is_guest")), "guest_days": GUEST_DAYS})


# ─── LOGOUT / RESET ──────────────────────────────────────────────────
@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        data     = _data()
        username = (data.get("username") or "").strip().lower()
        code     = _norm_code(data.get("recovery_code"))
        new_pw   = data.get("new_password") or ""
        if not username or not code or not new_pw:
            return jsonify({"error": "Fill in all fields"}), 400
        if len(new_pw) < MIN_PASSWORD:
            return jsonify({"error": f"Password must be at least {MIN_PASSWORD} characters"}), 400
        key = f"reset|{get_client_ip()}|{username}"
        if _blocked(key, limit=5):
            return jsonify({"error": "Too many wrong attempts. Wait 10 minutes and try again."}), 429
        user = _get_by_username(username)
        if not user or user.get("is_guest") or not user.get("recovery_hash") or not check_password(code, user["recovery_hash"]):
            _fail(key)
            return jsonify({"error": "Username or recovery code is wrong"}), 401
        new_code = _new_recovery_code()                          # codes are single-use: a fresh one replaces the old one
        sb_patch("users", f"id=eq.{user['id']}", {"password_hash": hash_password(new_pw),
                                                  "recovery_hash": hash_password(_norm_code(new_code))})
        return jsonify({"success": True, "recovery_code": new_code})
    return render_template("forgot.html")
