"""Seed the database with a small fictional demo portfolio.

This is idempotent — re-running it will not duplicate rows (UNIQUE
constraint on (symbol, market) for holdings and watchlist).

last_price is left NULL — the live refresh fills it in.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_conn, init_schema, get_db_path


# (symbol, name, market, currency, quantity, avg_cost, unit, notes)
HOLDINGS = [
    ("NVDA", "NVIDIA", "us", "USD", 10, 100.00, "share", "Demo data"),
    ("513120", "港股创新药ETF广发", "cn_a", "CNY", 1000, 1.00, "share", "示例数据"),
    ("POCKET-IOO", "Global 100 (IOO.AX)", "asx_pocket", "AUD", 5, 150.00, "share", "Demo data"),
]

WATCHLIST: list[tuple] = [
    ("MSFT", "Microsoft", "us", "USD", "Demo data"),
]


def seed() -> None:
    init_schema()
    with get_conn() as conn:
        for sym, name, market, ccy, qty, avg, unit, notes in HOLDINGS:
            conn.execute(
                """INSERT OR IGNORE INTO holdings
                   (symbol, name, market, currency, quantity, avg_cost, unit, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sym, name, market, ccy, qty, avg, unit, notes),
            )
        for sym, name, market, ccy, notes in WATCHLIST:
            conn.execute(
                """INSERT OR IGNORE INTO watchlist
                   (symbol, name, market, currency, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (sym, name, market, ccy, notes),
            )
        conn.commit()
    print(f"[ok] seeded {len(HOLDINGS)} holdings, {len(WATCHLIST)} watchlist rows -> {get_db_path()}")


if __name__ == "__main__":
    seed()
