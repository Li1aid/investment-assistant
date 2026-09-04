#!/usr/bin/env bash
#
# Boots the local Investment Assistant dashboard.
# - Creates the SQLite database and seeds it on first run.
# - Loads .env if present (for ANTHROPIC_API_KEY etc.)
# - Starts Flask on http://127.0.0.1:5180
#
set -euo pipefail

cd "$(dirname "$0")"

PY=${PYTHON:-python3}

# Check deps are installed
if ! "$PY" -c "import flask, dotenv" 2>/dev/null; then
  echo "[!] Missing dependencies. Install with:"
  echo "    pip3 install -r requirements.txt --break-system-packages"
  echo "    (akshare is large; first install can take a few minutes)"
  exit 1
fi

# Always make sure schema exists; both scripts are idempotent
# (IF NOT EXISTS + INSERT OR IGNORE).
"$PY" scripts/init_db.py
"$PY" scripts/seed.py

# Friendly URL banner
PORT="${FLASK_PORT:-5180}"
echo ""
echo "  📊 Investment Assistant"
echo "  → http://127.0.0.1:${PORT}"
echo "  (Ctrl-C to stop)"
echo ""

# Try to open the browser (macOS / Linux)
( sleep 1 && (open "http://127.0.0.1:${PORT}" 2>/dev/null || xdg-open "http://127.0.0.1:${PORT}" 2>/dev/null) ) &

exec "$PY" wsgi.py
