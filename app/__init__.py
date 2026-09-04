"""Package marker. The Flask app factory lives in app.app_factory so that
lightweight scripts (e.g. seed.py, refresh_prices.py) can import app.db
and app.services without pulling in Flask.
"""
