"""routes/pages.py — Page routes: home, landing, donate, health"""
from flask import Blueprint, render_template, session, redirect, url_for, jsonify
from core import login_required

pages_bp = Blueprint("pages", __name__)

@pages_bp.route("/")
def home():
    if "user_id" in session:
        return render_template("home.html")
    return render_template("landing.html")

@pages_bp.route("/search")
@login_required
def search_page():
    return render_template("index.html")

@pages_bp.route("/donate")
def donate():
    return render_template("donate.html")


@pages_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Sturch", "version": "3.0-blueprints"})

@pages_bp.route("/payment/success")
def payment_success():
    return render_template("payment_result.html", success=True, points_added=200)

@pages_bp.route("/payment/failed")
def payment_failed():
    return render_template("payment_result.html", success=False)
