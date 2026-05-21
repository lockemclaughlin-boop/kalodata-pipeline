#!/usr/bin/env bash
# Mac launcher — sign in to Kalodata and Google Flow.
# Double-click in Finder. Run this AFTER the first start.command launch
# (which builds the environment). You only need to do this once, or again
# whenever a session expires.

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
    echo "The app isn't built yet."
    echo "Double-click start.command first, wait for the dashboard, then run this."
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi

echo "================================================================"
echo " Step 1 of 2 — Sign in to Kalodata"
echo "================================================================"
echo "A Chrome window will open. Sign into your Kalodata account."
echo "When you can see the products page, CLOSE THE WINDOW to continue."
echo ""
.venv/bin/python scripts/login_kalodata.py

echo ""
echo "================================================================"
echo " Step 2 of 2 — Sign in to Google (Flow)"
echo "================================================================"
echo "A Chrome window will open. Sign into the Google account that has"
echo "your AI subscription. When you see labs.google/fx/tools/flow with"
echo "the 'New project' tile, CLOSE THE WINDOW to finish."
echo ""
.venv/bin/python scripts/login_flow.py

echo ""
echo "Done — both sessions are saved. You can close this window."
read -n 1 -s -r -p "Press any key to close..."
