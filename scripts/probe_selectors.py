#!/usr/bin/env python3
"""
Quick diagnostic: open the products page with our persistent Chrome profile,
try to dismiss any modal, then probe a list of candidate row/cell selectors
and print which ones actually match elements. Also saves a screenshot.

Usage:
    python scripts/probe_selectors.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from src.scraper import selectors as S
from src.scraper.kalodata import CHROME_PROFILE_DIR, _dismiss_blocking_modals


ROW_CANDIDATES = [
    "tr.ant-table-row",
    "tr[data-row-key]",
    "tbody tr",
    "div.ant-table-row",
    "div[role='row']",
    "tr[data-product-id]",
    "a[href*='/product/']",
    "div[class*='product-item']",
    "div[class*='ProductItem']",
    "div[class*='row'][class*='product']",
]

MODAL_CANDIDATES = [
    ".ant-modal-close",
    ".ant-modal-close-x",
    "button[aria-label='Close']",
    "div[role='dialog']",
    ".ant-modal",
]


def main() -> None:
    if not CHROME_PROFILE_DIR.exists():
        sys.exit(
            f"No session at {CHROME_PROFILE_DIR}. Run scripts/login_kalodata.py first."
        )

    out_dir = Path("outputs/_probe")
    out_dir.mkdir(parents=True, exist_ok=True)

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

        print(f"[probe] navigating to {S.PRODUCT_SEARCH_URL}", flush=True)
        page.goto(S.PRODUCT_SEARCH_URL, wait_until="domcontentloaded")
        time.sleep(3)

        print("[probe] before-dismiss screenshot", flush=True)
        page.screenshot(path=str(out_dir / "1-before-dismiss.png"), full_page=True)

        print("[probe] modal candidate matches BEFORE dismiss:", flush=True)
        for sel in MODAL_CANDIDATES:
            try:
                n = page.locator(sel).count()
                print(f"  {n:>3}  {sel}", flush=True)
            except Exception as e:
                print(f"  ERR  {sel}: {e}", flush=True)

        print("[probe] attempting auto-dismiss…", flush=True)
        _dismiss_blocking_modals(page)
        time.sleep(2)

        print("[probe] after-dismiss screenshot", flush=True)
        page.screenshot(path=str(out_dir / "2-after-dismiss.png"), full_page=True)

        print("[probe] row candidate matches AFTER dismiss:", flush=True)
        for sel in ROW_CANDIDATES:
            try:
                n = page.locator(sel).count()
                print(f"  {n:>4}  {sel}", flush=True)
            except Exception as e:
                print(f"  ERR  {sel}: {e}", flush=True)

        # Dump the body's outerHTML for the first ~50KB so we can eyeball it
        try:
            html = page.evaluate("() => document.body.outerHTML")
            (out_dir / "body.html").write_text(html[:200_000])
            print(f"[probe] wrote {out_dir / 'body.html'} ({len(html):,} chars)", flush=True)
        except Exception as e:
            print(f"[probe] body dump failed: {e}", flush=True)

        print("[probe] done. Screenshots in outputs/_probe/", flush=True)
        context.close()


if __name__ == "__main__":
    main()
