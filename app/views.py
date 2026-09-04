"""HTML views — just serves the single-page shell."""
from flask import Blueprint, make_response, render_template

bp = Blueprint("views", __name__)


@bp.route("/")
def index():
    resp = make_response(render_template("index.html"))
    # The HTML shell must never be cached — JS/CSS already carry a
    # mtime-stamped ?v= query so they cache safely.
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp
