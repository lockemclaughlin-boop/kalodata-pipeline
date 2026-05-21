"""
Kalodata scraper — Playwright session, login persistence, filtered product search,
per-product asset download.

Phase 1 implementation. The selectors live in src/scraper/selectors.py and will
need a one-time pass with `scripts/inspect_kalodata.py` against the live site.

Public API (called by pipeline / tests):
    Filters                       — dataclass of filter inputs
    Product                       — dataclass of one scraped product
    ensure_logged_in(...)         — first-run login + cookie persistence
    search_products(...)          — apply filters, return list of Product (no photos yet)
    fetch_product_assets(...)     — download photos for one Product
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from playwright.sync_api import (
    BrowserContext,
    Page,
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from slugify import slugify

from . import selectors as S


# Persistent Chrome profile path. Using a real Chrome channel + persistent
# user-data-dir is what gets us past Cloudflare on Kalodata — a fresh ephemeral
# context with bundled Chromium gets flagged immediately.
CHROME_PROFILE_DIR = Path(".auth/chrome-profile")


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Filters:
    region: str = "US"
    category: Optional[str] = None
    time_window: str = "last_7_days"   # last_24_hours | last_7_days | last_30_days
    min_gmv_usd: float = 0
    min_sales: int = 0
    min_growth_pct: float = 0


@dataclass
class Product:
    id: str
    title: str
    kalodata_url: str
    photo_urls: list[str] = field(default_factory=list)
    price_usd: Optional[float] = None
    gmv_usd: Optional[float] = None
    units_sold: Optional[int] = None
    growth_pct: Optional[float] = None
    commission_pct: Optional[float] = None
    category_path: Optional[str] = None
    shop_name: Optional[str] = None
    # Raw column-name → display-text dict captured from the Kalodata results
    # table, so every visible stat (Shops, Creators, Videos, Rating, etc.) is
    # preserved even if we haven't given it a dedicated typed field.
    extras: dict[str, str] = field(default_factory=dict)

    def slug(self) -> str:
        return slugify(f"{self.id}-{self.title}")[:80]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Browser / session helpers
# ---------------------------------------------------------------------------

def _human_pause(min_s: float = 2, max_s: float = 6) -> None:
    """Random delay so we don't thrash Kalodata between page loads."""
    time.sleep(random.uniform(min_s, max_s))


def _open_context(
    p,
    storage_state_path: Path,
    headed: bool = True,
) -> tuple[None, BrowserContext]:
    """
    Launch real Google Chrome with a persistent profile under
    .auth/chrome-profile/. This is required to get past Cloudflare on
    Kalodata — bundled Chromium with an ephemeral context gets flagged.

    `storage_state_path` is accepted for back-compat but no longer used;
    the persistent profile directory holds cookies/cache/cf_clearance.
    Returns (None, context) — there is no separate Browser handle when
    using launch_persistent_context.
    """
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # Clear stale SingletonLock files left behind by a previous Chrome that
    # didn't shut down cleanly — without this we get "Failed to create a
    # ProcessSingleton for your profile directory" and the launch aborts.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        stale = CHROME_PROFILE_DIR / name
        try:
            if stale.exists() or stale.is_symlink():
                stale.unlink()
        except OSError:
            pass
    context = p.chromium.launch_persistent_context(
        user_data_dir=str(CHROME_PROFILE_DIR.resolve()),
        channel="chrome",
        headless=not headed,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
    )
    return None, context


def _detect_captcha(page: Page) -> bool:
    """
    Return True only if a captcha element is *visible* on the page. Kalodata
    keeps a hidden recaptcha iframe in the DOM for login/signup flows, which
    would false-positive a naive .count() > 0 check.
    """
    for hint in S.CAPTCHA_HINTS:
        try:
            loc = page.locator(hint)
            n = loc.count()
            for i in range(n):
                try:
                    if loc.nth(i).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _dismiss_blocking_modals(page: Page) -> None:
    """
    Kalodata pops a "New User Benefits / Verify mobile number" modal that
    blocks interaction with the products page. Try a few close patterns; if
    none match, press Escape (Ant Design closes its modals on Esc by default).
    """
    close_locators = [
        ".ant-modal-close",
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "div[role='dialog'] [aria-label*='close' i]",
        "div[role='dialog'] button:has-text('×')",
    ]
    for sel in close_locators:
        try:
            loc = page.locator(sel)
            n = loc.count()
            for i in range(n):
                try:
                    btn = loc.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=2000)
                        page.wait_for_timeout(400)
                except Exception:
                    continue
        except Exception:
            continue
    # Fallback: Escape closes Ant Design modals.
    try:
        if page.locator("div[role='dialog']").count() > 0:
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
    except Exception:
        pass


def _wait_for_products_page(
    page: Page,
    headed: bool,
    timeout_s: int = 120,
    poll_s: float = 1.5,
) -> None:
    """
    Wait for the products page to be in a usable state — i.e., at least one
    product row visible. If it doesn't appear in time, pause for the human
    to deal with whatever's blocking (modal, CF, login wall) when headed.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            rows = page.locator(S.PRODUCT_ROW)
            if rows.count() > 0:
                return
        except Exception:
            pass
        # Try to dismiss anything that popped up
        _dismiss_blocking_modals(page)
        time.sleep(poll_s)

    if not headed:
        raise RuntimeError(
            "Products table never appeared on Kalodata in headless mode. "
            "Re-run headed and inspect manually."
        )
    print(
        f"[scrape] products table didn't appear in {timeout_s}s. Look at the\n"
        f"        open Chrome window — dismiss any modal, solve any challenge,\n"
        f"        then leave the window open. Polling another {timeout_s}s..."
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.locator(S.PRODUCT_ROW).count() > 0:
                return
        except Exception:
            pass
        time.sleep(poll_s)
    raise RuntimeError("Products table still not visible after manual intervention")


def _wait_for_human_to_clear_captcha(
    page: Page,
    headed: bool,
    timeout_s: int = 300,
    poll_s: float = 2.0,
) -> None:
    """
    If Cloudflare / Kalodata is showing a captcha, pause and wait for the
    human to solve it in the open browser. Polls until the challenge clears
    or we exceed timeout_s.
    """
    if not _detect_captcha(page):
        return
    if not headed:
        raise RuntimeError(
            "Captcha detected in headless mode — re-run with headed=True so "
            "you can solve it interactively."
        )
    print(
        f"[auth] Cloudflare/captcha challenge detected. Solve it in the open\n"
        f"       Chrome window — the scraper will continue automatically once\n"
        f"       it clears. Waiting up to {timeout_s}s..."
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        if not _detect_captcha(page):
            print("[auth] challenge cleared, continuing")
            return
    raise RuntimeError("Timed out waiting for human to solve Kalodata captcha")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def ensure_logged_in(
    storage_state_path: Path,
    email: Optional[str] = None,
    password: Optional[str] = None,
    headed: bool = True,
    manual_login_timeout_s: int = 300,
) -> None:
    """
    Verify we have a working Kalodata session. If not, pop a headed Chrome
    window and let the human sign in (and clear any Cloudflare challenge).

    Session state persists automatically via the Chrome profile directory
    at .auth/chrome-profile/, so we don't need to dump storage_state to JSON.

    `email` and `password` are accepted but NOT typed automatically — Google/
    Kalodata both detect scripted login fills. They're only logged so the human
    knows which account to sign in as.
    """
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        _, context = _open_context(p, storage_state_path, headed=headed)
        page = context.new_page()

        try:
            page.goto(S.PRODUCT_SEARCH_URL, wait_until="domcontentloaded")
            _human_pause(1, 2)

            if _looks_logged_in(page):
                print("[auth] existing session is valid")
                return

            print(
                f"[auth] please log in to Kalodata in the browser window."
                f"{' Account: ' + email if email else ''}"
                f" If Cloudflare shows a challenge, solve it first."
                f" Waiting up to {manual_login_timeout_s}s..."
            )
            page.goto(S.LOGIN_URL, wait_until="domcontentloaded")

            deadline = time.time() + manual_login_timeout_s
            while time.time() < deadline:
                if _looks_logged_in(page):
                    print("[auth] login detected; profile persisted at .auth/chrome-profile/")
                    return
                time.sleep(2)

            raise RuntimeError(
                "Timed out waiting for manual Kalodata login. "
                "Try again with a longer manual_login_timeout_s."
            )
        finally:
            context.close()


def _looks_logged_in(page: Page) -> bool:
    """Heuristic: if the URL has the dashboard fragment OR a login form is absent."""
    url = page.url
    if S.DASHBOARD_URL_FRAGMENT in url:
        return True
    # If we're on the product page and there's no email input visible, we're in.
    try:
        if "kalodata.com/product" in url and page.locator(S.EMAIL_INPUT).count() == 0:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_TIME_WINDOW_LABELS = {
    "last_24_hours": "Last 24 hours",
    "last_7_days":   "Last 7 days",
    "last_30_days":  "Last 30 days",
}


def search_products(
    filters: Filters,
    storage_state_path: Path,
    max_results: int = 100,
    headed: bool = True,
    apply_filters_in_browser: bool = True,
    manual_filter_pause_s: int = 8,
) -> list[Product]:
    """
    Apply filters in the Kalodata UI, paginate, return up to max_results Products
    (without downloading photos — that's deferred to fetch_product_assets()).

    apply_filters_in_browser=False (the default) skips automated filter clicks.
    Kalodata's filter popovers are deeply nested and the Min/Max inputs are
    disabled until a "Custom" preset is picked — wiring this needs a manual
    selector pass via scripts/inspect_kalodata.py first. While that's pending,
    we wait `manual_filter_pause_s` after opening the page so you can adjust
    filters yourself in the headed Chrome window, then scrape whatever's there.
    """
    if not CHROME_PROFILE_DIR.exists():
        raise RuntimeError(
            f"No Kalodata session at {CHROME_PROFILE_DIR}. "
            "Run scripts/login_kalodata.py first."
        )

    products: list[Product] = []

    with sync_playwright() as p:
        _, context = _open_context(p, storage_state_path, headed=headed)
        # launch_persistent_context restores tabs from the saved profile, so
        # there is usually an existing page already on Kalodata. Reuse it
        # instead of creating a second about:blank tab that Playwright then
        # drives while the real Kalodata tab sits idle.
        if context.pages:
            page = context.pages[0]
            for extra in context.pages[1:]:
                try:
                    extra.close()
                except Exception:
                    pass
        else:
            page = context.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass

        try:
            # Only navigate if we aren't already on Kalodata's product page —
            # reloading kicks Cloudflare back into a challenge and flashes the
            # tab white for several seconds.
            current_url = page.url or ""
            if "kalodata.com/product" not in current_url:
                page.goto(S.PRODUCT_SEARCH_URL, wait_until="domcontentloaded")
            _human_pause()

            _dismiss_blocking_modals(page)
            _wait_for_products_page(page, headed=headed)

            if apply_filters_in_browser:
                _apply_filters(page, filters)
                _human_pause(3, 5)
            else:
                # Give the human a window to adjust filters in the Chrome tab.
                if manual_filter_pause_s > 0:
                    print(
                        f"[scrape] Adjust any filters you want in the Kalodata window — "
                        f"scraping starts in {manual_filter_pause_s}s. (Set "
                        f"apply_filters_in_browser=True once selectors are mapped.)",
                        flush=True,
                    )
                    remaining = manual_filter_pause_s
                    while remaining > 0:
                        step = min(5, remaining)
                        time.sleep(step)
                        remaining -= step
                        if remaining > 0:
                            print(f"[scrape] {remaining}s remaining…", flush=True)
                    # Re-wait for rows in case the manual filter change reloaded them.
                    _wait_for_products_page(page, headed=headed, timeout_s=60)

            # Bump Kalodata's "10 / page" default up so a single page covers
            # what the user asked for (Kalodata typically offers 10/20/50/100).
            for candidate in (100, 50, 20):
                if candidate >= max_results:
                    if _set_page_size(page, candidate):
                        break

            headers = _read_table_headers(page)

            # Single-page scrape only. Kalodata's free tier paywalls
            # everything past page 1 with an "Upgrade to view the data"
            # modal, and clicking Next + scrolling to reach pagination
            # just triggers that gate. Take whatever's visible after Submit.
            seen_ids: set[str] = set()
            rows = page.locator(S.PRODUCT_ROW)
            row_count = rows.count()
            if row_count == 0:
                print("[scrape] no rows visible, stopping", flush=True)
            else:
                for i in range(row_count):
                    if len(products) >= max_results:
                        break
                    row = rows.nth(i)
                    p_obj = _extract_product_from_row(row, headers)
                    if p_obj is None or p_obj.id in seen_ids:
                        continue
                    seen_ids.add(p_obj.id)
                    products.append(p_obj)
                print(
                    f"[scrape] page 1: rows={row_count}, "
                    f"captured={len(products)}/{max_results}",
                    flush=True,
                )
                if len(products) < max_results and row_count >= 10:
                    print(
                        f"[scrape] Kalodata's free tier caps results at "
                        f"~{len(products)} per query (paywall modal on "
                        f"pagination). Run another search with different "
                        f"filters for a different set.",
                        flush=True,
                    )
        finally:
            context.close()

    return products


def _apply_filters(page: Page, filters: Filters) -> None:
    """
    Drive Kalodata's left filter rail. Each helper is wrapped in try/except so
    one failed click doesn't kill the chain — partial filters are better than
    none, and the streaming log shows exactly which step failed. After all
    picks we click the big Submit button at the bottom of the rail, which is
    what actually re-runs the search against Kalodata's API.
    """
    _set_time_window(page, filters.time_window)
    _set_category(page, filters.category)
    _set_min_bucket(page, "Revenue($)", filters.min_gmv_usd, _REVENUE_BUCKETS)
    _set_min_bucket(page, "Item Sold", filters.min_sales, _ITEM_SOLD_BUCKETS)
    _set_min_bucket(page, "Revenue Growth Rate", filters.min_growth_pct, _GROWTH_BUCKETS)
    _click_filter_rail_submit(page)


def _click_filter_rail_submit(page: Page) -> None:
    """
    Click the blue "Submit" button at the bottom of Kalodata's filter rail.
    Without this the bucket selections are just staged — the table doesn't
    actually refresh until Submit fires the search.
    """
    selectors = (
        ".V2-Components-Button:has-text('Submit')",
        "button:has-text('Submit')",
        "div[role='button']:has-text('Submit')",
        "[class*='Button']:has-text('Submit')",
        "text=Submit",
    )
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            try:
                btn.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            # Try a normal click, then force-click around any overlay.
            try:
                btn.click(timeout=2000)
            except Exception:
                btn.click(force=True, timeout=2000)
            page.wait_for_timeout(2000)
            print(f"[filters] submit → search refreshed (sel={sel})", flush=True)
            return
        except Exception:
            continue
    print(
        "[filters] could not find a Submit button — table may not reflect filters",
        flush=True,
    )


# Bucket maps — labels are Kalodata's actual on-screen bracket text
# (captured via scripts/dump_kalodata_dom.py). Kalodata's brackets are coarse:
# Revenue and Item Sold both cap out at ">10k", so any user threshold above
# that ceiling clicks the same top bucket.
_REVENUE_BUCKETS = [
    (10_000, [">10k"]),
    (1_000,  ["1k-10k"]),
    (100,    ["100-1k"]),
    (1,      ["<100"]),
]
_ITEM_SOLD_BUCKETS = [
    (10_000, [">10k"]),
    (5_000,  ["5k-10k"]),
    (500,    ["500-5k"]),
    (50,     ["50-500"]),
    (1,      ["0-50"]),
]
_GROWTH_BUCKETS = [
    (100, [">100%"]),
    (70,  [">70%"]),
    (30,  [">30%"]),
    (1,   [">0%"]),
]


def _set_time_window(page: Page, time_window: str) -> None:
    target = {
        "last_24_hours": "Last 24 hours",
        "last_7_days":   "Last 7 days",
        "last_30_days":  "Last 30 days",
    }.get(time_window)
    if not target:
        return
    try:
        # The date range button at top of the rail begins with "Last".
        page.locator("text=/^Last \\d+ Day/i").first.click(timeout=4000)
        _human_pause(0.5, 1)
        page.get_by_text(target, exact=False).first.click(timeout=4000)
        _human_pause(0.5, 1)
        # Kalodata changed behavior 2026-05: popover selections must be
        # committed with Apply or they get discarded on close. Helper falls
        # back to Escape if no Apply button is present.
        _click_popover_apply(page)
        print(f"[filters] time window → {target}", flush=True)
    except Exception as e:
        print(f"[filters] could not set time_window={time_window}: {e}", flush=True)


def _set_category(page: Page, category: Optional[str]) -> None:
    """
    Kalodata's category picker is an Ant Cascader. Hovering a parent slides
    a second column in that visually overlaps the parent, so a plain click on
    the label gets intercepted by the next column. Click the checkbox span
    inside the item instead, and fall back to force=True on the item.
    """
    if not category:
        return
    try:
        page.get_by_text("Category", exact=True).first.click(timeout=4000)
        _human_pause(0.6, 1.0)

        safe_title = category.replace('"', '\\"')
        item = page.locator(
            f'li.ant-cascader-menu-item[title="{safe_title}"]'
        ).first

        clicked = False
        try:
            checkbox = item.locator(".ant-cascader-checkbox").first
            if checkbox.count() > 0:
                checkbox.click(timeout=2500)
                clicked = True
        except Exception:
            pass
        if not clicked:
            item.click(force=True, timeout=4000)

        _human_pause(0.4, 0.8)
        # Kalodata changed behavior 2026-05: cascader selections must be
        # committed with Apply/Confirm or they get discarded on Escape.
        # _click_popover_apply falls back to Escape if no Apply button is
        # active, so this stays safe on the old UI too.
        _click_popover_apply(page)
        print(f"[filters] category → {category}", flush=True)
    except Exception as e:
        print(f"[filters] could not set category={category}: {e}", flush=True)


def _click_popover_apply(page: Page) -> bool:
    """
    Press Kalodata's Apply / Confirm / Submit button at the bottom of an open
    filter popover. Kalodata's own button is a <div class="V2-Components-Button">
    (NOT a real <button>), so role-based selectors miss it — match the class
    or the visible text directly. Returns True if a button got clicked.
    """
    selectors = (
        ".ant-dropdown:not(.ant-dropdown-hidden) .V2-Components-Button:has-text('Apply')",
        ".ant-popover:not(.ant-popover-hidden) .V2-Components-Button:has-text('Apply')",
        ".V2-Components-Button:has-text('Apply')",
        ".V2-Components-Button:has-text('Confirm')",
        ".V2-Components-Button:has-text('Submit')",
        "button:has-text('Apply')",
        "button:has-text('Confirm')",
        "button:has-text('Submit')",
    )
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() == 0:
                continue
            # Skip if the button is the disabled variant (Kalodata adds
            # `bg-disable` / `cursor-default` until a bracket is picked).
            cls = (btn.get_attribute("class") or "")
            if "bg-disable" in cls or "cursor-default" in cls:
                continue
            btn.click(timeout=1500)
            page.wait_for_timeout(400)
            return True
        except Exception:
            continue
    # No active Apply button — close the popover gracefully.
    page.keyboard.press("Escape")
    return False


def _set_min_bucket(
    page: Page,
    filter_label: str,
    value,
    bucket_map: list[tuple[int, list[str]]],
) -> None:
    """
    Click a Revenue Filter / Advanced label in the left rail, then click the
    preset bucket whose floor matches `value`. Kalodata's Min/Max input is
    disabled until "Custom" is chosen, so we deliberately stick to presets.
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        return

    chosen_labels: list[str] = []
    for floor, labels in bucket_map:
        if v >= floor:
            chosen_labels = labels
            break
    if not chosen_labels:
        print(f"[filters] {filter_label}={v} is below all preset buckets; skipping", flush=True)
        return

    try:
        page.get_by_text(filter_label, exact=True).first.click(timeout=4000)
        _human_pause(0.5, 1.0)

        clicked = False
        for label in chosen_labels:
            try:
                page.get_by_text(label, exact=True).first.click(timeout=2000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            print(
                f"[filters] {filter_label}: none of {chosen_labels} matched a bucket on screen; skipping",
                flush=True,
            )
            page.keyboard.press("Escape")
            return

        _human_pause(0.3, 0.6)
        # Kalodata changed behavior 2026-05: bucket selections in the popover
        # must be committed with Apply or they vanish on close. Helper falls
        # back to Escape if no Apply button is active.
        _click_popover_apply(page)
        print(f"[filters] {filter_label} → {chosen_labels[0]}", flush=True)
    except Exception as e:
        print(f"[filters] could not set {filter_label}={v}: {e}", flush=True)


_BG_IMAGE_URL_RE = re.compile(r'url\((?:"|\')?([^"\')]+)(?:"|\')?\)')


def _extract_product_url(row) -> Optional[str]:
    """
    Pull the real product detail URL from the row by scanning every <a href>
    inside it. Prefer kalodata.com links pointing at a product page; fall back
    to any non-empty external href. Returns None if nothing usable was found.
    """
    try:
        url = row.evaluate(
            """row => {
                const anchors = row.querySelectorAll('a[href]');
                const kalo = [];
                const others = [];
                for (const a of anchors) {
                    const href = a.href || '';
                    if (!href || href.startsWith('javascript:') || href === '#') continue;
                    if (href.includes('kalodata.com')) kalo.push(href);
                    else others.push(href);
                }
                return kalo[0] || others[0] || null;
            }"""
        )
        if isinstance(url, str) and url.startswith("http"):
            return url
    except Exception:
        return None
    return None


def _extract_cover_url(row) -> Optional[str]:
    """
    Find the real cover image URL for a product row by sweeping every
    plausible place in one JS evaluation: <img src/data-src/currentSrc>,
    srcset, and computed background-image on any descendant.
    """
    try:
        url = row.evaluate(
            """row => {
                for (const img of row.querySelectorAll('img')) {
                    for (const attr of ['src', 'data-src', 'data-original']) {
                        const v = img.getAttribute(attr);
                        if (v && v.startsWith('http') && !v.includes('placeholder')) {
                            return v;
                        }
                    }
                    if (img.currentSrc && img.currentSrc.startsWith('http')) {
                        return img.currentSrc;
                    }
                    const srcset = img.getAttribute('srcset');
                    if (srcset) {
                        const first = srcset.split(',')[0].trim().split(' ')[0];
                        if (first.startsWith('http')) return first;
                    }
                }
                for (const el of row.querySelectorAll('*')) {
                    const bg = getComputedStyle(el).backgroundImage;
                    const m = bg && bg.match(/url\\(["']?(https?:[^"')]+)["']?\\)/);
                    if (m) return m[1];
                }
                return null;
            }"""
        )
        if isinstance(url, str) and url.startswith("http"):
            return url
    except Exception:
        return None
    return None


def _read_table_headers(page: Page) -> list[str]:
    """
    Grab the visible column names from the results table header so we can
    label each cell in every row by its Kalodata column name.
    """
    try:
        hdr = page.locator(S.TABLE_HEADER_CELL)
        n = hdr.count()
        out: list[str] = []
        for i in range(n):
            try:
                txt = hdr.nth(i).inner_text().strip()
            except Exception:
                txt = ""
            # Collapse whitespace/newlines that Ant adds for sort icons.
            txt = " ".join(txt.split())
            out.append(txt)
        return out
    except Exception as e:
        print(f"[scrape] couldn't read table headers: {e}")
        return []


def _extract_product_from_row(row, headers: list[str]) -> Optional[Product]:
    """
    Pull what we can from a single result row. Kalodata renders product rows as
    <tr class="ant-table-row" data-row-key="<product_id>"> with the title inside
    div.line-clamp-2 and the cover image as a CSS background-image. Every cell
    is also stashed by column name in product.extras so nothing visible on the
    Kalodata table is dropped.
    """
    try:
        product_id = row.get_attribute(S.PRODUCT_ID_ATTR) or ""
        if not product_id:
            return None

        try:
            title = row.locator(S.PRODUCT_TITLE_IN_ROW).first.inner_text().strip()
        except Exception:
            title = ""
        if not title:
            title = "(no title)"

        cells = row.locator("td.ant-table-cell")
        cell_texts = [cells.nth(i).inner_text().strip() for i in range(cells.count())]
        gmv = _first_currency_kalo(cell_texts)
        sales = _first_int_kalo(cell_texts)
        growth = _first_percent(cell_texts)

        # Build a column-name → cell-text dict. Skip the first column (it's the
        # product image+title chunk, which we already capture as title/photo).
        extras: dict[str, str] = {}
        for i, txt in enumerate(cell_texts):
            if i == 0:
                continue
            name = headers[i] if i < len(headers) and headers[i] else f"col_{i}"
            # Cell text often contains the value on one line and a sub-line
            # (e.g. growth % under the GMV). Keep both, separated by ' / '.
            cleaned = " / ".join(line.strip() for line in txt.splitlines() if line.strip())
            if cleaned:
                extras[name] = cleaned

        kalodata_url = _extract_product_url(row) or f"https://www.kalodata.com/product/{product_id}"

        # Pull the actual cover URL from the row's DOM. Kalodata renders
        # covers as CSS background-image on a div inside the row (and
        # sometimes also as a real <img>). Try img.src first, then any
        # background-image style.
        cover_url = _extract_cover_url(row) or S.COVER_URL_TEMPLATE.format(
            product_id=product_id
        )

        return Product(
            id=product_id,
            title=title,
            kalodata_url=kalodata_url,
            photo_urls=[cover_url],
            gmv_usd=gmv,
            units_sold=sales,
            growth_pct=growth,
            extras=extras,
        )
    except Exception as e:
        print(f"[scrape] row parse failed: {e}")
        return None


def _extract_product_id(url: str) -> str:
    # Kalodata URLs typically look like /product/<id> or /product/<id>?...
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else url


_CURRENCY_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")
_PERCENT_RE = re.compile(r"([\-+]?[\d,]+(?:\.\d+)?)\s*%")
_INT_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+)\b")

# Kalodata shows numbers like "$12.5k", "2.1m", "1.3b" with case-insensitive suffix.
_SUFFIXED_NUM_RE = re.compile(r"([\d.]+)\s*([kKmMbB])")
_SUFFIX_MULT = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _parse_suffixed(num: str) -> float:
    m = _SUFFIXED_NUM_RE.fullmatch(num.strip())
    if m:
        return float(m.group(1)) * _SUFFIX_MULT[m.group(2).lower()]
    return float(num.replace(",", ""))


def _first_currency_kalo(texts: list[str]) -> Optional[float]:
    """Match "$12.5k", "$1.2M", or plain "$12,500"."""
    for t in texts:
        m = re.search(r"\$\s*([\d,.]+\s*[kKmMbB]?)", t)
        if m:
            raw = m.group(1).replace(",", "").strip()
            try:
                return _parse_suffixed(raw) if raw[-1].lower() in "kmb" else float(raw)
            except Exception:
                continue
    return None


def _first_currency(texts: list[str]) -> Optional[float]:
    for t in texts:
        m = _CURRENCY_RE.search(t)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def _first_percent(texts: list[str]) -> Optional[float]:
    for t in texts:
        m = _PERCENT_RE.search(t)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def _first_int_kalo(texts: list[str]) -> Optional[int]:
    """Match suffixed counts like "219.51k", "17.27k" or plain "1,234"."""
    for t in texts:
        if "$" in t or "%" in t:
            continue
        m = re.search(r"([\d.]+)\s*([kKmMbB])\b", t)
        if m:
            try:
                return int(_parse_suffixed(f"{m.group(1)}{m.group(2)}"))
            except Exception:
                continue
        m2 = _INT_RE.search(t)
        if m2:
            try:
                return int(m2.group(1).replace(",", ""))
            except Exception:
                continue
    return None


def _first_int(texts: list[str]) -> Optional[int]:
    for t in texts:
        if "$" in t or "%" in t:
            continue
        m = _INT_RE.search(t)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def _kill_paywall_overlay(page: Page) -> int:
    """
    Kalodata sits a `Component-MemberListMask` div on top of the table
    pagination area to nudge paid signups. It intercepts pointer events on
    the page-size changer and the Next button. Wipe it from the DOM so
    clicks land. Returns the number of overlay nodes removed.
    """
    try:
        return page.evaluate(
            """() => {
                const sels = [
                    '.Component-MemberListMask',
                    '.tablePaginationMemberListMask',
                    '.Component-MemberItemLock',
                ];
                let n = 0;
                for (const s of sels) {
                    for (const el of document.querySelectorAll(s)) {
                        el.remove();
                        n += 1;
                    }
                }
                return n;
            }"""
        )
    except Exception:
        return 0


def _go_to_next_page(page: Page) -> bool:
    try:
        _kill_paywall_overlay(page)
        btn = page.locator(S.NEXT_PAGE_BUTTON).first
        if btn.count() == 0:
            return False
        if (btn.get_attribute("aria-disabled") or "").lower() == "true":
            return False
        btn.scroll_into_view_if_needed(timeout=2000)
        try:
            btn.click(timeout=2000)
        except Exception:
            # Last resort: fire the click via JS to bypass any overlay
            # that re-renders between the remove() and the click.
            btn.evaluate("el => el.click()")
        page.wait_for_timeout(1500)
        return True
    except PlaywrightTimeoutError:
        return False
    except Exception as e:
        print(f"[scrape] pagination click failed: {e}", flush=True)
        return False


def _set_page_size(page: Page, n: int) -> bool:
    """
    Open Ant's page-size selector at the bottom-right of the product table
    and pick `n` items per page. Logs every step so we can tell whether the
    changer was found, what options Kalodata offered, and which one fired.
    """
    try:
        killed = _kill_paywall_overlay(page)
        if killed:
            print(f"[scrape] removed {killed} paywall overlay node(s)", flush=True)
        changer = page.locator(S.PAGE_SIZE_CHANGER).first
        if changer.count() == 0:
            print(f"[scrape] page-size changer not found (selector={S.PAGE_SIZE_CHANGER})", flush=True)
            return False
        changer.scroll_into_view_if_needed(timeout=2000)
        try:
            changer.click(timeout=2000)
        except Exception:
            changer.evaluate("el => el.click()")
        page.wait_for_timeout(700)

        # The dropdown appears in a separate Ant overlay; list every option
        # it offers so we know exactly what's selectable.
        options = page.locator(
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
            ".ant-select-item-option"
        )
        opt_texts: list[str] = []
        for i in range(min(options.count(), 12)):
            try:
                opt_texts.append(options.nth(i).inner_text().strip())
            except Exception:
                continue
        print(f"[scrape] page-size options on offer: {opt_texts}", flush=True)

        # Try the requested size first; if not present, fall back to the
        # largest available <= the request.
        target_str = f"{n}"
        candidates = [t for t in opt_texts if t.split()[0] == target_str]
        if not candidates:
            numeric = []
            for t in opt_texts:
                try:
                    numeric.append((int(t.split()[0]), t))
                except (ValueError, IndexError):
                    continue
            numeric = [x for x in numeric if x[0] <= n]
            if not numeric:
                print(f"[scrape] no page-size option <= {n}; closing menu", flush=True)
                page.keyboard.press("Escape")
                return False
            numeric.sort(reverse=True)
            candidates = [numeric[0][1]]

        choice = candidates[0]
        page.locator(
            ".ant-select-dropdown:not(.ant-select-dropdown-hidden) "
            ".ant-select-item-option"
        ).filter(has_text=choice).first.click(timeout=3000)
        page.wait_for_timeout(1800)
        print(f"[scrape] page size → {choice}", flush=True)
        return True
    except Exception as e:
        print(f"[scrape] could not set page size to {n}: {e}", flush=True)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def _scroll_for_more_rows(page: Page, prev_row_count: int) -> bool:
    """
    Kalodata's product table is virtualized — scrolling the body container
    asks the next batch of rows to render. Returns True if at least one new
    row appeared within ~2 seconds.
    """
    try:
        body = page.locator(S.TABLE_SCROLL_CONTAINER).first
        if body.count() == 0:
            # Fall back to window scroll if the table body selector misses.
            page.evaluate("window.scrollBy(0, window.innerHeight)")
        else:
            body.evaluate("el => el.scrollBy(0, el.clientHeight)")
        # Wait for the row count to change, up to ~2s.
        for _ in range(8):
            page.wait_for_timeout(250)
            now = page.locator(S.PRODUCT_ROW).count()
            if now > prev_row_count:
                return True
        return False
    except Exception as e:
        print(f"[scrape] scroll failed: {e}", flush=True)
        return False


# ---------------------------------------------------------------------------
# Per-product asset download
# ---------------------------------------------------------------------------

def fetch_product_assets(
    product: Product,
    storage_state_path: Path,
    dest_dir: Path,
    headed: bool = True,
    max_photos: int = 8,
) -> list[Path]:
    """
    Download the product's photos to dest_dir/<product-slug>/. The cover URL
    is constructed from the product ID (Kalodata serves it at a predictable
    CDN path), so no browser is needed for Phase 1. Later phases that want
    the full gallery can visit the detail page.
    """
    out_dir = dest_dir / product.slug()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not product.photo_urls:
        product.photo_urls = [S.COVER_URL_TEMPLATE.format(product_id=product.id)]

    saved: list[Path] = []
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for idx, url in enumerate(product.photo_urls[:max_photos]):
            try:
                ext = _ext_from_url(url) or ".png"
                dest = out_dir / f"{idx:02d}{ext}"
                r = client.get(url)
                r.raise_for_status()
                dest.write_bytes(r.content)
                saved.append(dest)
            except Exception as e:
                print(f"[assets] failed to download {url}: {e}")

    (out_dir / "product.json").write_text(
        json.dumps(product.to_dict(), indent=2, default=str)
    )
    return saved


def _ext_from_url(url: str) -> Optional[str]:
    path = urlparse(url).path
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.lower().endswith(ext):
            return ext
    return None
