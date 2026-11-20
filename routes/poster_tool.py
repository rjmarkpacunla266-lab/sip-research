"""routes/poster_tool.py: Poster & Slide Maker page. The editor runs fully in the browser; nothing is stored on the server."""
from flask import render_template

from core import login_required
from routes.tools_common import tools_bp


@tools_bp.route("/poster-maker")
@login_required
def poster_maker_page():
    resp = render_template("poster_maker.html")
    return resp
