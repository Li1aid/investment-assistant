"""资金池 — pool_transactions ledger and its SUM-derived totals."""
from __future__ import annotations


def _cny_total(client):
    rows = client.get("/api/buckets").get_json()
    row = next((r for r in rows if r["currency"] == "CNY"), None)
    return row["total_amount"] if row else None


def test_deposit_and_withdraw_sum(client):
    r = client.post("/api/buckets/CNY/transactions",
                    json={"amount": 1000, "note": "工资"})
    assert r.status_code == 201
    client.post("/api/buckets/CNY/transactions", json={"amount": -300})

    assert _cny_total(client) == 700

    txs = client.get("/api/buckets/CNY/transactions").get_json()
    assert len(txs) == 2
    assert txs[0]["amount"] in (-300, 1000)  # newest first by tx_date, id


def test_delete_transaction_recomputes_total(client):
    client.post("/api/buckets/CNY/transactions", json={"amount": 1000})
    tid = client.post("/api/buckets/CNY/transactions",
                      json={"amount": 500}).get_json()["transaction"]["id"]

    assert client.delete(f"/api/buckets/CNY/transactions/{tid}").get_json()["total_amount"] == 1000
    assert client.delete(f"/api/buckets/CNY/transactions/{tid}").status_code == 404


def test_reset_replaces_history_with_single_row(client):
    client.post("/api/buckets/CNY/transactions", json={"amount": 1000})
    client.post("/api/buckets/CNY/transactions", json={"amount": 2000})

    r = client.put("/api/buckets/CNY", json={"total_amount": 5000})
    assert r.get_json()["total_amount"] == 5000

    txs = client.get("/api/buckets/CNY/transactions").get_json()
    assert len(txs) == 1 and txs[0]["amount"] == 5000
    assert _cny_total(client) == 5000


def test_validation(client):
    assert client.post("/api/buckets/CNY/transactions", json={"amount": 0}).status_code == 400
    assert client.post("/api/buckets/CNY/transactions", json={}).status_code == 400
    assert client.put("/api/buckets/CNY", json={"total_amount": -5}).status_code == 400
    assert client.put("/api/buckets/CNY", json={}).status_code == 400
