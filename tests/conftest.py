"""Test fixtures for the Flask app.

Each test gets a fresh Flask app pointed at a throwaway SQLite DB under
pytest's tmp_path. The scheduler stays dormant (ENABLE_CRON unset), the
auth gate stays off (API_TOKEN forced empty so a token in the local
.env can't leak in — load_dotenv never overrides existing env vars),
and outbound price fetches are stubbed to None so no test touches the
network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable so `from app.app_factory import ...`
# resolves whether pytest is run from the project root or from tests/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stub the single-symbol price fetch used by holdings/transactions
    POST so tests never hit Tencent/Yahoo."""
    import app.services.prices as prices
    monkeypatch.setattr(prices, "fetch_one", lambda market, symbol: None)


@pytest.fixture
def app_and_db(tmp_path, monkeypatch):
    """Fresh Flask app on a temp SQLite DB. Yields (flask_app, db_path)."""
    db_path = tmp_path / "test_portfolio.db"
    monkeypatch.setenv("PORTFOLIO_DB", str(db_path))
    monkeypatch.delenv("ENABLE_CRON", raising=False)
    monkeypatch.setenv("API_TOKEN", "")

    from app.app_factory import create_app
    app = create_app()
    app.config["TESTING"] = True
    yield app, db_path


@pytest.fixture
def client(app_and_db):
    app, _ = app_and_db
    return app.test_client()


@pytest.fixture
def seed_holding(app_and_db):
    """Returns a callable that inserts a holdings row directly, bypassing
    the POST endpoint — for tests that need a position that existed
    before today (with prev_close / last_price already set)."""
    def _seed(symbol, market="cn_a", currency="CNY", qty=100.0, avg=10.0,
              last=None, prev=None, realized=0.0):
        from app.db import get_conn
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO holdings
                   (symbol, name, market, currency, quantity, avg_cost,
                    last_price, prev_close, realized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, f"{symbol} name", market, currency, qty, avg,
                 last, prev, realized),
            )
            conn.commit()
    return _seed


@pytest.fixture
def seed_fx(app_and_db):
    """Insert fx_rates rows in the fetched directions only (AUDCNY,
    USDCNY, HKDCNY, CNYAUD) — the same shape prices.fetch_fx writes —
    so tests exercise the inverse / cross-via-CNY resolution paths."""
    def _seed(pairs=None):
        from app.db import get_conn
        pairs = pairs or {"AUDCNY": 5.0, "USDCNY": 7.0,
                          "HKDCNY": 0.9, "CNYAUD": 0.2}
        with get_conn() as conn:
            for pair, rate in pairs.items():
                conn.execute(
                    "INSERT INTO fx_rates (pair, rate, fetched_at) VALUES (?, ?, 'test')",
                    (pair, rate),
                )
            conn.commit()
        return pairs
    return _seed
