#!/usr/bin/env bash
# Mac launcher — double-click in Finder to start the app.
# First run sets up the venv; subsequent runs just launch Streamlit.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "First-time setup..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m playwright install chromium
fi

.venv/bin/streamlit run src/app.py --server.port 8765
