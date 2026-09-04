"""FX conversion shared by the summary / pool endpoints.

Rates live in the fx_rates table as directed pairs (AUDCNY, USDCNY, ...).
`convert` resolves direct, inverted, and cross-via-CNY paths — inverting
each leg when only the opposite direction was fetched — so a missing
CNYUSD row no longer silently drops AUD/HKD holdings from USD totals.
"""
from __future__ import annotations

from ..db import query_all


def load_fx() -> dict[str, float]:
    return {r["pair"]: r["rate"] for r in query_all("SELECT pair, rate FROM fx_rates")}


def _rate(src: str, dst: str, fx: dict[str, float]) -> float | None:
    if src == dst:
        return 1.0
    direct = fx.get(f"{src}{dst}")
    if direct:
        return direct
    inv = fx.get(f"{dst}{src}")
    if inv:
        return 1.0 / inv
    return None


def convert(amount: float, src: str, dst: str, fx: dict[str, float]) -> float | None:
    """Convert `amount` from src → dst. Returns None when no rate path exists."""
    r = _rate(src, dst, fx)
    if r is None:
        leg1 = _rate(src, "CNY", fx)
        leg2 = _rate("CNY", dst, fx)
        if leg1 is None or leg2 is None:
            return None
        r = leg1 * leg2
    return amount * r
