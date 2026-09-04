"""WSGI entrypoint.

Local dev:            python wsgi.py            (Flask dev server)
Production (Railway): gunicorn ... wsgi:app      (Procfile / railway.toml)
"""
from app.app_factory import create_app

app = create_app()


if __name__ == "__main__":
    import os
    # Railway / Heroku-style platforms set $PORT and expect 0.0.0.0 bind.
    # Local launchd uses FLASK_PORT (5180) and 127.0.0.1.
    port = int(os.environ.get("PORT") or os.environ.get("FLASK_PORT", "5180"))
    default_host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    app.run(
        host=os.environ.get("FLASK_HOST", default_host),
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
