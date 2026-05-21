#!/usr/bin/env python3
"""
First-run helper for the Flow video backend. Opens a real Chrome window
on the SHARED persistent profile under .auth/chrome-profile/ — the same
profile Kalodata uses. Sign into Google in the popped window so the
flow.py backend can drive Flow's UI without scripting Google's auth flow.

Cookies live in that profile dir from then on, so this only needs to run
once (or whenever the cookie expires).

Usage from project root:
    python scripts/login_flow.py

To wipe and start over: rm -rf .auth/chrome-profile/  (also wipes Kalodata)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.generators.flow import CHROME_PROFILE_DIR, ensure_logged_in


def main() -> None:
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[login-flow] profile dir: {CHROME_PROFILE_DIR.resolve()}")
    print(
        "[login-flow] Opening Chrome. Sign into Google in the window that opens,\n"
        "             land on labs.google/fx/tools/flow with the 'New project'\n"
        "             tile visible, then CLOSE THE WINDOW to persist the session."
    )
    ensure_logged_in(headed=True)
    print("[login-flow] window closed. Session persisted at", CHROME_PROFILE_DIR.resolve())


if __name__ == "__main__":
    main()
