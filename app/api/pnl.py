"""Daily P&L endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import query_all, query_one

bp = Blueprint("pnl", __name__, url_prefix="/api/pnl")


@bp.get("/today")
def today_pnl():
    """Most recent P&L row (used by the dashboard summary card)."""
    row = query_one(
        """SELECT pnl_date,
                  market_value_cny, market_value_aud, market_value_usd,
                  prev_market_value_cny, prev_market_value_aud, prev_market_value_usd,
                  net_invested_cny, net_invested_aud, net_invested_usd,
                  pnl_cny, pnl_aud, pnl_usd,
                  pnl_pct_cny, pnl_pct_aud, pnl_pct_usd,
                  created_at
           FROM daily_pnl
           ORDER BY pnl_date DESC LIMIT 1"""
    )
    return jsonify(row or {})


@bp.get("/history")
def history():
    limit = int(request.args.get("limit", 90))
    rows = query_all(
        """SELECT pnl_date,
                  market_value_cny, market_value_aud, market_value_usd,
                  net_invested_cny, net_invested_aud, net_invested_usd,
                  pnl_cny, pnl_aud, pnl_usd,
                  pnl_pct_cny, pnl_pct_aud, pnl_pct_usd
           FROM daily_pnl
           ORDER BY pnl_date DESC LIMIT ?""",
        (limit,),
    )
    return jsonify(rows)


@bp.post("/compute")
def compute_now():
    """Manually trigger a P&L compute (in addition to the scheduled job)."""
    from scripts.compute_daily_pnl import main
    code = main()
    if code != 0:
        return jsonify(error="compute failed; check logs"), 500
    return jsonify(ok=True), 201
