# Investment Assistant

A multi-market, multi-currency portfolio dashboard — with a daily briefing written by an AI that reads it every morning.

<img src="https://aidenyang.me/Assets/investment-poster.svg" width="720">

**Status:** in production (personal use)
**Case study:** [aidenyang.me/projects/investment.html](https://aidenyang.me/projects/investment.html)

> This public repository is a portfolio sample. It ships with fictional seed data only — no personal holdings, no production credentials.

## What it does

- One screen for holdings across US, Hong Kong, A-share, ASX, China funds and physical gold, in four currencies.
- A transactions ledger drives day-P&L (US trading day) and nightly snapshots build the equity curve.
- Prices refresh every 5 minutes from several sources with fallback.
- **API-first:** every number the page shows is also available at `/api/*`, so a scheduled Claude agent can read holdings and watchlist each weekday morning and write a pre-market briefing.

## Why I built it

I hold assets in five markets and none of the broker apps could show them together, let alone in one currency. I also wanted a second opinion every morning that reads *my* positions, not the news — that only works if the data is a clean API.

## How it works

```
Browser (Alpine.js)  ──►  Flask API  ──►  SQLite (Railway volume)
Claude agent (cron)  ──►  /api/holdings, /api/watchlist, /api/summary
Quote sources: Tencent qt · yfinance · akshare  ──►  APScheduler (every 5 min)
```

- **Backend** — Flask app factory + blueprints; SQLite with additive migrations.
- **Scheduler** — APScheduler in-process: prices every 5 min, daily P&L after the US close.
- **Timezones** — Sydney for display, US Eastern for the trading day.
- **Hosting** — Railway, SQLite on a persistent `/data` volume.

## Run it locally

```bash
pip3 install -r requirements.txt --break-system-packages   # akshare is large
python3 scripts/init_db.py
python3 scripts/seed.py        # fictional data
./run.sh                       # http://127.0.0.1:5180
```

## Layout

```
app/
  app_factory.py   Flask factory, blueprints, scheduler start
  db.py            SQLite helpers and migrations
  cron.py          APScheduler jobs
  api/             REST blueprints: holdings, transactions, watchlist, buckets, snapshots, prices, pnl, search
  services/        quotes, FX, ticker search, snapshots
scripts/           init_db · seed · compute_daily_pnl
templates/ static/ single-page dashboard (Alpine.js, Tailwind CDN)
```

## Deploy

Copy `.env.example`, set `PORTFOLIO_DB`, `TZ`, `ENABLE_CRON` and a strong `API_TOKEN`. Every data endpoint except `/api/health` requires `Authorization: Bearer <token>` once a token is configured.

## Disclaimer

Personal record-keeping and research only. Not investment advice.
