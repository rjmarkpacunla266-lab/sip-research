"""
╔══════════════════════════════════════════════════════════════════╗
║           Sturch — Flask Backend  app.py  v3.0               ║
╠══════════════════════════════════════════════════════════════════╣
║  Blueprint structure:                                            ║
║    core.py          — shared helpers, Supabase, API functions    ║
║    routes/auth.py   — signup, login, logout, password reset      ║
║    routes/search.py — search, load-more, paper, history          ║
║    routes/library.py— bookmarks, collections                     ║
║    routes/tools.py  — answer finder, future tools                ║
║    routes/pages.py  — home, landing, donate, health              ║
╚══════════════════════════════════════════════════════════════════╝
"""
import os
from flask import Flask, Response
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ─── Public address (used for canonical / share-preview URLs, robots.txt and sitemap.xml) ───
# When you get your own domain, set SITE_URL on Render (for example https://sturch.com).
# No template needs editing: every page reads {{ site_url }}.
SITE_URL = os.getenv("SITE_URL", "https://sturch.onrender.com").strip().rstrip("/")
PUBLIC_PAGES = ["/", "/guide", "/news", "/signup", "/donate"]        # pages that should appear in Google

@app.context_processor
def inject_site_url():
    return {"site_url": SITE_URL}

@app.route("/robots.txt")
def robots_txt():
    body = ("User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "Disallow: /logout\n\n"
            f"Sitemap: {SITE_URL}/sitemap.xml\n")
    return Response(body, mimetype="text/plain", headers={"Cache-Control": "public, max-age=3600"})

@app.route("/sitemap.xml")
def sitemap_xml():
    urls = "".join(f"  <url><loc>{SITE_URL}{p}</loc></url>\n" for p in PUBLIC_PAGES)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + '</urlset>\n')
    return Response(xml, mimetype="application/xml", headers={"Cache-Control": "public, max-age=3600"})

app.secret_key = os.getenv("FLASK_SECRET_KEY", "sturch-secret-key")
CORS(app)

# ─── Register Blueprints ─────────────────────────────────────────────
from routes.pages   import pages_bp
from routes.auth    import auth_bp
from routes.search  import search_bp
from routes.library import library_bp
from routes.tools   import tools_bp

app.register_blueprint(pages_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(search_bp)
app.register_blueprint(library_bp)
app.register_blueprint(tools_bp)

# ─── Run ─────────────────────────────────────────────────────────────
# ── Sturch error pages (404 / 500) ──
from flask import request as _rq, jsonify as _jf, render_template as _rt

@app.errorhandler(404)
def _sturch_404(e):
    if _rq.path.startswith("/api/"):
        return _jf({"error": "Not found"}), 404
    return _rt("404.html"), 404

@app.errorhandler(500)
def _sturch_500(e):
    if _rq.path.startswith("/api/"):
        return _jf({"error": "Something went wrong. Please try again."}), 500
    return _rt("500.html"), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
