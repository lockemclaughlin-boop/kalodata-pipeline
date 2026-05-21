#!/usr/bin/env python3
"""
First-run helper. Opens a real Chrome window with a persistent profile under
.auth/chrome-profile/, lets you log into Kalodata manually (handling 2FA,
Cloudflare challenges, captchas, whatever), and exits when you close the
window. Your session — cookies, cache, cf_clearance — lives in the profile
directory and is reused by every subsequent run.

Usage from the project root:
    python scripts/login_kalodata.py

You only need to re-run this when the cookie expires or the profile gets
corrupted. To wipe and start over: rm -rf .auth/chrome-profile/
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from src.scraper import selectors as S
from src.scraper.kalodata import CHROME_PROFILE_DIR


def main() -> None:
    load_dotenv()
    email = os.environ.get("KALODATA_EMAIL") or None

    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[login] Chrome profile dir: {CHROME_PROFILE_DIR.resolve()}")
    if email:
        print(f"[login] expected account: {email}")

    print(
        "[login] Opening Chrome. Sign in to Kalodata in the window that opens.\n"
        "        If Cloudflare shows a 'Verify you're human' check, click it.\n"
        "        Confirm you can see the products page, then CLOSE THE WINDOW\n"
        "        to finish — that saves the session into the profile dir."
    )

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(CHROME_PROFILE_DIR.resolve()),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.new_page()
        page.goto(S.PRODUCT_SEARCH_URL)

        # Block until the user closes the tab (or the whole window).
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass

    print("[login] window closed. Session persisted at", CHROME_PROFILE_DIR.resolve())


if __name__ == "__main__":
    main()
