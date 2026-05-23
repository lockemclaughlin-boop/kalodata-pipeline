#!/usr/bin/env python3
"""
One-shot DOM dump for Kalodata's product page. Uses the existing authenticated
Chrome profile at .auth/chrome-profile/, navigates to /product, opens each
filter popover in turn, and writes the relevant HTML chunks + outer text to
outputs/_inspect/dom.json so Claude can map real selectors without guessing.

Usage:
    python scripts/dump_kalodata_dom.py

If a Cloudflare challenge or modal pops up, deal with it in the Chrome window
— the script waits up to 90s for the products table to be visible before it
starts probing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright

from src.scraper import selectors as S
from src.scraper.kalodata import (
    CHROME_PROFILE_DIR,
    _dismiss_blocking_modals,
    _open_context,
    _wait_for_products_page,
)


OUT_DIR = Path("outputs/_inspect")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _safe_html(page, selector: str, limit: int = 8000) -> list[str]:
    """Return outerHTML for up to 5 matches of `selector`, truncated."""
    out: list[str] = []
    try:
        loc = page.locator(selector)
        n = min(loc.count(), 5)
        for i in range(n):
            try:
                html = loc.nth(i).evaluate("el => el.outerHTML")
                out.append(html[:limit])
            except Exception as e:
                out.append(f"[err {e}]")
    except Exception as e:
        out.append(f"[locator err {e}]")
    return out


def _probe_region(page) -> dict:
    """
    Snapshot Kalodata's region picker. The pill is `<div id="region-dropdown">`
    at the top-right of the product page; the current country label sits
    inside `<div id="regionName">`. Both IDs verified via scripts/probe_region.py.
    """
    info: dict = {"label": "region", "popover": []}
    try:
        dropdown = page.locator("#region-dropdown").first
        if dropdown.count() == 0:
            info["error"] = "#region-dropdown not present"
            return info
        info["pill_outer_html"] = dropdown.evaluate("el => el.outerHTML")[:2500]
        try:
            info["current_region"] = page.locator("#regionName").first.inner_text().strip()
        except Exception:
            info["current_region"] = ""
        dropdown.click(timeout=4000)
        page.wait_for_timeout(700)
    except Exception as e:
        info["error"] = f"could not open region picker: {e}"
        return info

    for sel in (
        ".ant-popover:not(.ant-popover-hidden)",
        ".ant-dropdown:not(.ant-dropdown-hidden)",
        "div[role='dialog']",
        "div[role='listbox']",
    ):
        html = _safe_html(page, sel, limit=12000)
        if html:
            info["popover_selector"] = sel
            info["popover"] = html
            break
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass
    return info


def _probe_filter(page, label: str) -> dict:
    """Click a filter label, snapshot any popover that opens, close it."""
    info: dict = {"label": label, "label_outer_html": [], "popover": []}
    try:
        target = page.get_by_text(label, exact=True).first
        if target.count() == 0:
            info["error"] = "label not found by exact text"
            return info
        info["label_outer_html"] = [target.evaluate("el => el.outerHTML")[:2000]]
        target.click(timeout=4000)
        page.wait_for_timeout(700)
        # Common Ant popover containers — capture whichever's open.
        for sel in (
            ".ant-popover:not(.ant-popover-hidden)",
            ".ant-dropdown:not(.ant-dropdown-hidden)",
            ".ant-cascader-dropdown:not(.ant-cascader-dropdown-hidden)",
            "div[role='tooltip']",
            "div[role='dialog']",
        ):
            html = _safe_html(page, sel, limit=12000)
            if html:
                info["popover_selector"] = sel
                info["popover"] = html
                break
        # Close the popover so the next probe starts clean.
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception as e:
        info["error"] = str(e)
    return info


def main() -> None:
    if not CHROME_PROFILE_DIR.exists():
        sys.exit(f"No Chrome profile at {CHROME_PROFILE_DIR}. Run login_kalodata.py first.")

    with sync_playwright() as p:
        _, context = _open_context(p, Path(".auth/kalodata.json"), headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass

        if "kalodata.com/product" not in (page.url or ""):
            page.goto(S.PRODUCT_SEARCH_URL, wait_until="domcontentloaded")
        time.sleep(2)
        _dismiss_blocking_modals(page)
        _wait_for_products_page(page, headed=True, timeout_s=90)

        dump: dict = {
            "url": page.url,
            "title": page.title(),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 1. Filter rail outer HTML (everything left of the product table)
        dump["filter_rail"] = _safe_html(
            page,
            "aside, [class*='filter'][class*='sidebar'], [class*='filter-rail']",
            limit=18000,
        )

        # 2. Top bar (region picker is usually here)
        dump["top_bar"] = _safe_html(page, "header, .ant-layout-header", limit=12000)

        # 3. Pagination area at the bottom of the table
        dump["pagination_blocks"] = _safe_html(
            page,
            ".ant-pagination, [class*='pagination']",
            limit=8000,
        )
        # And the table footer / scroll container info
        dump["table_scroll_container_present"] = (
            page.locator(".ant-table-body").count() > 0
        )
        dump["row_count_initial"] = page.locator(S.PRODUCT_ROW).count()

        # 4. Probe each filter that opens a popover. Order matters — popovers
        # close on Escape between probes. Region first because it's a
        # different widget shape (country pill, not a labeled rail row).
        dump["probes"] = {}
        dump["probes"]["region"] = _probe_region(page)
        for label in (
            "Revenue($)",
            "Item Sold",
            "Revenue Source(Content)",
            "Revenue Growth Rate",
            "Avg. Unit Price($)",
            "Commission Rate",
            "Creator Number",
        ):
            dump["probes"][label] = _probe_filter(page, label)

        # 5. Try a window-scroll to see if more rows load (infinite scroll test).
        before = page.locator(S.PRODUCT_ROW).count()
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(2000)
        after = page.locator(S.PRODUCT_ROW).count()
        dump["window_scroll_test"] = {"before": before, "after": after}

        # 6. Same test on the ant-table-body
        if dump["table_scroll_container_present"]:
            tb_before = page.locator(S.PRODUCT_ROW).count()
            try:
                page.locator(".ant-table-body").first.evaluate(
                    "el => el.scrollBy(0, el.scrollHeight)"
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)
            tb_after = page.locator(S.PRODUCT_ROW).count()
            dump["table_body_scroll_test"] = {"before": tb_before, "after": tb_after}

        out_path = OUT_DIR / "dom.json"
        out_path.write_text(json.dumps(dump, indent=2))
        print(f"[inspect] wrote {out_path}")
        print(f"[inspect] row_count_initial={dump['row_count_initial']}")
        print(f"[inspect] window_scroll new rows: {dump['window_scroll_test']}")
        if "table_body_scroll_test" in dump:
            print(f"[inspect] table-body scroll new rows: {dump['table_body_scroll_test']}")

        context.close()


if __name__ == "__main__":
    main()
