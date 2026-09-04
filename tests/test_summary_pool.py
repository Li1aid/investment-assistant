"""/api/summary + /api/pool cross-currency math, and fx.convert itself.

fx_rates only ever holds the four fetched directions (AUDCNY, USDCNY,
HKDCNY, CNYAUD) — every other conversion must resolve via inversion or
cross-via-CNY. These tests pin the paths that used to silently drop
holdings from the totals.
"""
from __future__ import annotations

import pytest

from app.services.fx import convert


FX = {"AUDCNY": 5.0, "USDCNY": 7.0, "HKDCNY": 0.9, "CNYAUD": 0.2}


@pytest.mark.parametrize("amount,src,dst,expected", [
    (100, "AUD", "CNY", 500.0),          # direct
    (70, "CNY", "USD", 10.0),            # inverse of a fetched pair
    (100, "AUD", "USD", 500.0 / 7.0),    # cross via CNY, inverted 2nd leg
    (100, "HKD", "USD", 90.0 / 7.0),     # cross via CNY, inverted 2nd leg
    (10, "USD", "AUD", 70.0 * 0.2),      # cross via CNY, direct 2nd leg
    (5, "CNY", "CNY", 5.0),              # identity
])
def test_convert_paths(amount, src, dst, expected):
    assert convert(amount, src, dst, FX) == pytest.approx(expected)


def test_convert_returns_none_without_a_path():
    assert convert(1, "GBP", "JPY", FX) is None
    assert convert(1, "AUD", "USD", {}) is None


def _seed_portfolio(seed_holding):
    # mv: CNY 100 (cost 80), USD 70 (cost 50), AUD 80 (cost 60)
    seed_holding("CNH", market="cn_a", currency="CNY", qty=10, avg=8, last=10)
    seed_holding("USH", market="us", currency="USD", qty=1, avg=50, last=70)
    seed_holding("AUH", market="asx_pocket", currency="AUD", qty=2, avg=30, last=40)


def test_summary_totals_include_every_currency(client, seed_holding, seed_fx):
    _seed_portfolio(seed_holding)
    seed_fx(FX)

    s = client.get("/api/summary").get_json()

    assert s["totals"]["CNY"]["market_value"] == pytest.approx(100 + 70 * 7 + 80 * 5)
    # USD total needs AUD→USD via CNY — the path that used to be dropped.
    assert s["totals"]["USD"]["market_value"] == pytest.approx(100 / 7 + 70 + 80 * 5 / 7)
    assert s["totals"]["AUD"]["market_value"] == pytest.approx(100 * 0.2 + 70 * 7 * 0.2 + 80)

    assert s["by_currency"]["USD"]["market_value"] == pytest.approx(70)
    assert s["totals"]["CNY"]["pnl"] == pytest.approx(20 + 20 * 7 + 20 * 5)


def test_pool_converts_all_holdings_to_cny(client, seed_holding, seed_fx):
    _seed_portfolio(seed_holding)
    seed_fx(FX)
    client.post("/api/buckets/CNY/transactions", json={"amount": 10000})

    p = client.get("/api/pool").get_json()
    assert p["principal_cny"] == pytest.approx(10000)
    assert p["pnl_cny"] == pytest.approx(20 + 20 * 7 + 20 * 5)     # 260
    assert p["total_cny"] == pytest.approx(10260)
    assert p["cost_basis_cny"] == pytest.approx(80 + 50 * 7 + 60 * 5)
    assert p["skipped_currencies"] == []
