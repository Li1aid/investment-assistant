"""In-process cron scheduler.

When running on Railway (single gunicorn worker) launchd is gone, so
we schedule jobs inside the web process via APScheduler. Each job
runs inside `app.app_context()` and any uncaught exception is logged
to stderr without killing the scheduler.

Schedules:
  - prices    every 5 min   (24/7, lets premarket / Asian hours feed
                            through)
  - pnl       Mon–Fri 16:15 ET (after US close) — writes pnl_date=ET
                                today; Sydney Tue–Sat ~06:30 AEST.

Gunicorn runs us with `-w 1 --threads 4`, so there's only ONE
scheduler instance per app. If we ever scale to multiple workers,
this needs a distributed lock or a dedicated cron service.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone

_SCHEDULER = None


def _log(msg: str) -> None:
    print(f"[cron {datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}",
          flush=True, file=sys.stderr)


def _wrap(app, fn, label: str):
    """Wrap a job so exceptions log instead of killing the scheduler."""
    def runner():
        _log(f"{label}: starting")
        try:
            with app.app_context():
                fn()
            _log(f"{label}: done")
        except Exception as e:
            _log(f"{label}: FAILED — {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stderr)
    return runner


def _job_refresh_prices() -> None:
    from .services.prices import refresh_all
    refresh_all()


def _job_compute_pnl() -> None:
    from scripts.compute_daily_pnl import main as compute_main
    rc = compute_main()
    if rc != 0:
        raise RuntimeError(f"compute_daily_pnl exited with {rc}")


def start(app) -> None:
    """Idempotently start the in-process scheduler.

    OPT-IN: only starts when `ENABLE_CRON=1`. Local dev where you want
    to avoid double-running with launchd should leave it unset.
    """
    global _SCHEDULER
    if _SCHEDULER is not None:
        return

    if os.environ.get("ENABLE_CRON") != "1":
        _log("ENABLE_CRON not set — scheduler dormant (set ENABLE_CRON=1 in prod)")
        return

    # Flask reloader: parent spawns a child that re-runs the module.
    # WERKZEUG_RUN_MAIN=true is set in the actual serving child.
    if (os.environ.get("FLASK_DEBUG") == "1"
            and os.environ.get("WERKZEUG_RUN_MAIN") != "true"):
        _log("reloader parent — skipping scheduler")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = os.environ.get("TZ") or "Australia/Sydney"
    sched = BackgroundScheduler(timezone=tz, daemon=True)

    sched.add_job(_wrap(app, _job_refresh_prices, "prices"),
                  IntervalTrigger(minutes=5), id="prices",
                  max_instances=1, coalesce=True)

    # P&L compute fires right after US market close (16:00 ET, Mon–Fri).
    # pnl_date is the ET date so each row corresponds to a US trading day.
    sched.add_job(_wrap(app, _job_compute_pnl, "pnl"),
                  CronTrigger(day_of_week="mon-fri", hour=16, minute=15,
                              timezone="America/New_York"),
                  id="pnl", max_instances=1, coalesce=True)

    sched.start()
    _SCHEDULER = sched
    _log(f"scheduler started (tz={tz}) — 2 jobs registered (prices, pnl)")
