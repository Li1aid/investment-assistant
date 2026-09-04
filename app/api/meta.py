"""Meta endpoints: health, summary, pool."""
from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify

from ..db import query_all, query_one
from ..services.fx import convert, load_fx

bp = Blueprint("meta", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    return jsonify(status="ok")


@bp.get("/summary")
def summary():
    """Roll-up across all holdings.

    Returns per-currency buckets plus combined totals converted into
    every supported display currency (CNY, AUD, ...). Holdings without
    an available fx rate are skipped from the combined total for that
    target currency.
    """
    holdings = query_all(
        "SELECT symbol, name, market, currency, quantity, avg_cost, "
        "last_price, unit, realized_pnl FROM holdings"
    )
    fx = load_fx()

    display_ccys = ("CNY", "AUD", "USD", "HKD")

    by_ccy: dict[str, dict] = {}
    totals: dict[str, dict] = {c: {"market_value": 0.0, "cost": 0.0, "pnl": 0.0} for c in display_ccys}

    for h in holdings:
        ccy = h["currency"]
        mv = (h["quantity"] or 0) * (h["last_price"] or 0)
        cost = (h["quantity"] or 0) * (h["avg_cost"] or 0)
        realized = h["realized_pnl"] or 0
        # Total P&L per symbol = floating (mv − cost) on what's still
        # held + realized banked on prior sells.
        pnl = (mv - cost) + realized
        bucket = by_ccy.setdefault(ccy, {"market_value": 0.0, "cost": 0.0, "pnl": 0.0})
        bucket["market_value"] += mv
        bucket["cost"] += cost
        bucket["pnl"] += pnl
        for dst in display_ccys:
            mv_c = convert(mv, ccy, dst, fx)
            cost_c = convert(cost, ccy, dst, fx)
            realized_c = convert(realized, ccy, dst, fx)
            if mv_c is None or cost_c is None or realized_c is None:
                continue
            totals[dst]["market_value"] += mv_c
            totals[dst]["cost"] += cost_c
            totals[dst]["pnl"] += (mv_c - cost_c) + realized_c

    return jsonify(
        by_currency=by_ccy,
        totals=totals,
        # legacy: keep total_cny for backwards compat
        total_cny=totals["CNY"]["market_value"],
        fx=fx,
    )


@bp.get("/pool")
def pool():
    """资金池快照 — mirrors the frontend's poolTotal() so AI assistants
    (or any external tool) get a single canonical 总仓 figure instead of
    having to merge /api/buckets + /api/summary on their side.

    Returned amounts are all in CNY:
      principal_cny       本金 — what Aiden has deposited (from buckets)
      pnl_cny             total P&L = floating + realized, fx'd to CNY
      total_cny           本金 + 盈利 — this is the 总仓 number
      market_value_cny    current market value of all positions, in CNY
      cost_basis_cny      sum of (qty × avg_cost), in CNY
      cash_remaining_cny  本金 − cost_basis, floored at 0 (dry powder)
    """
    holdings = query_all(
        "SELECT currency, quantity, avg_cost, last_price, realized_pnl "
        "FROM holdings"
    )
    fx = load_fx()

    # Principal is the SUM of pool_transactions, same source the /api/buckets
    # endpoint uses — avoids any drift against the legacy currency_buckets
    # cache.
    cny_bucket = query_one(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM pool_transactions "
        "WHERE currency = 'CNY'"
    )
    principal = (cny_bucket["total"] or 0) if cny_bucket else 0

    pnl_cny = 0.0
    cost_cny = 0.0
    mv_cny = 0.0
    skipped: list[str] = []
    for h in holdings:
        qty = h["quantity"] or 0
        avg = h["avg_cost"] or 0
        last = h["last_price"] or 0
        realized = h["realized_pnl"] or 0
        cost = qty * avg
        mv = qty * last
        pnl = (mv - cost) + realized
        ccy = h["currency"]
        pnl_c = convert(pnl, ccy, "CNY", fx)
        cost_c = convert(cost, ccy, "CNY", fx)
        mv_c = convert(mv, ccy, "CNY", fx)
        if pnl_c is None or cost_c is None or mv_c is None:
            if ccy not in skipped:
                skipped.append(ccy)
            continue
        pnl_cny += pnl_c
        cost_cny += cost_c
        mv_cny += mv_c

    total = principal + pnl_cny
    cash_remaining = max(0.0, principal - cost_cny)

    return jsonify(
        principal_cny=round(principal, 2),
        pnl_cny=round(pnl_cny, 2),
        total_cny=round(total, 2),
        market_value_cny=round(mv_cny, 2),
        cost_basis_cny=round(cost_cny, 2),
        cash_remaining_cny=round(cash_remaining, 2),
        skipped_currencies=skipped,
        as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
