"""Transactions POST/PUT/DELETE and their holdings sync."""
from __future__ import annotations


def _holding(client, symbol, market=None):
    rows = client.get("/api/holdings").get_json()
    return next((h for h in rows if h["symbol"] == symbol
                 and (market is None or h["market"] == market)), None)


def _post_txn(client, **kw):
    body = {"trade_date": "2026-07-01", "symbol": "AAA", "market": "cn_a",
            "side": "buy", "quantity": 10, "price": 5.0, "currency": "CNY"}
    body.update(kw)
    return client.post("/api/transactions", json=body)


def test_buy_new_symbol_creates_holding_with_market(client):
    resp = _post_txn(client, symbol="NEW1", market="us", currency="USD")
    assert resp.status_code == 201

    h = _holding(client, "NEW1")
    assert h is not None
    assert h["market"] == "us"          # NULL market would dodge price refresh
    assert h["quantity"] == 10 and h["avg_cost"] == 5.0


def test_buy_existing_applies_weighted_avg(client):
    _post_txn(client, quantity=10, price=10.0)
    _post_txn(client, quantity=30, price=20.0)
    h = _holding(client, "AAA")
    assert h["quantity"] == 40
    assert abs(h["avg_cost"] - 17.5) < 1e-9


def test_sell_deducts_qty_and_accumulates_realized_pnl(client):
    _post_txn(client, quantity=100, price=10.0)

    _post_txn(client, side="sell", quantity=40, price=15.0)
    h = _holding(client, "AAA")
    assert h["quantity"] == 60
    assert abs(h["realized_pnl"] - (15.0 - 10.0) * 40) < 1e-9

    _post_txn(client, side="sell", quantity=10, price=5.0)
    h = _holding(client, "AAA")
    assert h["quantity"] == 50
    assert abs(h["realized_pnl"] - (200 + (5.0 - 10.0) * 10)) < 1e-9


def test_oversell_clamps_to_zero_and_warns(client):
    _post_txn(client, quantity=10, price=10.0)
    resp = _post_txn(client, side="sell", quantity=25, price=12.0)
    assert resp.get_json()["warnings"]

    h = _holding(client, "AAA")
    assert h["quantity"] == 0
    # Realized only on the 10 shares actually held — no phantom P&L.
    assert abs(h["realized_pnl"] - (12.0 - 10.0) * 10) < 1e-9


def test_sell_unknown_symbol_warns_but_records_ledger(client):
    resp = _post_txn(client, symbol="GHOST", side="sell")
    assert resp.status_code == 201
    assert resp.get_json()["warnings"]
    assert _holding(client, "GHOST") is None
    assert len(client.get("/api/transactions").get_json()) == 1


def test_market_disambiguates_colliding_six_digit_codes(client):
    """A-share and cn_fund codes are both 6 digits — the sync must match
    on (symbol, market), not symbol alone."""
    _post_txn(client, symbol="160632", market="cn_a", quantity=100, price=1.0)
    _post_txn(client, symbol="160632", market="cn_fund", quantity=50, price=2.0)

    etf = _holding(client, "160632", market="cn_a")
    fund = _holding(client, "160632", market="cn_fund")
    assert etf["quantity"] == 100 and etf["avg_cost"] == 1.0
    assert fund["quantity"] == 50 and fund["avg_cost"] == 2.0

    _post_txn(client, symbol="160632", market="cn_fund", side="sell",
              quantity=20, price=3.0)
    assert _holding(client, "160632", market="cn_a")["quantity"] == 100
    assert _holding(client, "160632", market="cn_fund")["quantity"] == 30


def test_side_and_number_validation(client):
    assert _post_txn(client, side="dividend").status_code == 400
    assert _post_txn(client, quantity=0).status_code == 400
    assert _post_txn(client, price="abc").status_code == 400
    assert client.post("/api/transactions", json={"symbol": "X"}).status_code == 400


def test_put_edits_metadata_only(client):
    tid = _post_txn(client).get_json()["id"]

    resp = client.put(f"/api/transactions/{tid}",
                      json={"notes": "fixed", "trade_date": "2026-06-30"})
    assert resp.status_code == 200
    t = client.get("/api/transactions").get_json()[0]
    assert t["notes"] == "fixed" and t["trade_date"] == "2026-06-30"

    # Immutable fields are not in the whitelist → nothing to update.
    assert client.put(f"/api/transactions/{tid}",
                      json={"quantity": 999}).status_code == 400
    assert client.get("/api/transactions").get_json()[0]["quantity"] == 10

    assert client.put("/api/transactions/999999",
                      json={"notes": "x"}).status_code == 404

    # PUT never touches holdings.
    assert _holding(client, "AAA")["quantity"] == 10


def test_delete_keeps_holdings_and_warns(client):
    tid = _post_txn(client, quantity=10, price=5.0).get_json()["id"]
    resp = client.delete(f"/api/transactions/{tid}")
    assert resp.get_json()["warnings"]

    assert client.get("/api/transactions").get_json() == []
    # Deliberately NO holdings rollback — the user reconciles by hand.
    assert _holding(client, "AAA")["quantity"] == 10
