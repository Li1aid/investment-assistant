"""Watchlist CRUD."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..db import get_conn, query_all

bp = Blueprint("watchlist", __name__, url_prefix="/api/watchlist")


@bp.get("")
def list_watchlist():
    # Order: market group, then priority_band ascending (NULLs last so
    # un-scanned rows fall to the bottom of each market), then symbol.
    rows = query_all(
        """SELECT id, symbol, name, market, currency, last_price,
                  last_price_at, notes, priority_band
           FROM watchlist
           ORDER BY market,
                    CASE WHEN priority_band IS NULL THEN 1 ELSE 0 END,
                    priority_band,
                    symbol"""
    )
    return jsonify(rows)


@bp.post("")
def add_watch():
    d = request.get_json(force=True)
    required = ["symbol", "name", "market", "currency"]
    missing = [k for k in required if d.get(k) in (None, "")]
    if missing:
        return jsonify(error=f"missing fields: {missing}"), 400
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO watchlist (symbol, name, market, currency, notes)
               VALUES (?,?,?,?,?)""",
            (d["symbol"], d["name"], d["market"], d["currency"], d.get("notes")),
        )
        conn.commit()
    return jsonify(id=cur.lastrowid), 201


@bp.put("/<int:wid>")
def update_watch(wid: int):
    """Partial update — only the fields present in the payload are touched.
    Used today to edit `notes` from the client; symbol/market/etc are also
    settable in case we wire an inline-edit UI later."""
    d = request.get_json(force=True)
    fields = ["symbol", "name", "market", "currency", "notes", "priority_band"]
    sets, vals = [], []
    for f in fields:
        if f in d:
            sets.append(f"{f} = ?")
            vals.append(d[f])
    if not sets:
        return jsonify(error="nothing to update"), 400
    vals.append(wid)
    with get_conn() as conn:
        conn.execute(f"UPDATE watchlist SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    return jsonify(ok=True)


@bp.delete("/<int:wid>")
def delete_watch(wid: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE id = ?", (wid,))
        conn.commit()
    return jsonify(ok=True)
