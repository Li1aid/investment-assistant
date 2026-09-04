"""SQLite connection helpers and schema bootstrap."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  market TEXT NOT NULL,
  currency TEXT NOT NULL,
  quantity REAL NOT NULL,
  avg_cost REAL NOT NULL,
  last_price REAL,
  last_price_at TEXT,
  prev_close REAL,
  unit TEXT DEFAULT 'share',
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(symbol, market)
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,
  market TEXT,
  side TEXT NOT NULL,
  quantity REAL NOT NULL,
  price REAL NOT NULL,
  fee REAL DEFAULT 0,
  currency TEXT NOT NULL,
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watchlist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  market TEXT NOT NULL,
  currency TEXT NOT NULL,
  last_price REAL,
  last_price_at TEXT,
  notes TEXT,
  UNIQUE(symbol, market)
);

CREATE TABLE IF NOT EXISTS fx_rates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pair TEXT NOT NULL UNIQUE,        -- e.g. AUDCNY, HKDCNY, USDCNY
  rate REAL NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_date TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,             -- hold / buy / sell / mixed / other
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_action_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  name TEXT,
  side TEXT NOT NULL,               -- buy / sell
  quantity REAL NOT NULL,
  price REAL NOT NULL,
  ai_followed TEXT,                 -- yes / partial / no / null
  FOREIGN KEY(action_id) REFERENCES daily_actions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS daily_pnl (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pnl_date TEXT NOT NULL UNIQUE,
  market_value_cny REAL,
  market_value_aud REAL,
  prev_market_value_cny REAL,
  prev_market_value_aud REAL,
  net_invested_cny REAL DEFAULT 0,
  net_invested_aud REAL DEFAULT 0,
  pnl_cny REAL,
  pnl_aud REAL,
  pnl_pct_cny REAL,
  pnl_pct_aud REAL,
  notes TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS currency_buckets (
  currency TEXT PRIMARY KEY,           -- 'CNY' | 'AUD' | 'USD' | 'HKD'
  total_amount REAL NOT NULL DEFAULT 0, -- legacy seed; canonical total is now SUM(pool_transactions)
  notes TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_transactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  currency TEXT NOT NULL,              -- 'CNY' for now (single pool)
  amount REAL NOT NULL,                -- positive=deposit, negative=withdraw
  note TEXT,                           -- user memo, e.g. "10月工资"
  tx_date TEXT NOT NULL,               -- YYYY-MM-DD (Sydney)
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pool_tx_ccy_date
  ON pool_transactions(currency, tx_date);
"""


def get_db_path() -> Path:
    """Database file location.

    Override with PORTFOLIO_DB env var (useful for testing / sandbox
    environments where the project dir is on a FUSE mount that does
    not support SQLite journal files).
    """
    import os
    env = os.environ.get("PORTFOLIO_DB")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "portfolio.db"


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    import os
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Allow journal mode override for sandbox/FUSE environments where
    # the default DELETE journal cannot be created. Defaults to WAL on
    # real systems for better concurrent-read behavior.
    journal = os.environ.get("PORTFOLIO_JOURNAL_MODE", "WAL")
    try:
        conn.execute(f"PRAGMA journal_mode = {journal}")
    except sqlite3.OperationalError:
        pass
    return conn


def _ensure_column(conn, table: str, column: str, decl: str) -> None:
    """Idempotently add a column to an existing table.

    `CREATE TABLE IF NOT EXISTS` in SCHEMA only creates fresh tables — it
    silently skips tables that already exist, so columns added to SCHEMA
    after first init never propagate to production until we explicitly
    ALTER. PRAGMA table_info gives us the current column set so we ALTER
    only when needed.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_schema(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        # USD support — additive, idempotent migrations.
        _ensure_column(conn, "daily_pnl", "market_value_usd", "REAL")
        _ensure_column(conn, "daily_pnl", "prev_market_value_usd", "REAL")
        _ensure_column(conn, "daily_pnl", "net_invested_usd", "REAL DEFAULT 0")
        _ensure_column(conn, "daily_pnl", "pnl_usd", "REAL")
        _ensure_column(conn, "daily_pnl", "pnl_pct_usd", "REAL")
        # Realized P&L accumulator: every sell adds (sell_price -
        # avg_cost_at_sell) × sold_qty here, so the total per-symbol P&L
        # we report stays correct after partial exits (instead of just
        # showing the floating piece on whatever's still held).
        _ensure_column(conn, "holdings", "realized_pnl", "REAL DEFAULT 0")
        # Display-only region tag for the holdings sub-tab grouping. Lets
        # US-listed Chinese ADRs (BABA, TCEHY, ...) show up under 港股
        # without changing `market`, which still drives the price-fetch
        # routing (market='us' → Yahoo, market='hk' → Tencent quotes API).
        # NULL falls back to `market` in the UI grouping logic.
        _ensure_column(conn, "holdings", "region", "TEXT")
        # Priority band (1–6) assigned by the external Market Tracker
        # scanner. 1 = L3 actionable, 6 = 不追高 主升浪. NULL = not
        # scanned yet. Sortable + colored as a badge in the UI.
        _ensure_column(conn, "watchlist", "priority_band", "INTEGER")
        conn.commit()


def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(sql, list(params)).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(sql, list(params)).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with get_conn() as conn:
        cur = conn.execute(sql, list(params))
        conn.commit()
        return cur.lastrowid
