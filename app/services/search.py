"""Multi-market ticker search with in-process caching.

Sources:
    cn_fund → akshare.fund_name_em (cached 24h, lazy-loaded, ~2.4s cold)
    cn_a    → akshare.stock_info_a_code_name (primary)
              → falls back to combined sh+sz+bj exchange endpoints if primary fails
    us + hk → Yahoo Finance autocomplete (live, single call returns both)

Public API:
    search(q, limit=20) -> {"results": [...], "degraded": [markets]}
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

_TTL_SECONDS = 24 * 3600
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _cached(key: str, loader: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Return cached rows for `key`, or call `loader()` and cache for 24h."""
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    rows = loader()
    _cache[key] = (now + _TTL_SECONDS, rows)
    return rows


def _filter_local(rows: list[dict[str, Any]], q: str, limit: int = 5) -> list[dict[str, Any]]:
    """Rank rows by relevance to query `q`.

    Ranking (lower number = better match):
        0 = symbol exact
        1 = symbol prefix
        2 = name prefix (case-insensitive)
        3 = symbol/name substring
    """
    qn = q.strip().lower()
    if not qn:
        return []
    matches: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        sym = str(r.get("symbol", "")).lower()
        name = str(r.get("name", "")).lower()
        rank = None
        if sym == qn:
            rank = 0
        elif sym.startswith(qn):
            rank = 1
        elif name.startswith(qn):
            rank = 2
        elif qn in sym or qn in name:
            rank = 3
        if rank is not None:
            matches.append((rank, r))
    matches.sort(key=lambda m: m[0])
    return [r for _, r in matches[:limit]]


def _load_cn_a() -> list[dict[str, Any]]:
    """All A-share individual stocks (code + name only — no spot data).

    Four-tier loader chain — earlier tiers are lighter / more portable:

    1. **Sina HQ paginated** (primary) — pure requests, paginated 100/page,
       ~6s parallel cold load. Most reliable historically.
    2. **Eastmoney HTTP JSON** — direct REST, no pandas.
    3. ak.stock_info_a_code_name() — single call, all exchanges combined.
    4. Stitch sh + sz + bj exchange-specific akshare endpoints.

    Returns rows with schema {symbol, name, market:'cn_a', currency:'CNY',
    exchange: 'SSE'|'SZSE'|'BSE'|''}.
    """
    # Tier 1: Sina HQ (most reliable)
    try:
        rows = _from_sina_paginated()
        if rows:
            return rows
    except Exception as e:
        print(f"[search] cn_a sina failed: {e}", file=sys.stderr)

    # Tier 2: Eastmoney HTTP (no pandas)
    try:
        rows = _from_eastmoney_http()
        if rows:
            return rows
    except Exception as e:
        print(f"[search] cn_a eastmoney-http failed: {e}", file=sys.stderr)

    # Tiers 2 + 3 need akshare → pandas → numpy. Skip the import entirely
    # if it's broken on this platform (Railway/Nixpacks libstdc++ issue).
    try:
        import akshare as ak
    except ImportError as e:
        print(f"[search] akshare unavailable (numpy/libstdc++ issue?): {e}",
              file=sys.stderr)
        return []

    try:
        df = ak.stock_info_a_code_name()
        return [
            {
                "symbol": str(r["code"]).zfill(6),
                "name": str(r["name"]),
                "market": "cn_a",
                "currency": "CNY",
                "exchange": _exchange_for_code(str(r["code"]).zfill(6)),
            }
            for _, r in df.iterrows()
        ]
    except Exception as e:
        print(f"[search] cn_a akshare combined failed: {e}", file=sys.stderr)

    out: list[dict[str, Any]] = []
    for fn, ex in (
        (getattr(ak, "stock_info_sh_name_code", None), "SSE"),
        (getattr(ak, "stock_info_sz_name_code", None), "SZSE"),
        (getattr(ak, "stock_info_bj_name_code", None), "BSE"),
    ):
        if fn is None:
            continue
        try:
            df = fn()
        except Exception as e:
            print(f"[search] cn_a {ex} backup failed: {e}", file=sys.stderr)
            continue
        cols = list(df.columns)
        code_col = next((c for c in ("证券代码", "A股代码", "code") if c in cols), None)
        name_col = next((c for c in ("证券简称", "A股简称", "name") if c in cols), None)
        if not code_col or not name_col:
            continue
        for _, r in df.iterrows():
            code = str(r[code_col]).zfill(6)
            out.append({
                "symbol": code, "name": str(r[name_col]),
                "market": "cn_a", "currency": "CNY", "exchange": ex,
            })
    return out


def _from_sina_paginated() -> list[dict[str, Any]]:
    """Sina HQ list endpoint, paginated 100 rows/page in parallel.

    Sina caps `num` at 100 regardless of value, so we fan out ~60 pages
    across 8 threads. ~6s cold load for 5500+ A-shares. Pure HTTP, no
    pandas, no numpy — same portability as the Eastmoney fallback but
    historically more available.
    """
    import json
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed

    url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn/",
    }

    def fetch(page: int) -> list[dict]:
        try:
            resp = requests.get(
                url,
                params={"page": page, "num": 100, "sort": "symbol",
                        "asc": 1, "node": "hs_a"},
                headers=headers, timeout=8,
            )
            return json.loads(resp.text) if resp.status_code == 200 else []
        except Exception:
            return []

    # 60 pages × 100 = 6000 row ceiling, enough headroom for the ~5500 A-shares.
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch, p) for p in range(1, 61)]
        for fut in as_completed(futs):
            all_rows.extend(fut.result())

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in all_rows:
        code = str(r.get("code") or "").strip()
        name = str(r.get("name") or "").strip()
        if not code or not name or code in seen:
            continue
        seen.add(code)
        out.append({
            "symbol": code.zfill(6),
            "name": name,
            "market": "cn_a",
            "currency": "CNY",
            "exchange": _exchange_for_code(code.zfill(6)),
        })
    return out


def _from_eastmoney_http() -> list[dict[str, Any]]:
    """Pull the full A-share list via Eastmoney's public clist endpoint.

    Single GET returns all stocks (we ask for pz=10000 which fits the
    ~5500 A-shares plus headroom). No pagination, no auth, no Python
    dependencies beyond `requests` (already in requirements.txt).

    fs filter explanation (Eastmoney sector codes):
      m:0 t:6   — 上海主板 A
      m:0 t:80  — 上海科创板
      m:1 t:2   — 深圳主板 A
      m:1 t:23  — 深圳创业板
      m:0 t:81 + s:2048 — 北交所
    """
    import requests
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 10000, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f3",
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
        "fields": "f12,f14",
    }
    resp = requests.get(
        url, params=params, timeout=10,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = (payload.get("data") or {}).get("diff") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        code = str(r.get("f12") or "").strip()
        name = str(r.get("f14") or "").strip()
        if not code or not name or code == "-":
            continue
        out.append({
            "symbol": code.zfill(6),
            "name": name,
            "market": "cn_a",
            "currency": "CNY",
            "exchange": _exchange_for_code(code.zfill(6)),
        })
    return out


def _exchange_for_code(code: str) -> str:
    """Identify the exchange a Chinese stock code belongs to. Based on the
    standard numbering convention — adapted from typical akshare conventions."""
    if not code:
        return ""
    p = code[0]
    if p == "6":             # 沪市主板
        return "SSE"
    if p in ("0", "3"):      # 深市主板 / 创业板
        return "SZSE"
    if p == "8" or code.startswith("43"):  # 北交所 / 新三板精选层
        return "BSE"
    return ""


def _load_cn_fund() -> list[dict[str, Any]]:
    """All CN funds (open + ETF) from East Money via akshare.fund_name_em.

    Single-fetch (no pagination), ~2.4s for ~26k rows. Reliable in practice.
    Earlier alternatives (fund_etf_spot_em, stock_zh_a_spot_em, stock_hk_spot_em)
    paginate under the hood and break mid-fetch when East Money is slow.
    """
    import akshare as ak
    df = ak.fund_name_em()
    return [
        {
            "symbol": str(r["基金代码"]),
            "name": str(r["基金简称"]),
            "market": "cn_fund",
            "currency": "CNY",
            "exchange": "OTC",
        }
        for _, r in df.iterrows()
    ]


def _fetch_yahoo(q: str) -> list[dict[str, Any]]:
    """Live Yahoo Finance autocomplete. Returns up to 5 hits for each of us + hk.

    Yahoo returns multi-market candidates in one call. We dispatch by suffix:

      - no dot       → market='us', currency='USD' (NASDAQ/NYSE primary)
      - '.HK' suffix → market='hk', currency='HKD' (strip suffix, zero-pad to 5)
      - other suffix → skip (we don't support .AS, .L, .TW, .MX, etc.)

    Uses `requests` (already in requirements.txt) so we pick up the certifi
    CA bundle — Python.org Python on macOS has no system trust store and
    `urllib.request` against query2.finance.yahoo.com fails with SSL errors.

    Yahoo's autocomplete 400s on queries containing CJK characters, so we
    short-circuit on those and return an empty list. That is *not* a
    degraded state — Yahoo simply doesn't index Chinese names. CN A / CN
    fund searches go through akshare.
    """
    # CJK short-circuit (CJK Unified Ideographs block).
    if any('一' <= c <= '鿿' for c in q):
        return []

    import requests
    resp = requests.get(
        "https://query2.finance.yahoo.com/v1/finance/search",
        params={"q": q, "quotesCount": 10, "newsCount": 0, "lang": "en-US"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()

    us_count, hk_count, out = 0, 0, []
    for qt in data.get("quotes", []):
        qtype = qt.get("quoteType", "")
        sym = qt.get("symbol", "")
        if qtype not in ("EQUITY", "ETF") or not sym:
            continue
        name = qt.get("shortname") or qt.get("longname") or sym
        exch = qt.get("exchange", "")

        if "." not in sym:
            if us_count >= 5:
                continue
            out.append({
                "symbol": sym,
                "name": name,
                "market": "us",
                "currency": "USD",
                "exchange": exch,
            })
            us_count += 1
        elif sym.endswith(".HK"):
            if hk_count >= 5:
                continue
            base = sym[:-3].zfill(5)  # "9988" → "09988"
            out.append({
                "symbol": base,
                "name": name,
                "market": "hk",
                "currency": "HKD",
                "exchange": "HKEX",
            })
            hk_count += 1
        # else: foreign listing (.AS, .L, .TW, .MX, ...) — skip.

    return out


def search(q: str, limit: int = 20) -> dict[str, Any]:
    """Search supported markets in parallel; return merged + ranked results.

    Single-source failures are isolated: the source is added to `degraded`
    and the other source still returns. Empty/short queries return an empty
    result without hitting any source.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": [], "degraded": []}

    degraded: list[str] = []

    def safe_cn_fund() -> list[dict[str, Any]]:
        try:
            rows = _cached("cn_fund", _load_cn_fund)
            return _filter_local(rows, q)
        except Exception as e:
            print(f"[search] cn_fund failed: {e}", file=sys.stderr)
            degraded.append("cn_fund")
            return []

    def safe_cn_a() -> list[dict[str, Any]]:
        try:
            rows = _cached("cn_a", _load_cn_a)
            if not rows:
                degraded.append("cn_a")
                return []
            return _filter_local(rows, q)
        except Exception as e:
            print(f"[search] cn_a failed: {e}", file=sys.stderr)
            degraded.append("cn_a")
            return []

    def safe_yahoo() -> list[dict[str, Any]]:
        try:
            return _fetch_yahoo(q)
        except Exception as e:
            print(f"[search] yahoo failed: {e}", file=sys.stderr)
            degraded.extend(["us", "hk"])
            return []

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_fund = ex.submit(safe_cn_fund)
        f_a = ex.submit(safe_cn_a)
        f_yh = ex.submit(safe_yahoo)
        results: list[dict[str, Any]] = []
        results.extend(f_fund.result())
        results.extend(f_a.result())
        results.extend(f_yh.result())

    return {"results": results[:limit], "degraded": sorted(set(degraded))}
