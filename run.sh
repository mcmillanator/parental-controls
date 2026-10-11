#!/usr/bin/env bash
# Start the Parental Controls Web UI (dev entrypoint).
# Binds 127.0.0.1 by default (localhost + SSH tunnel, no TLS in MVP).
# Pass --allow-lan to bind 0.0.0.0 explicitly (cookies travel in the clear).
set -euo pipefail

cd "$(dirname "$0")"

# Create venv if it doesn't exist
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Install dependencies
echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt

HOST=127.0.0.1
if [ "${1:-}" = "--allow-lan" ]; then
    HOST=0.0.0.0
    echo "WARNING: bound to all interfaces without TLS; session cookies travel in the clear (dev-only; production LAN requires install.sh --tls)."
fi

# Production WSGI server (waitress) serving the fail-closed wsgi entrypoint.
echo "Starting Parental Controls Web UI on http://${HOST}:5000"
exec .venv/bin/waitress-serve --host="$HOST" --port=5000 wsgi:application
