#!/usr/bin/env bash
# Start the Parental Controls Web UI
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

# Run the app
echo "Starting Parental Controls Web UI on http://0.0.0.0:5000"
exec .venv/bin/python app.py
