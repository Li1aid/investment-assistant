"""Price fetchers.

Markets and the source we use:

  cn_a        Chinese A-share ETF (6-digit code, no .SS/.SZ suffix)
              akshare.fund_etf_spot_em()  --> live snapshot
  cn_fund     Open-end mutual fund (6-digit code, Tencent / 天天基金 etc.)
              akshare.fund_open_fund_info_em(symbol=code, indicator='单位净值走势')
              Returns NAV per unit. For lump-sum positions we record NAV
              as-is in last_price and leave conversion to the dashboard.
  hk          Hong Kong stock (5-digit, leading zero optional)
              akshare.stock_hk_spot_em()
  asx_pocket  ASX ETF via underlying ticker (IOO.AX, NDQ.AX) — yfinance
              We map POCKET-IOO -> IOO.AX, POCKET-NDQ -> NDQ.AX and update
              last_price as the unit price ratio (current / cost basis).
              Because positions are lump-sum, last_price is taken as the
              current market unit price *scaled* so quantity * last_price
              equals current valuation. Easier: skip auto-refresh and let
              the user manually update (set notes to remind).
  spot_gold   Shanghai gold benchmark / sina spot. akshare provides
              spot_hist_sge() / spot_quotations_sge() for Au99.99.

Network failures are caught and the row is reported as 'failed' so the
overall refresh completes.
"""
from __future__ import annotations

import datetime as dt
import sys
from typing import Any

from ..db import get_conn, query_all


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ----- A-share ETF -----------------------------------------------------------
def fetch_cn_a_etf(codes: list[str]) -> dict[str, dict]:
    """Live quotes for A-share ETFs via Tencent qt.

    Returns {code: {"last": ..., "prev_close": ...}} for the requested
    6-digit codes. Single batched HTTP call (vs akshare's previous
    14-page paginated scrape that took 38-42s and occasionally dropped
    the whole batch — see docs/superpowers/specs/2026-05-16-cn-hk-tencent-source.md).
    """
    prefixed = [f"{_tencent_prefix('cn_a', c)}{c}" for c in codes]
    return fetch_tencent_quotes(prefixed)


# ----- Open-end mutual fund (NAV) --------------------------------------------
def fetch_cn_fund_nav(code: str) -> float | None:
    nav, _ = fetch_cn_fund_nav_with_prev(code)
    return nav


def fetch_cn_fund_nav_with_prev(code: str) -> tuple[float | None, float | None]:
    import akshare as ak
    try:
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        last = float(df.iloc[-1]["单位净值"])
        prev = float(df.iloc[-2]["单位净值"]) if len(df) >= 2 else None
        return last, prev
    except Exception as e:
        print(f"[warn] fund nav fetch failed for {code}: {e}", file=sys.stderr)
        return None, None


# ----- Spot gold (Au99.99 in CNY/g) ------------------------------------------
def fetch_spot_gold_cny_per_g() -> float | None:
    price, _ = fetch_spot_gold_with_prev()
    return price


def fetch_spot_gold_with_prev() -> tuple[float | None, float | None]:
    """Returns (today_price, prev_day_close) in CNY/gram, or (None, None)."""
    import akshare as ak
    # Prefer history endpoint so we can get both today and previous close.
    try:
        df = ak.spot_hist_sge(symbol="Au99.99")
        for col in ("收盘", "收盘价", "close", "最新价"):
            if col in df.columns:
                last = float(df.iloc[-1][col])
                prev = float(df.iloc[-2][col]) if len(df) >= 2 else None
                return last, prev
    except Exception:
        pass
    # Fallback: just today's quote, no prev.
    try:
        df = ak.spot_quotations_sge()
        for col in ("最新价", "现价", "收盘价"):
            if col in df.columns:
                return float(df.iloc[-1][col]), None
    except Exception:
        pass
    return None, None


# ----- ASX Pocket ------------------------------------------------------------
POCKET_TICKER_MAP = {
    "POCKET-IOO": "IOO.AX",  # Global 100
    "POCKET-NDQ": "NDQ.AX",  # Tech Savvy
}


def fetch_asx_ticker(ticker: str) -> dict | None:
    """Return {"last": ..., "prev_close": ...} or None on failure."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        info = t.fast_info
        last = float(getattr(info, "last_price", None) or info["last_price"])
        try:
            prev = float(getattr(info, "previous_close", None) or info["previous_close"])
        except (KeyError, TypeError, ValueError):
            prev = None
        return {"last": last, "prev_close": prev}
    except Exception as e:
        print(f"[warn] yfinance fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


# ----- Tencent qt (realtime quote, cn_a + hk) --------------------------------
def _tencent_prefix(market: str, symbol: str) -> str:
    """Tencent qt exchange prefix for a (market, symbol) tuple.

    cn_a routing by 6-digit code prefix:
      51 / 56 / 58 — 上海 ETF / LOF (51xx classic, 56xx since 2020, 58xx LOF)
      60          — 上海主板个股 (600xxx / 601xxx / 603xxx / 605xxx)
      68          — 上海科创板 (688xxx STAR Market — 中微公司、寒武纪等)
      00 / 30 / 15 / 16 / 18 — 深圳主板 / 创业板 / 深圳 ETF / LOF / 国债逆回购
      8 / 4 / 92  — 北交所 (含原新三板精选层)
      default     — fall back to 'sz' for unknown 0/3/1 prefixes

    hk → always 'hk'. Tencent's HK format keeps the leading zero
         (e.g. 'hk09988' for Alibaba), so callers should NOT strip it.
    """
    if market == "cn_a":
        if symbol.startswith(("51", "56", "58", "60", "68")):
            return "sh"
        if symbol.startswith(("4", "8", "92")):
            return "bj"
        return "sz"
    if market == "hk":
        return "hk"
    raise ValueError(f"no tencent prefix for market {market!r}")


def fetch_tencent_quotes(prefixed_codes: list[str],
                         retries: int = 2) -> dict[str, dict]:
    """Batched live quote via Tencent qt API.

    Input is a list of fully-prefixed Tencent codes, e.g.
    ['sh510300', 'sz159770', 'hk09988']. Returns
    {clean_code: {'last': float, 'prev_close': float | None}}
    where clean_code drops the prefix so downstream callers
    (fetch_cn_a_etf, the hk loop in refresh_all) get back the
    same dict shape akshare used to return.

    Empty input returns {} without making a request. On total
    failure after retries, raises the underlying exception so
    the caller can decide whether to log fetch_failed or fall
    through silently.

    Field positions (verified 2026-05-16 by empirical probe):
      A-share (sh/sz) and HK both use idx 3 = last, idx 4 = prev_close.
    """
    import time
    import re
    import requests

    if not prefixed_codes:
        return {}

    url = "http://qt.gtimg.cn/q=" + ",".join(prefixed_codes)

    last_err = None
    body = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            # Tencent serves GBK-encoded text on some setups; force the
            # encoding rather than letting requests guess.
            try:
                body = r.content.decode("gbk")
            except UnicodeDecodeError:
                body = r.text
            break
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    if body is None:
        raise last_err if last_err else RuntimeError("tencent qt unknown")

    out: dict[str, dict] = {}
    for m in re.finditer(r'v_([a-z]{2,3}\d+)="([^"]*)"', body):
        prefixed = m.group(1)
        fields = m.group(2).split("~")
        if len(fields) < 5:
            continue
        clean = re.sub(r'^(sh|sz|hk)', '', prefixed)
        try:
            last = float(fields[3])
        except (ValueError, IndexError):
            continue  # No usable last price → drop this row.
        try:
            prev = float(fields[4])
        except (ValueError, IndexError):
            prev = None
        out[clean] = {"last": last, "prev_close": prev}
    return out


# ----- US / HK via yfinance --------------------------------------------------
def _yf_ticker_for(market: str, symbol: str) -> str:
    """Translate (market, symbol) into the form yfinance expects.

    us → symbol as-is (NVDA, AMD, AVGO, ...)
    hk → strip leading zero, append .HK (09988 → 9988.HK; 89988 → 89988.HK)
    """
    if market == "us":
        return symbol
    if market == "hk":
        return f"{int(symbol)}.HK"
    raise ValueError(f"unsupported market for yahoo: {market!r}")


def fetch_yahoo_ticker(market: str, symbol: str) -> dict | None:
    """Return {"last": ..., "prev_close": ...} or None on failure.

    Same shape as fetch_asx_ticker — that path is already used by
    asx_pocket holdings. Network / data failures are caught and
    reported as None so the refresh loop continues for other rows.
    """
    import yfinance as yf
    yf_sym = _yf_ticker_for(market, symbol)
    try:
        t = yf.Ticker(yf_sym)
        info = t.fast_info
        last = float(getattr(info, "last_price", None) or info["last_price"])
        try:
            prev = float(getattr(info, "previous_close", None) or info["previous_close"])
        except (KeyError, TypeError, ValueError):
            prev = None
        return {"last": last, "prev_close": prev}
    except Exception as e:
        print(f"[warn] yfinance fetch failed for {yf_sym}: {e}", file=sys.stderr)
        return None


# ----- Single-symbol fetch (used by holdings POST for instant last_price) ----
def fetch_one(market: str, symbol: str) -> dict | None:
    """Best-effort live price for a single (market, symbol).

    Returns {"last": float, "prev_close": float | None} or None on
    failure / unsupported market. Used by holdings POST to populate
    last_price immediately after insert (instead of waiting for the
    periodic launchd refresh).

    spot_gold is intentionally not supported here — its price depends
    on a per-holding spread stored in holdings.notes, which the periodic
    refresh_all() reads. The insert still proceeds; the next 5-min
    refresh fills last_price in.
    """
    try:
        if market in ("cn_a", "hk"):
            prefixed = f"{_tencent_prefix(market, symbol)}{symbol}"
            return fetch_tencent_quotes([prefixed]).get(symbol)
        if market == "us":
            return fetch_yahoo_ticker("us", symbol)
        if market == "asx_pocket":
            ticker = POCKET_TICKER_MAP.get(symbol)
            return fetch_asx_ticker(ticker) if ticker else None
        if market == "cn_fund":
            last, prev = fetch_cn_fund_nav_with_prev(symbol)
            if last is None:
                return None
            return {"last": last, "prev_close": prev}
    except Exception as e:
        print(f"[prices.fetch_one] {market}/{symbol} failed: {e}", file=sys.stderr)
    return None


# ----- FX --------------------------------------------------------------------
FX_PAIRS = ["AUDCNY=X", "HKDCNY=X", "USDCNY=X", "CNYAUD=X"]


def fetch_fx() -> dict[str, float]:
    import yfinance as yf
    out: dict[str, float] = {}
    for sym in FX_PAIRS:
        try:
            info = yf.Ticker(sym).fast_info
            rate = float(getattr(info, "last_price", None) or info["last_price"])
            out[sym.replace("=X", "")] = rate
        except Exception as e:
            print(f"[warn] fx {sym}: {e}", file=sys.stderr)
    return out


# ----- Top-level refresh -----------------------------------------------------
def refresh_all() -> dict[str, Any]:
    """Refresh prices for every holding and watchlist row.

    Returns a summary dict per row: {symbol, market, status, new_price?}.
    Never raises — partial failures are reported per-symbol.
    """
    now = _now_iso()
    summary: list[dict] = []

    holdings = query_all("SELECT id, symbol, market, notes FROM holdings")
    watch = query_all("SELECT id, symbol, market FROM watchlist")

    cn_a_codes = sorted({r["symbol"] for r in (holdings + watch) if r["market"] == "cn_a"})
    cn_fund_codes = [r["symbol"] for r in holdings if r["market"] == "cn_fund"]
    pocket_holdings = [r for r in holdings if r["market"] == "asx_pocket"]
    gold_holdings = [r for r in holdings if r["market"] == "spot_gold"]

    # --- A-share ETFs (batched single fetch) ---
    if cn_a_codes:
        try:
            cn_prices = fetch_cn_a_etf(cn_a_codes)
        except Exception as e:
            cn_prices = {}
            summary.append({"market": "cn_a", "status": "fetch_failed", "error": str(e)})
        with get_conn() as conn:
            for row in holdings + watch:
                if row["market"] != "cn_a":
                    continue
                p = cn_prices.get(row["symbol"])
                if p is None:
                    summary.append({"symbol": row["symbol"], "market": "cn_a", "status": "no_price"})
                    continue
                table = "holdings" if row in holdings else "watchlist"
                if table == "holdings":
                    conn.execute(
                        "UPDATE holdings SET last_price = ?, last_price_at = ?, prev_close = ? WHERE id = ?",
                        (p["last"], now, p.get("prev_close"), row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE watchlist SET last_price = ?, last_price_at = ? WHERE id = ?",
                        (p["last"], now, row["id"]),
                    )
                summary.append({"symbol": row["symbol"], "market": "cn_a", "status": "ok",
                                "price": p["last"], "prev_close": p.get("prev_close")})
            conn.commit()

    # --- Mutual funds (NAV) ---
    # Treat fund holdings as shares × NAV. prev_close = NAV of prior trading day.
    for code in cn_fund_codes:
        nav, prev_nav = fetch_cn_fund_nav_with_prev(code)
        if nav is None:
            summary.append({"symbol": code, "market": "cn_fund", "status": "no_price"})
            continue
        with get_conn() as conn:
            for row in holdings:
                if row["market"] == "cn_fund" and row["symbol"] == code:
                    conn.execute(
                        "UPDATE holdings SET last_price = ?, last_price_at = ?, prev_close = ? WHERE id = ?",
                        (nav, now, prev_nav, row["id"]),
                    )
            conn.commit()
        summary.append({"symbol": code, "market": "cn_fund", "status": "ok",
                        "price": nav, "prev_close": prev_nav})

    # --- Spot gold (CNY per gram) ---
    # Bank physical gold sells at SGE spot + a bank spread.
    if gold_holdings:
        price, prev_sge = fetch_spot_gold_with_prev()
        if price is None:
            summary.append({"market": "spot_gold", "status": "no_price"})
        else:
            import re as _re
            with get_conn() as conn:
                for row in gold_holdings:
                    notes = row["notes"] or ""
                    m = _re.search(r"spread\s*=\s*([\d.]+)", notes)
                    spread = float(m.group(1)) if m else 5.0
                    effective = price + spread
                    effective_prev = (prev_sge + spread) if prev_sge is not None else None
                    conn.execute(
                        "UPDATE holdings SET last_price = ?, last_price_at = ?, prev_close = ? WHERE id = ?",
                        (effective, now, effective_prev, row["id"]),
                    )
                conn.commit()
            summary.append({"market": "spot_gold", "status": "ok",
                            "sge_price": price, "applied_spread": spread,
                            "prev_close": (prev_sge + spread) if prev_sge is not None else None})

    # --- ASX Pocket: write underlying ETF price into last_price. Holding
    # is stored as units × purchase_price, so pnl computes correctly.
    for row in pocket_holdings:
        ticker = POCKET_TICKER_MAP.get(row["symbol"])
        if not ticker:
            continue
        p = fetch_asx_ticker(ticker)
        if p is None:
            summary.append({"symbol": row["symbol"], "market": "asx_pocket", "underlying": ticker, "status": "no_price"})
            continue
        with get_conn() as conn:
            conn.execute(
                "UPDATE holdings SET last_price = ?, last_price_at = ?, prev_close = ? WHERE id = ?",
                (p["last"], now, p.get("prev_close"), row["id"]),
            )
            conn.commit()
        summary.append({"symbol": row["symbol"], "market": "asx_pocket", "underlying": ticker,
                        "price": p["last"], "prev_close": p.get("prev_close"), "status": "ok"})

    # --- HK via Tencent qt (batched single HTTP) ---
    hk_rows = [r for r in (holdings + watch) if r["market"] == "hk"]
    if hk_rows:
        try:
            hk_prices = fetch_tencent_quotes(
                [f"hk{r['symbol']}" for r in hk_rows]
            )
        except Exception as e:
            hk_prices = {}
            summary.append({"market": "hk", "status": "fetch_failed",
                            "error": str(e)})
        with get_conn() as conn:
            for row in hk_rows:
                p = hk_prices.get(row["symbol"])
                if p is None:
                    summary.append({"symbol": row["symbol"], "market": "hk",
                                    "status": "no_price"})
                    continue
                if row in holdings:
                    conn.execute(
                        "UPDATE holdings SET last_price = ?, last_price_at = ?, "
                        "prev_close = ? WHERE id = ?",
                        (p["last"], now, p.get("prev_close"), row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE watchlist SET last_price = ?, last_price_at = ? "
                        "WHERE id = ?",
                        (p["last"], now, row["id"]),
                    )
                summary.append({"symbol": row["symbol"], "market": "hk",
                                "status": "ok", "price": p["last"],
                                "prev_close": p.get("prev_close")})
            conn.commit()

    # --- US via yfinance (per row — no Chinese alternative) ---
    us_rows = [r for r in (holdings + watch) if r["market"] == "us"]
    for row in us_rows:
        p = fetch_yahoo_ticker("us", row["symbol"])
        if p is None:
            summary.append({"symbol": row["symbol"], "market": "us",
                            "status": "no_price"})
            continue
        with get_conn() as conn:
            if row in holdings:
                conn.execute(
                    "UPDATE holdings SET last_price = ?, last_price_at = ?, "
                    "prev_close = ? WHERE id = ?",
                    (p["last"], now, p.get("prev_close"), row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE watchlist SET last_price = ?, last_price_at = ? "
                    "WHERE id = ?",
                    (p["last"], now, row["id"]),
                )
            conn.commit()
        summary.append({"symbol": row["symbol"], "market": "us", "status": "ok",
                        "price": p["last"], "prev_close": p.get("prev_close")})

    # --- FX rates ---
    fx = fetch_fx()
    if fx:
        with get_conn() as conn:
            for pair, rate in fx.items():
                conn.execute(
                    """INSERT INTO fx_rates (pair, rate, fetched_at)
                       VALUES (?,?,?)
                       ON CONFLICT(pair) DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at""",
                    (pair, rate, now),
                )
            conn.commit()
        summary.append({"market": "fx", "status": "ok", "pairs": fx})

    return {"refreshed_at": now, "items": summary}
