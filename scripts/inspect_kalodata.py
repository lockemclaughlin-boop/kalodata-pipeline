#!/usr/bin/env python3
"""
Interactive selector-discovery helper. Opens Kalodata in headed Chromium with
the Playwright Inspector attached so you can:

  - Click on any element to get a suggested locator
  - Test selectors live in the Inspector REPL
  - Update src/scraper/selectors.py with what works

Usage from the project root:
    python scripts/inspect_kalodata.py

The script reuses your saved session at .auth/kalodata.json. Run
scripts/login_kalodata.py first if you don't have one.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from src.scraper import selectors as S
from src.scraper.kalodata import CHROME_PROFILE_DIR


def main() -> None:
    if not CHROME_PROFILE_DIR.exists():
        sys.exit(
            f"No session at {CHROME_PROFILE_DIR}. Run scripts/login_kalodata.py first."
        )

    # PWDEBUG=1 makes Playwright open the Inspector. Set before launching.
    os.environ.setdefault("PWDEBUG", "1")

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
        print(
            "Inspector is open. Use the 'Pick locator' button in the Inspector\n"
            "to click elements and copy locators into src/scraper/selectors.py.\n"
            "Close the browser window when done."
        )
        page.pause()
        context.close()


if __name__ == "__main__":
    main()
