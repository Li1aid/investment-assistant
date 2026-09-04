"""Timezone-aware date helpers.

Python's `date.today()` reads the system TZ via libc. In containers
without system tzdata (e.g., some Nixpacks Python images on Railway),
libc silently falls back to UTC even when the `TZ` env var is set —
so `date.today()` returns the UTC date, not the user-configured TZ
date. This module bypasses libc by using `zoneinfo` directly with the
PyPI `tzdata` package as the data source.

Use `today_iso()` instead of `date.today().isoformat()` everywhere
that the "calendar day" matters (P&L row dates, trade dates, etc.).
"""
from __future__ import annotations

import os
from datetime import date, datetime

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


def today_date() -> date:
    """Today's date in the configured TZ. Falls back to local date if
    TZ is unset OR zoneinfo can't resolve the TZ name."""
    tz_name = os.environ.get("TZ")
    if tz_name and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            pass
    return date.today()


def today_iso() -> str:
    """Today's date in the configured TZ, ISO format (YYYY-MM-DD)."""
    return today_date().isoformat()


def et_today_iso() -> str:
    """Today's date in US Eastern (America/New_York) — i.e., the
    current US trading-day calendar date.

    Used by P&L / trade-date logic so that a single buy made at any
    Sydney time during a US session belongs to the same trading day
    no matter where the user clicks "save":

      Sydney Tue 14:00 (AEST) → ET Mon 20:00 → trading day = Monday ET
      Sydney Sat 02:00       → ET Fri 12:00 → trading day = Friday ET
      Sydney Sun 12:00       → ET Sat 22:00 → trading day = Saturday ET (no session)
    """
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        except Exception:
            pass
    return date.today().isoformat()
