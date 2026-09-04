"""Currency pool endpoints — total capital tracked as a transaction log.

Each deposit / withdrawal is a `pool_transactions` row (positive = deposit,
negative = withdraw). The bucket's `total_amount` is the SUM of those rows.

The legacy `currency_buckets.total_amount` is kept as a write-through cache
for backward compat, but every read goes through the SUM so it can't drift.
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, jsonify, request

from ..db import get_conn, query_all, query_one

bp = Blueprint("buckets", __name__, url_prefix="/api/buckets")


def _today_sydney_iso() -> str:
    """YYYY-MM-DD in Sydney TZ. Used as the default tx_date when the client
    doesn't pass one. SQLite's date('now','localtime') would do the same on
    Railway (TZ=Australia/Sydney) but we compute in Python for symmetry with
    the rest of the codebase."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Australia/Sydney")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _sync_cache(conn, ccy: str) -> float:
    """Recompute total_amount from pool_transactions and persist into
    currency_buckets so legacy reads stay consistent. Returns the total."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM pool_transactions WHERE currency=?",
        (ccy,),
    ).fetchone()
    total = float(row["total"]) if row else 0.0
    conn.execute(
        """INSERT INTO currency_buckets (currency, total_amount, updated_at)
           VALUES (?, ?, datetime('now'))
           ON CONFLICT(currency) DO UPDATE SET
             total_amount = excluded.total_amount,
             updated_at = excluded.updated_at""",
        (ccy, total),
    )
    return total


@bp.get("")
def list_buckets():
    """Return [{currency, total_amount, notes, updated_at}] with total_amount
    computed live from pool_transactions."""
    rows = query_all(
        """SELECT cb.currency,
                  COALESCE(SUM(pt.amount), cb.total_amount, 0) AS total_amount,
                  cb.notes, cb.updated_at
           FROM currency_buckets cb
           LEFT JOIN pool_transactions pt ON pt.currency = cb.currency
           GROUP BY cb.currency
           ORDER BY cb.currency"""
    )
    # Currencies with txns but no cb row (shouldn't happen in normal flow,
    # but UNION just in case migrations diverge).
    extra = query_all(
        """SELECT currency, SUM(amount) AS total_amount,
                  NULL AS notes, MAX(created_at) AS updated_at
           FROM pool_transactions
           WHERE currency NOT IN (SELECT currency FROM currency_buckets)
           GROUP BY currency"""
    )
    return jsonify(rows + extra)


@bp.put("/<ccy>")
def set_bucket(ccy: str):
    """Legacy: set the total to an absolute amount. Implemented as a
    'reset' transaction — first deletes existing txns for the currency,
    then writes a single seed row with the new total. Use POST .../transactions
    instead for incremental adds."""
    d = request.get_json(force=True) or {}
    if "total_amount" not in d:
        return jsonify(error="total_amount required"), 400
    try:
        total = float(d["total_amount"])
    except (TypeError, ValueError):
        return jsonify(error="total_amount must be numeric"), 400
    if total < 0:
        return jsonify(error="total_amount must be >= 0"), 400
    ccy = ccy.upper()
    with get_conn() as conn:
        conn.execute("DELETE FROM pool_transactions WHERE currency=?", (ccy,))
        conn.execute(
            """INSERT INTO pool_transactions (currency, amount, note, tx_date)
               VALUES (?, ?, '总额重置', ?)""",
            (ccy, total, _today_sydney_iso()),
        )
        new_total = _sync_cache(conn, ccy)
        conn.commit()
    return jsonify(ok=True, currency=ccy, total_amount=new_total)


@bp.get("/<ccy>/transactions")
def list_transactions(ccy: str):
    """Return all deposit/withdraw transactions for a currency, newest first."""
    ccy = ccy.upper()
    rows = query_all(
        """SELECT id, currency, amount, note, tx_date, created_at
           FROM pool_transactions
           WHERE currency = ?
           ORDER BY tx_date DESC, id DESC""",
        (ccy,),
    )
    return jsonify(rows)


@bp.post("/<ccy>/transactions")
def add_transaction(ccy: str):
    """Add a deposit (amount > 0) or withdrawal (amount < 0). Server
    recomputes the bucket total from SUM after insert."""
    d = request.get_json(force=True) or {}
    if "amount" not in d:
        return jsonify(error="amount required"), 400
    try:
        amount = float(d["amount"])
    except (TypeError, ValueError):
        return jsonify(error="amount must be numeric"), 400
    if amount == 0:
        return jsonify(error="amount must be non-zero"), 400
    note = (d.get("note") or "").strip() or None
    tx_date = (d.get("tx_date") or "").strip() or _today_sydney_iso()
    ccy = ccy.upper()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pool_transactions (currency, amount, note, tx_date)
               VALUES (?, ?, ?, ?)""",
            (ccy, amount, note, tx_date),
        )
        new_total = _sync_cache(conn, ccy)
        conn.commit()
        row = conn.execute(
            "SELECT id, currency, amount, note, tx_date, created_at FROM pool_transactions WHERE id=?",
            (cur.lastrowid,),
        ).fetchone()
    return jsonify(ok=True, total_amount=new_total, transaction=dict(row)), 201


@bp.delete("/<ccy>/transactions/<int:tx_id>")
def delete_transaction(ccy: str, tx_id: int):
    """Undo a mis-entered transaction."""
    ccy = ccy.upper()
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM pool_transactions WHERE id=? AND currency=?",
            (tx_id, ccy),
        )
        if cur.rowcount == 0:
            return jsonify(error="not found"), 404
        new_total = _sync_cache(conn, ccy)
        conn.commit()
    return jsonify(ok=True, total_amount=new_total)
