"""routes/auth.py — Authentication: signup, login, logout, password reset"""
import string, secrets
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify
from core import (normalize_email, hash_password, check_password, get_client_ip,
                  is_ip_allowed_to_signup, record_ip_signup, get_user_by_email,
                  sb_post, sb_patch, sb_get, send_email, MAX_ACCOUNTS_PER_IP)

auth_bp = Blueprint("auth", __name__)

def generate_reset_code():
    return "".join(secrets.choice(string.digits) for _ in range(6))

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
            "search_count": 0, "is_paid": False, "paid_searches": 0,
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
        session["user_id"]    = user["id"]
        session["user_email"] = user["email"]
        return jsonify({"success": True, "redirect": "/"})
    return render_template("login.html")

@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot.html")
    data  = request.get_json() or request.form
    email = normalize_email(data.get("email") or "")
    if not email or "@" not in email:
        return jsonify({"error": "Enter a valid email address"}), 400
    user = get_user_by_email(email)
    if not user:
        return jsonify({"success": True, "message": "If that email exists, a code has been sent."})
    code = generate_reset_code()
    sb_post("password_resets", {"email": email, "code": code, "used": False,
                                "created_at": datetime.now().isoformat()})
    html = f"""<div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;background:#0b0d12;color:#d8e0f8;padding:32px;border-radius:12px">
<h2 style="font-family:Georgia,serif;color:#4f8de8;margin-bottom:8px">Sturch Password Reset</h2>
<p style="color:#8a94b8;margin-bottom:24px">Your one-time reset code:</p>
<div style="background:#181d2e;border:2px solid #4f8de8;border-radius:10px;padding:20px;text-align:center;margin-bottom:24px">
<span style="font-size:2.5rem;font-weight:700;letter-spacing:12px;color:#4f8de8;font-family:monospace">{code}</span>
</div>
<p style="color:#8a94b8;font-size:.85rem">This code expires in <strong>15 minutes</strong>. Do not share it with anyone.</p>
<p style="color:#8a94b8;font-size:.85rem">If you did not request this, ignore this email.</p>
<hr style="border:none;border-top:1px solid #1f2540;margin:20px 0"/>
<p style="color:#5a6385;font-size:.75rem">Sturch — Built for Filipino STE students 🇵🇭</p>
</div>"""
    sent = send_email(email, "Sturch — Your password reset code", html)
    if not sent:
        return jsonify({"error": "Could not send email. Try again later."}), 500
    return jsonify({"success": True, "message": "Reset code sent to your email."})

@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if request.method == "GET":
        return render_template("forgot.html")
    data     = request.get_json() or request.form
    email    = normalize_email(data.get("email") or "")
    code     = (data.get("code") or "").strip()
    password = (data.get("password") or "").strip()
    if not email or not code or not password:
        return jsonify({"error": "All fields are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    cutoff  = (datetime.now() - timedelta(minutes=15)).isoformat()
    results = sb_get("password_resets",
        f"email=eq.{email}&code=eq.{code}&used=eq.false&created_at=gte.{cutoff}&order=created_at.desc&limit=1")
    if not results:
        return jsonify({"error": "Invalid or expired code. Please request a new one."}), 400
    sb_patch("password_resets", f"id=eq.{results[0]['id']}", {"used": True})
    sb_patch("users", f"email=eq.{email}", {"password_hash": hash_password(password)})
    return jsonify({"success": True, "redirect": "/login"})
