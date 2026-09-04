"""End-of-day P&L computation.

We compute today's P&L PER HOLDING as
  (last_price - prev_close) * quantity
…then bucket by currency. This matches the live "今日盈亏" cards on the
dashboard exactly — no discrepancy between the live view and the locked
calendar entry.

Run after the relevant markets close so prev_close has been refreshed
to today's actual previous day's close.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.app_factory import create_app  # noqa: E402
from app.db import get_conn, query_all  # noqa: E402
from app.timeutil import et_today_iso  # noqa: E402


def _log(msg: str) -> None:
    from datetime import datetime
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def _today_pnl_from_day_changes() -> tuple[float, float, float, float, float, float]:
    """Compute today's market value vs yesterday's market value, accounting
    for positions opened / topped-up TODAY.

    Formula per holding:
        yesterday_qty = current_qty - today_qty_change
        prev_mv       = yesterday_qty * prev_close
        mv            = current_qty   * last_price

    Then in main(): pnl = mv - prev_mv - net_invested_today
    which correctly handles three cases:
      1. Held all day, no trades  → pnl = (last - prev_close) * qty
      2. Top-up today              → pnl = day-move on yesterday's qty + bought-at-vs-current on new qty
      3. Brand new buy today       → pnl = (last - your_buy_price) * qty  (yesterday_qty = 0)

    Returns (mv_cny, prev_mv_cny, mv_aud, prev_mv_aud, mv_usd, prev_mv_usd).
    """
    target_date = et_today_iso()
    # Per-symbol net quantity change today (buy=+, sell=−), read from the
    # transactions ledger — the single source of trade entry (both the
    # transactions tab and the holdings form write a row there).
    today_qty_changes: dict[str, float] = {}
    for r in query_all(
        """SELECT symbol,
                  SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) AS net_qty
           FROM transactions
           WHERE trade_date = ?
           GROUP BY symbol""",
        (target_date,),
    ):
        today_qty_changes[r["symbol"]] = r["net_qty"] or 0

    rows = query_all(
        """SELECT symbol, currency, quantity, last_price, prev_close
           FROM holdings WHERE last_price IS NOT NULL"""
    )
    mv_cny = prev_cny = 0.0
    mv_aud = prev_aud = 0.0
    mv_usd = prev_usd = 0.0
    for r in rows:
        qty = r["quantity"] or 0
        last = r["last_price"] or 0
        prev = r["prev_close"] or 0
        if not prev:
            continue
        delta = today_qty_changes.get(r["symbol"], 0)
        yesterday_qty = qty - delta
        mv = qty * last
        pv = yesterday_qty * prev  # what we OWNED yesterday at yesterday's close
        if r["currency"] == "CNY":
            mv_cny += mv
            prev_cny += pv
        elif r["currency"] == "AUD":
            mv_aud += mv
            prev_aud += pv
        elif r["currency"] == "USD":
            mv_usd += mv
            prev_usd += pv
    return mv_cny, prev_cny, mv_aud, prev_aud, mv_usd, prev_usd


def _net_invested_today() -> tuple[float, float, float]:
    """Net cash deployed today per currency. buy = +, sell = −."""
    today = et_today_iso()
    rows = query_all(
        """SELECT side, quantity, price, currency
           FROM transactions
           WHERE trade_date = ?""",
        (today,),
    )
    net_cny = net_aud = net_usd = 0.0
    for r in rows:
        amt = (r["quantity"] or 0) * (r["price"] or 0)
        signed = amt if r["side"] == "buy" else -amt
        ccy = r["currency"] or "CNY"
        if ccy == "CNY":
            net_cny += signed
        elif ccy == "AUD":
            net_aud += signed
        elif ccy == "USD":
            net_usd += signed
    return net_cny, net_aud, net_usd


def main() -> int:
    # NOTE: we do NOT refresh prices here. The dedicated 'prices' launchd
    # job runs every 5 minutes during market hours, so last_price &
    # prev_close are already current. Re-pulling here used to take 70+
    # seconds — pointless and brittle. Reads only from now on.
    app = create_app()
    with app.app_context():
        try:
            mv_cny, prev_cny, mv_aud, prev_aud, mv_usd, prev_usd = _today_pnl_from_day_changes()
            net_cny, net_aud, net_usd = _net_invested_today()
            pnl_cny = mv_cny - prev_cny - net_cny
            pnl_aud = mv_aud - prev_aud - net_aud
            pnl_usd = mv_usd - prev_usd - net_usd
            pnl_pct_cny = (pnl_cny / prev_cny * 100.0) if prev_cny else 0.0
            pnl_pct_aud = (pnl_aud / prev_aud * 100.0) if prev_aud else 0.0
            pnl_pct_usd = (pnl_usd / prev_usd * 100.0) if prev_usd else 0.0
            today = et_today_iso()
            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO daily_pnl
                       (pnl_date,
                        market_value_cny, market_value_aud, market_value_usd,
                        prev_market_value_cny, prev_market_value_aud, prev_market_value_usd,
                        net_invested_cny, net_invested_aud, net_invested_usd,
                        pnl_cny, pnl_aud, pnl_usd,
                        pnl_pct_cny, pnl_pct_aud, pnl_pct_usd)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(pnl_date) DO UPDATE SET
                         market_value_cny=excluded.market_value_cny,
                         market_value_aud=excluded.market_value_aud,
                         market_value_usd=excluded.market_value_usd,
                         prev_market_value_cny=excluded.prev_market_value_cny,
                         prev_market_value_aud=excluded.prev_market_value_aud,
                         prev_market_value_usd=excluded.prev_market_value_usd,
                         net_invested_cny=excluded.net_invested_cny,
                         net_invested_aud=excluded.net_invested_aud,
                         net_invested_usd=excluded.net_invested_usd,
                         pnl_cny=excluded.pnl_cny,
                         pnl_aud=excluded.pnl_aud,
                         pnl_usd=excluded.pnl_usd,
                         pnl_pct_cny=excluded.pnl_pct_cny,
                         pnl_pct_aud=excluded.pnl_pct_aud,
                         pnl_pct_usd=excluded.pnl_pct_usd""",
                    (today,
                     mv_cny, mv_aud, mv_usd,
                     prev_cny, prev_aud, prev_usd,
                     net_cny, net_aud, net_usd,
                     pnl_cny, pnl_aud, pnl_usd,
                     pnl_pct_cny, pnl_pct_aud, pnl_pct_usd),
                )
                conn.commit()
            _log(f"  today CNY: mv=¥{mv_cny:,.2f} prev=¥{prev_cny:,.2f} "
                 f"net_invested=¥{net_cny:,.2f} → pnl=¥{pnl_cny:+,.2f} ({pnl_pct_cny:+.2f}%)")
            _log(f"  today AUD: mv=A${mv_aud:,.2f} prev=A${prev_aud:,.2f} "
                 f"net_invested=A${net_aud:,.2f} → pnl=A${pnl_aud:+,.2f} ({pnl_pct_aud:+.2f}%)")
            _log(f"  today USD: mv=US${mv_usd:,.2f} prev=US${prev_usd:,.2f} "
                 f"net_invested=US${net_usd:,.2f} → pnl=US${pnl_usd:+,.2f} ({pnl_pct_usd:+.2f}%)")
        except Exception:
            _log("pnl compute FAILED:")
            traceback.print_exc()
            return 1

        _log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
