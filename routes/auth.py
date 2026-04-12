"""routes/auth.py — Authentication: signup, login, logout"""
from datetime import datetime
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from core import (normalize_email, hash_password, check_password, get_client_ip,
                  is_ip_allowed_to_signup, record_ip_signup, get_user_by_email,
                  sb_post, sb_patch, sb_get, MAX_ACCOUNTS_PER_IP)

auth_bp = Blueprint("auth", __name__)

@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session:
        return redirect(url_for("pages.home"))
    if request.method == "POST":
        data     = request.get_json() or request.form
        email    = normalize_email(data.get("email") or "")
        password = (data.get("password") or "").strip()
        if not email or not password:
            return jsonify({"error": "Email and password required"}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        if "@" not in email:
            return jsonify({"error": "Enter a valid email address"}), 400
        ip = get_client_ip()
        if not is_ip_allowed_to_signup(ip):
            return jsonify({"error": f"Too many accounts from your device today. Max is {MAX_ACCOUNTS_PER_IP} per day."}), 429
        if get_user_by_email(email):
            return jsonify({"error": "Email already registered. Please login."}), 400
        result = sb_post("users", {
            "email": email, "password_hash": hash_password(password),
            "search_count": 0,
            "created_at": datetime.now().isoformat(),
        })
        if not result:
            return jsonify({"error": "Could not create account. Try again."}), 500
        record_ip_signup(ip)
        new_user = result[0] if isinstance(result, list) else result
        session["user_id"]    = new_user["id"]
        session["user_email"] = new_user["email"]
        return jsonify({"success": True, "redirect": "/"})
    return render_template("signup.html")

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("pages.home"))
    if request.method == "POST":
        data     = request.get_json() or request.form
        email    = normalize_email(data.get("email") or "")
        password = (data.get("password") or "").strip()
        if not email or not password:
            return jsonify({"error": "Email and password required"}), 400
        user = get_user_by_email(email)
        if not user or not check_password(password, user["password_hash"]):
            return jsonify({"error": "Invalid email or password"}), 401
        if not user["password_hash"].startswith("scrypt:"):
            sb_patch("users", f"id=eq.{user['id']}",
                     {"password_hash": hash_password(password)})
        sb_patch("users", f"id=eq.{user['id']}",
                 {"last_login": datetime.now().isoformat()})
        session["user_id"]    = user["id"]
        session["user_email"] = user["email"]
        return jsonify({"success": True, "redirect": "/"})
    return render_template("login.html")

@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


