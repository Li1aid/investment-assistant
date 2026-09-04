"""Flask application factory."""
from __future__ import annotations

import os
import time
from pathlib import Path

from flask import Flask, url_for as flask_url_for
from dotenv import load_dotenv

# Force libc to (re)read the TZ env var. Without this, Python's
# date.today() / datetime.now() on Railway return UTC even when
# TZ=Australia/Sydney is set in the env — because the libc cached the
# tz at process startup before the env var existed (or before tzdata
# was importable). tzset() is a no-op on platforms that already had
# TZ correct.
if os.environ.get("TZ"):
    time.tzset()


def _versioned_static(filename: str) -> str:
    """Append the file's mtime as ?v=… so browsers re-fetch when we ship
    new JS/CSS without the user having to force-reload."""
    static_folder = Path(__file__).resolve().parent.parent / "static"
    target = static_folder / filename
    if target.exists():
        return flask_url_for("static", filename=filename, v=int(target.stat().st_mtime))
    return flask_url_for("static", filename=filename)


def create_app() -> Flask:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")

    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    app.config["ROOT_DIR"] = root
    app.config["DB_PATH"] = root / "data" / "portfolio.db"

    # Gate portfolio-data API requests behind API_TOKEN (no-op when unset).
    from .auth import init_auth
    init_auth(app)

    from .api import (
        holdings,
        transactions,
        watchlist,
        prices,
        meta,
        pnl,
        search,
        buckets,
    )
    app.register_blueprint(meta.bp)
    app.register_blueprint(holdings.bp)
    app.register_blueprint(transactions.bp)
    app.register_blueprint(watchlist.bp)
    app.register_blueprint(prices.bp)
    app.register_blueprint(pnl.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(buckets.bp)

    from .views import bp as views_bp
    app.register_blueprint(views_bp)

    # Expose `static_v(filename)` in Jinja so templates can reference
    # /static/foo.js with a content-hash-ish ?v=mtime query string.
    app.jinja_env.globals["static_v"] = _versioned_static

    # Ensure DB schema exists (creates tables + runs additive migrations).
    # Important on Railway first boot where the volume is empty: without
    # this, the first request would hit "no such table" errors.
    from .db import init_schema
    init_schema()

    # Start the in-process scheduler (replaces launchd cron in cloud).
    # No-ops in tests / when DISABLE_CRON=1.
    from . import cron as _cron
    _cron.start(app)

    return app
