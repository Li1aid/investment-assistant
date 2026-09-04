"""API_TOKEN gate: portfolio data requires a bearer token."""
from __future__ import annotations

import pytest

WATCH = {"symbol": "NVDA", "name": "NVIDIA", "market": "us", "currency": "USD"}


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_DB", str(tmp_path / "auth.db"))
    monkeypatch.delenv("ENABLE_CRON", raising=False)
    monkeypatch.setenv("API_TOKEN", "sekret")

    from app.app_factory import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_data_gets_require_token(auth_client):
    assert auth_client.get("/api/holdings").status_code == 401
    assert auth_client.get("/api/summary").status_code == 401
    assert auth_client.get("/").status_code == 200


def test_health_check_stays_public(auth_client):
    assert auth_client.get("/api/health").status_code == 200


def test_get_with_token_passes(auth_client):
    resp = auth_client.get(
        "/api/holdings",
        headers={"Authorization": "Bearer sekret"},
    )
    assert resp.status_code == 200


def test_writes_require_token(auth_client):
    assert auth_client.post("/api/watchlist", json=WATCH).status_code == 401
    assert auth_client.post(
        "/api/watchlist", json=WATCH,
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    assert auth_client.delete("/api/holdings/1").status_code == 401


def test_write_with_token_passes(auth_client):
    resp = auth_client.post(
        "/api/watchlist", json=WATCH,
        headers={"Authorization": "Bearer sekret"},
    )
    assert resp.status_code == 201


def test_gate_off_when_token_unset(client):
    # The default fixture forces API_TOKEN="" — writes need no header.
    assert client.post("/api/watchlist", json=WATCH).status_code == 201
