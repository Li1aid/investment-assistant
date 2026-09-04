"""Bearer-token protection for the portfolio API.

When API_TOKEN is set (Railway env), every /api request except the
health check must carry ``Authorization: Bearer <token>``.  The HTML and
static assets remain public, but holdings, transactions and totals do not.

Unset API_TOKEN (local development and tests) disables the gate.
"""
from __future__ import annotations

import hmac
import os

from flask import jsonify, request

_PUBLIC_API_PATHS = frozenset({"/api/health"})


def init_auth(app) -> None:
    token = os.environ.get("API_TOKEN", "")
    if not token:
        return

    expected = f"Bearer {token}"

    @app.before_request
    def _require_token_for_api():
        if request.path.startswith("/api/") and request.path not in _PUBLIC_API_PATHS:
            got = request.headers.get("Authorization", "")
            if not hmac.compare_digest(got, expected):
                return jsonify(error="unauthorized"), 401
