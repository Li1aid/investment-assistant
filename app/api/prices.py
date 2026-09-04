"""Price refresh endpoint — delegates to services.prices."""
from __future__ import annotations

from flask import Blueprint, jsonify

from ..db import query_all

bp = Blueprint("prices", __name__, url_prefix="/api/prices")


@bp.post("/refresh")
def refresh_prices():
    """Refresh last_price for every holding and watchlist row."""
    try:
        from ..services.prices import refresh_all
    except ImportError as e:
        return jsonify(error=f"prices service unavailable: {e}"), 500
    result = refresh_all()
    return jsonify(result)


@bp.get("/fx")
def list_fx():
    return jsonify(query_all("SELECT pair, rate, fetched_at FROM fx_rates"))
