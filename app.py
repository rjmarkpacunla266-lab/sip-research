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
from flask import Flask
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
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
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
