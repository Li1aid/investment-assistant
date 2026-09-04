"""Holdings CRUD endpoints."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import get_conn, query_all
from ..timeutil import et_today_iso

bp = Blueprint("holdings", __name__, url_prefix="/api/holdings")


def _today_trade_state() -> dict[str, dict]:
    """For each symbol traded today, return {qty_change, cash_invested}.
    Used by _row_to_dict to compute a *correct* day_pnl that accounts
    for positions opened or topped up today (don't pretend we held the
    new shares all day at yesterday's close).

    Reads the transactions ledger — the single source of trade entry
    (both the transactions tab and the holdings form write a row there)."""
    today = et_today_iso()
    out: dict[str, dict] = {}
    for r in query_all(
        """SELECT symbol,
                  SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END)
                       AS net_qty,
                  SUM(CASE WHEN side='buy' THEN quantity*price
                           ELSE -quantity*price END)
                       AS net_invested
           FROM transactions
           WHERE trade_date = ?
           GROUP BY symbol""",
        (today,),
    ):
        out[r["symbol"]] = {
            "qty_change": r["net_qty"] or 0,
            "net_invested": r["net_invested"] or 0,
        }
    return out


def _row_to_dict(r: dict, today_trades: dict | None = None) -> dict:
    today_trades = today_trades or {}
    qty = r.get("quantity") or 0
    avg = r.get("avg_cost") or 0
    last = r.get("last_price") or 0
    prev = r.get("prev_close") or 0
    realized = r.get("realized_pnl") or 0
    cost = qty * avg
    mv = qty * last
    floating_pnl = mv - cost
    # Total P&L for the symbol = unrealized on the still-held shares
    # plus everything we've already booked on prior sells.
    pnl = floating_pnl + realized
    # pnl_pct matches what brokerage apps display: total P&L (floating
    # + realized) divided by net invested. "Net invested" backs out
    # the cash Aiden has already pulled out via sells:
    #   net_invested = current_cost − realized_pnl
    # which equals (original_qty × avg_cost) − sell_proceeds because
    # sells don't move avg_cost. When nothing has been sold, realized
    # is 0 and the formula degrades to floating / cost — same number as
    # before. The difference only shows up for symbols you've trimmed.
    net_invested = cost - realized
    pct = (pnl / net_invested * 100.0) if net_invested > 0 else 0.0
    # day_change / day_change_pct describe the MARKET move per share —
    # keep as (last - prev_close). They're informative even for
    # positions opened today (tells you the ticker's day).
    day_change = (last - prev) if (last and prev) else 0.0
    day_pct = (day_change / prev * 100.0) if prev else 0.0
    # day_pnl is YOUR money's day move. Position opened today must use
    # your buy price, not yesterday's close.
    t = today_trades.get(r["symbol"], {})
    today_qty_delta = t.get("qty_change", 0)
    today_net_invested = t.get("net_invested", 0)
    yesterday_qty = qty - today_qty_delta
    prev_mv = yesterday_qty * prev
    day_pnl = mv - prev_mv - today_net_invested if prev else 0.0
    return {**r,
            "cost_basis": cost, "market_value": mv,
            "floating_pnl": floating_pnl, "realized_pnl": realized,
            "pnl": pnl, "pnl_pct": pct,
            "day_change": day_change, "day_change_pct": day_pct,
            "day_pnl": day_pnl}


@bp.get("")
def list_holdings():
    rows = query_all(
        """SELECT id, symbol, name, market, currency, quantity, avg_cost,
                  last_price, last_price_at, prev_close, unit, notes,
                  realized_pnl, region, updated_at
           FROM holdings
           ORDER BY market, symbol"""
    )
    today_trades = _today_trade_state()
    return jsonify([_row_to_dict(r, today_trades) for r in rows])


@bp.post("")
def add_holding():
    """Create or top-up a holding AND emit a buy-trade record.

    Semantics:
      - New (symbol, market): plain INSERT into holdings.
      - Existing (symbol, market): UPDATE with weighted-avg cost (补仓).
      - Always emits one `transactions` row for the delta_qty /
        delta_price the user just submitted, so day-P&L math (which
        reads the transactions ledger) sees the trade.

    Failures in the trade-record block do NOT roll back the holdings
    upsert — they surface via `warnings` in the response.
    """
    import sys

    d = request.get_json(force=True)
    required = ["symbol", "name", "market", "currency", "quantity", "avg_cost"]
    missing = [k for k in required if d.get(k) in (None, "")]
    if missing:
        return jsonify(error=f"missing fields: {missing}"), 400

    try:
        delta_qty = float(d["quantity"])
        delta_price = float(d["avg_cost"])
    except (TypeError, ValueError):
        return jsonify(error="quantity / avg_cost must be numeric"), 400
    if delta_qty <= 0 or delta_price <= 0:
        return jsonify(error="quantity and avg_cost must be positive"), 400

    trade_date = (d.get("trade_date") or et_today_iso()).strip()
    symbol = d["symbol"]
    market = d["market"]
    name = d["name"]
    currency = d["currency"]
    notes = d.get("notes")

    warnings: list[str] = []

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, quantity, avg_cost FROM holdings WHERE symbol=? AND market=?",
            (symbol, market),
        ).fetchone()
        if existing:
            old_qty = existing["quantity"] or 0
            old_avg = existing["avg_cost"] or 0
            new_qty = old_qty + delta_qty
            new_avg = (
                ((old_qty * old_avg) + (delta_qty * delta_price)) / new_qty
                if new_qty else delta_price
            )
            conn.execute(
                """UPDATE holdings
                   SET quantity = ?, avg_cost = ?, name = ?, currency = ?,
                       notes = COALESCE(?, notes),
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (new_qty, new_avg, name, currency, notes, existing["id"]),
            )
            holding_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO holdings
                   (symbol, name, market, currency, quantity, avg_cost,
                    unit, notes)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (symbol, name, market, currency, delta_qty, delta_price,
                 d.get("unit", "share"), notes),
            )
            holding_id = cur.lastrowid
        conn.commit()

    # Best-effort live price fetch.
    try:
        from ..services.prices import fetch_one as _fetch_price_one
        p = _fetch_price_one(market, symbol)
        if p is not None:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE holdings SET last_price = ?, "
                    "last_price_at = datetime('now'), prev_close = ? "
                    "WHERE id = ?",
                    (p["last"], p.get("prev_close"), holding_id),
                )
                conn.commit()
    except Exception as e:
        print(f"[holdings] post-insert price fetch failed: {e}", file=sys.stderr)

    # Emit transaction record (cash-flow ledger).
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO transactions
                   (trade_date, symbol, name, market, side,
                    quantity, price, fee, currency, notes)
                   VALUES (?, ?, ?, ?, 'buy', ?, ?, 0, ?, ?)""",
                (trade_date, symbol, name, market, delta_qty, delta_price,
                 currency, "auto: 加持仓"),
            )
            conn.commit()
    except Exception as e:
        warnings.append(f"transactions: {e}")
        print(f"[holdings] transactions insert failed: {e}", file=sys.stderr)

    return jsonify(id=holding_id, warnings=warnings), 201


@bp.put("/<int:hid>")
def update_holding(hid: int):
    d = request.get_json(force=True)
    fields = ["symbol", "name", "market", "currency", "quantity", "avg_cost",
              "last_price", "unit", "notes", "region"]
    sets, vals = [], []
    for f in fields:
        if f in d:
            sets.append(f"{f} = ?")
            vals.append(d[f])
    if not sets:
        return jsonify(error="nothing to update"), 400
    sets.append("updated_at = datetime('now')")
    vals.append(hid)
    with get_conn() as conn:
        conn.execute(f"UPDATE holdings SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    return jsonify(ok=True)


@bp.delete("/<int:hid>")
def delete_holding(hid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM holdings WHERE id = ?", (hid,))
        conn.commit()
    return jsonify(ok=True)
