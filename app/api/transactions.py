"""Transactions CRUD + holdings sync.

POST inserts a transaction AND applies it to the matching holdings row
(weighted-avg add for buy, qty deduct for sell). This is the single
source of trade entry now — the daily-action UI was retired, so the
transactions tab is the only place trades flow in from the user.

DELETE removes the transactions row but does NOT roll back holdings,
on purpose: reverse-applying a weighted-avg add is floating-point lossy
and the baseline may have drifted from other paths. A warnings string
in the response asks the user to reconcile via the holdings tab.
"""
from __future__ import annotations

import sys

from flask import Blueprint, jsonify, request

from ..db import get_conn, query_all

bp = Blueprint("transactions", __name__, url_prefix="/api/transactions")


@bp.get("")
def list_transactions():
    rows = query_all(
        """SELECT id, trade_date, symbol, name, market, side,
                  quantity, price, fee, currency, notes
           FROM transactions
           ORDER BY trade_date DESC, id DESC"""
    )
    return jsonify(rows)


@bp.post("")
def add_transaction():
    d = request.get_json(force=True)
    required = ["trade_date", "symbol", "side", "quantity", "price", "currency"]
    missing = [k for k in required if d.get(k) in (None, "")]
    if missing:
        return jsonify(error=f"missing fields: {missing}"), 400
    side = d["side"]
    if side not in ("buy", "sell"):
        return jsonify(error="side must be 'buy' or 'sell'"), 400
    try:
        qty = float(d["quantity"])
        price = float(d["price"])
    except (TypeError, ValueError):
        return jsonify(error="quantity / price must be numeric"), 400
    if qty <= 0 or price <= 0:
        return jsonify(error="quantity and price must be positive"), 400

    warnings: list[str] = []
    symbol = d["symbol"]
    market = d.get("market")
    name = d.get("name")
    currency = d["currency"]

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO transactions
               (trade_date, symbol, name, market, side, quantity, price, fee, currency, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                d["trade_date"], symbol, name, market,
                side, qty, price, d.get("fee", 0),
                currency, d.get("notes"),
            ),
        )
        tx_id = cur.lastrowid

        # --- Sync to holdings: weighted-avg add for buy, qty deduct for sell.
        # Match on (symbol, market) when market is supplied — A-share and
        # cn_fund codes are both 6 digits, so symbol alone can collide.
        # Symbol-only fallback keeps old clients (no market field) working.
        if market:
            holding = conn.execute(
                """SELECT id, quantity, avg_cost, market, currency FROM holdings
                   WHERE symbol = ? AND market = ?""",
                (symbol, market),
            ).fetchone()
        else:
            holding = conn.execute(
                """SELECT id, quantity, avg_cost, market, currency FROM holdings
                   WHERE symbol = ? LIMIT 1""",
                (symbol,),
            ).fetchone()

        if side == "buy":
            if holding is None:
                # New symbol — auto-create holdings row if we have enough
                # info (market + currency). If market missing, fall back
                # to whatever the form supplied.
                conn.execute(
                    """INSERT INTO holdings
                       (symbol, name, market, currency, quantity, avg_cost,
                        unit, notes)
                       VALUES (?, ?, ?, ?, ?, ?, 'share', NULL)""",
                    (symbol, name or symbol, market, currency, qty, price),
                )
            else:
                old_qty = holding["quantity"] or 0
                old_avg = holding["avg_cost"] or 0
                new_qty = old_qty + qty
                new_avg = (
                    ((old_qty * old_avg) + (qty * price)) / new_qty
                    if new_qty else price
                )
                conn.execute(
                    """UPDATE holdings
                       SET quantity = ?, avg_cost = ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (new_qty, new_avg, holding["id"]),
                )
        else:  # sell
            if holding is None:
                warnings.append(
                    f"{symbol}: 卖出但持仓里没有该 symbol,交易已记录但持仓未变化"
                )
            else:
                old_qty = holding["quantity"] or 0
                old_avg = holding["avg_cost"] or 0
                new_qty = max(0.0, old_qty - qty)
                # Realized gain on the executed quantity (capped at what's
                # actually held — selling more than you have can't realize
                # P&L on phantom shares).
                realized_qty = min(qty, old_qty) if old_qty > 0 else 0
                realized_gain = (price - old_avg) * realized_qty
                conn.execute(
                    """UPDATE holdings
                       SET quantity = ?,
                           realized_pnl = COALESCE(realized_pnl, 0) + ?,
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    (new_qty, realized_gain, holding["id"]),
                )
                if old_qty - qty < -1e-6:
                    warnings.append(
                        f"{symbol}: 卖出 {qty} 超过持仓现量 {old_qty},已置为 0;"
                        f"如有疑义请去持仓 tab 校正"
                    )
        conn.commit()

    # Best-effort live price for a holding we just created — mirrors the
    # holdings-POST path. Day-P&L math skips unpriced rows, so without a
    # price this buy's cash-out would distort today's pnl until the next
    # 5-min cron fill.
    if side == "buy" and holding is None and market:
        try:
            from ..services.prices import fetch_one as _fetch_price_one
            p = _fetch_price_one(market, symbol)
            if p is not None:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE holdings SET last_price = ?, "
                        "last_price_at = datetime('now'), prev_close = ? "
                        "WHERE symbol = ? AND market = ?",
                        (p["last"], p.get("prev_close"), symbol, market),
                    )
                    conn.commit()
        except Exception as e:
            print(f"[transactions] post-insert price fetch failed: {e}",
                  file=sys.stderr)

    return jsonify(id=tx_id, warnings=warnings), 201


@bp.put("/<int:tid>")
def update_transaction(tid: int):
    """Partial-update a transaction's metadata fields only.

    Whitelisted to fields that don't affect the holdings sync that was
    applied at POST time: trade_date, notes, name, market. Mutating
    side/quantity/price/currency would require reversing the original
    weighted-avg apply (which we deliberately don't snapshot — see the
    delete handler) so those stay immutable; delete + re-post is the
    intended path if you need to correct them, and reconcile holdings
    by hand afterward.
    """
    d = request.get_json(force=True) or {}
    fields = ["trade_date", "notes", "name", "market"]
    sets, vals = [], []
    for f in fields:
        if f in d:
            sets.append(f"{f} = ?")
            vals.append(d[f])
    if not sets:
        return jsonify(error="nothing to update"), 400
    vals.append(tid)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", vals
        )
        conn.commit()
    if cur.rowcount == 0:
        return jsonify(error="not found"), 404
    return jsonify(ok=True)


@bp.delete("/<int:tid>")
def delete_transaction(tid: int):
    """Delete a transaction. Does NOT roll back holdings.

    Reverse-applying a weighted-avg buy reliably requires the baseline at
    apply time, which we don't snapshot. So delete just removes the
    transactions row and warns the user to reconcile manually.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT side, quantity, symbol FROM transactions WHERE id = ?",
            (tid,),
        ).fetchone()
        conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
        conn.commit()
    warnings = []
    if row:
        warnings.append(
            f"已删除 {row['side']} {row['symbol']} × {row['quantity']},"
            f"**持仓表不会自动回滚**;如有需要请去持仓 tab 校正数量 / 成本"
        )
    return jsonify(ok=True, warnings=warnings)
