"""Holdings CRUD + the transactions-ledger day-P&L contract."""
from __future__ import annotations

from app.timeutil import et_today_iso


def _holding(client, symbol):
    rows = client.get("/api/holdings").get_json()
    return next((h for h in rows if h["symbol"] == symbol), None)


NVDA = {"symbol": "NVDA", "name": "NVIDIA", "market": "us",
        "currency": "USD", "quantity": 10, "avg_cost": 100.0}


def test_post_new_holding_creates_row_and_ledger_entry(client):
    resp = client.post("/api/holdings", json=NVDA)
    assert resp.status_code == 201
    assert resp.get_json()["warnings"] == []

    h = _holding(client, "NVDA")
    assert h["quantity"] == 10 and h["avg_cost"] == 100.0

    # The buy must land in the transactions ledger — day-P&L math and the
    # transactions tab both read from there.
    txs = client.get("/api/transactions").get_json()
    assert len(txs) == 1
    t = txs[0]
    assert (t["symbol"], t["side"], t["quantity"], t["price"]) == ("NVDA", "buy", 10, 100.0)


def test_post_existing_holding_applies_weighted_avg(client):
    client.post("/api/holdings", json=NVDA)
    client.post("/api/holdings", json={**NVDA, "quantity": 10, "avg_cost": 200.0})

    h = _holding(client, "NVDA")
    assert h["quantity"] == 20
    assert h["avg_cost"] == 150.0
    assert len(client.get("/api/transactions").get_json()) == 2


def test_post_validation(client):
    assert client.post("/api/holdings", json={"symbol": "X"}).status_code == 400
    assert client.post("/api/holdings", json={**NVDA, "quantity": -1}).status_code == 400
    assert client.post("/api/holdings", json={**NVDA, "avg_cost": "abc"}).status_code == 400


def test_put_and_delete(client):
    client.post("/api/holdings", json=NVDA)
    hid = _holding(client, "NVDA")["id"]

    assert client.put(f"/api/holdings/{hid}",
                      json={"notes": "n1", "region": "hk"}).get_json()["ok"]
    h = _holding(client, "NVDA")
    assert h["notes"] == "n1" and h["region"] == "hk"

    client.delete(f"/api/holdings/{hid}")
    assert _holding(client, "NVDA") is None


def test_day_pnl_counts_positions_held_since_yesterday(client, seed_holding):
    seed_holding("ETF1", qty=100, avg=9, last=11, prev=10)
    h = _holding(client, "ETF1")
    assert abs(h["day_pnl"] - 100 * (11 - 10)) < 1e-9
    assert abs(h["day_change_pct"] - 10.0) < 1e-9


def test_day_pnl_unmoved_by_same_day_buy_at_current_price(client, seed_holding):
    """Buying more today AT the current price adds zero day-P&L. The old
    daily_action_trades path missed transactions-tab buys and would have
    counted the new shares as held all day."""
    seed_holding("ETF1", qty=100, avg=9, last=11, prev=10)
    client.post("/api/transactions", json={
        "trade_date": et_today_iso(), "symbol": "ETF1", "market": "cn_a",
        "side": "buy", "quantity": 50, "price": 11, "currency": "CNY"})

    h = _holding(client, "ETF1")
    assert h["quantity"] == 150
    # mv(150*11) − yesterday_mv(100*10) − invested_today(50*11) = 100
    assert abs(h["day_pnl"] - 100.0) < 1e-6
