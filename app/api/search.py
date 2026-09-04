"""GET /api/search?q=... — multi-market ticker search."""
from flask import Blueprint, jsonify, request

from ..services.search import search as do_search

bp = Blueprint("search", __name__, url_prefix="/api/search")


@bp.get("")
def search_endpoint():
    q = request.args.get("q", "").strip()
    return jsonify(do_search(q))
