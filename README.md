# Investment Assistant

Multi-market portfolio tracker and decision-support dashboard covering
US, Hong Kong, mainland China and Australian assets.

This public repository is a portfolio sample. It contains fictional seed
data only; personal holdings and production credentials are not included.

## Quick start (local)

```bash
pip3 install -r requirements.txt --break-system-packages   # akshare is large
python3 scripts/init_db.py
python3 scripts/seed.py
./run.sh          # http://127.0.0.1:5180
```

## Project layout

```
app/
  app_factory.py   Flask factory: blueprints, schema init, cron start
  db.py            SQLite helpers + idempotent schema / additive migrations
  cron.py          APScheduler (ENABLE_CRON=1): prices 5-min, P&L 16:15 ET
  timeutil.py      TZ-safe date helpers (Sydney display, ET trading day)
  api/             REST blueprints: holdings, transactions, watchlist,
                   buckets (资金池), snapshots, prices, pnl, search, meta
  services/
    prices.py      quote fetchers (Tencent qt, yfinance, akshare) + FX rates
    search.py      multi-market ticker search, 24h in-process cache
    fx.py          FX conversion shared by /api/summary and /api/pool
    snapshot.py    daily market-value snapshot writer (cron + manual button)
scripts/           init_db / fictional seed / compute_daily_pnl
templates/ static/ Alpine.js single-page dashboard (Tailwind CDN)
data/portfolio.db  SQLite (gitignored; /data volume on Railway)
```

Trade entry flows through `POST /api/transactions` (the holdings form also
writes a transactions row). Day-P&L math keys off the transactions ledger
using ET trading-day dates, so both entry paths hit the same numbers.

## Deploy

Copy `.env.example` and configure your own environment. In production, set
`PORTFOLIO_DB`, `TZ`, `ENABLE_CRON` and a strong `API_TOKEN`. Every data API
except `/api/health` requires `Authorization: Bearer <token>` when the token
is configured.

## Disclaimer

个人记录与研究用途,不构成投资建议,投资有风险。
