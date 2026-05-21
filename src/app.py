"""
Streamlit entrypoint for the KaloData → Nano Banana → Veo pipeline.

Run via:
    streamlit run src/app.py

Or via the launcher script:
    ./start.command   (mac)
    start.bat         (windows)

Phase 4 scope (current): filter form → scrape → review grid with checkboxes →
save selection.json. The Generate button is wired up but stubbed until Phase 2
generators land.
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import streamlit as st
import yaml

# Allow `streamlit run src/app.py` to import sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.posters import archive  # noqa: E402
from src.scraper.kalodata import Filters, Product  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORAGE_PATH = PROJECT_ROOT / ".auth/kalodata.json"
SELECTION_ROOT = PROJECT_ROOT / "outputs/_selection"
SCRAPE_OUT_DIR = PROJECT_ROOT / "outputs/_dashboard"
SCRAPE_SCRIPT = PROJECT_ROOT / "scripts/scrape_test.py"
PIPELINE_SCRIPT = PROJECT_ROOT / "src/pipeline.py"  # Veo API path
FLOW_PIPELINE_SCRIPT = PROJECT_ROOT / "scripts/run_flow_pipeline.py"  # Flow UI path
REGEN_SCRIPT = PROJECT_ROOT / "scripts/regenerate_product.py"
RUNS_ROOT = PROJECT_ROOT / "outputs"


def _render_results_gallery(manifest_path: Path) -> None:
    """
    Render Generate results. Each product becomes a full-width row showing the
    staged image, its variants (one per target account) side-by-side, plus
    per-variant edit/regenerate + mark-as-posted controls.
    """
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    run_id = manifest.get("run_id", manifest_path.parent.name)
    items = manifest.get("products", [])
    st.markdown(f"### Results — run `{run_id}`")

    # Bulk-assign whole run to an account. Each successful variant gets copied
    # into outputs/_posted/<account-slug>/<run-dir>/ with a sidecar JSON that
    # has caption, hook, and the computed affiliate link.
    _render_run_assign_bar(manifest_path, run_id, items)

    for entry in items:
        _render_product_row(entry, run_id)


def _render_run_assign_bar(manifest_path: Path, run_id: str, items: list) -> None:
    accounts = archive.load_accounts()
    n_ok = sum(1 for p in items if p.get("status") == "ok")
    a1, a2, a3 = st.columns([2, 3, 4])
    a1.caption(f"{n_ok} product(s) ready · {n_ok * 2} variants")
    if not accounts:
        a2.caption("No accounts in `config/accounts.yaml`")
        return
    label_to_account = {a.get("display_name") or a["handle"]: a for a in accounts}
    chosen_label = a2.selectbox(
        "Assign run to account",
        options=list(label_to_account.keys()),
        key=f"assign-{run_id}-select",
        label_visibility="collapsed",
    )
    chosen = label_to_account[chosen_label]
    tag_set = bool((chosen.get("affiliate_tag") or "").strip())
    if not tag_set:
        a3.warning(
            f"⚠️ `{chosen.get('display_name') or chosen['handle']}` has no "
            f"affiliate_tag set in config/accounts.yaml — links will be blank "
            f"in the sidecar JSON."
        )
    if a3.button(
        f"Assign all to {chosen_label}",
        key=f"assign-{run_id}-go",
        disabled=(n_ok == 0),
        help=(
            "Copies every successful variant MP4 into "
            "outputs/_posted/<account>/<run>/ with a per-variant .json "
            "sidecar (hook, caption, affiliate link, product URL)."
        ),
    ):
        try:
            result = archive.assign_run_to_account(manifest_path, chosen)
            st.success(
                f"Assigned **{result['n_copied']}** clip(s) to "
                f"`{chosen.get('display_name') or chosen['handle']}` → "
                f"`{result['dest_dir'].relative_to(PROJECT_ROOT)}`"
            )
            if result["skipped"]:
                with st.expander(f"Skipped {len(result['skipped'])} item(s)"):
                    for s in result["skipped"]:
                        st.caption(f"• {s}")
        except Exception as e:
            st.error(f"Assign failed: {e}")


def _render_product_row(entry: dict, run_id: str) -> None:
    title = (entry.get("title") or entry.get("slug") or "?")[:80]
    st.markdown(f"#### {title}")
    if entry.get("status") != "ok":
        st.error(entry.get("error", "failed"))
        return

    pid = entry.get("id") or entry.get("slug") or "unknown"
    key_prefix = f"prod-{run_id}-{pid}"

    # Legacy entries (no `variants` list): synthesise a single variant so the
    # same renderer works for both old and new manifests.
    variants = entry.get("variants")
    if not variants:
        variants = [{
            "label": "A",
            "account": None,
            "platform": None,
            "video": entry.get("captioned_video") or entry.get("video"),
            "hook": entry.get("hook"),
            "post_caption": entry.get("post_caption"),
            "veo_prompt_used": entry.get("veo_prompt_used"),
        }]

    # Top-row: shared restage image + shared Nano Banana prompt editor.
    left, right = st.columns([1, 2])
    with left:
        if entry.get("staged"):
            st.image(
                str(PROJECT_ROOT / entry["staged"]),
                width="stretch",
                caption=f"Staged in {entry.get('environment') or '?'}",
            )
    with right:
        with st.expander("Restage prompt (shared across variants)"):
            new_nano = st.text_area(
                "Nano Banana prompt",
                value=entry.get("nano_prompt_used", ""),
                height=110,
                key=f"{key_prefix}-nano",
            )
            if st.button(
                "Re-restage (regenerates all variants)",
                key=f"{key_prefix}-restage",
                help="Re-runs Nano Banana with the edited prompt, then re-runs Veo for every variant.",
            ):
                _shell_regen(run_id, pid, nano_prompt=new_nano)

    # Variants row.
    cols = st.columns(max(1, len(variants)))
    for v_idx, (col, v) in enumerate(zip(cols, variants)):
        with col:
            account_label = v.get("account") or f"Variant {v.get('label', v_idx + 1)}"
            plat = f" · {v['platform']}" if v.get("platform") else ""
            st.markdown(f"**{account_label}{plat}**")
            if v.get("error"):
                st.error(v["error"])
            if v.get("video"):
                st.video(str(PROJECT_ROOT / v["video"]), width="stretch")
            if v.get("hook"):
                st.caption(f"Hook: *{v['hook']}*")

            vkey = f"{key_prefix}-v{v.get('label', v_idx)}"
            with st.expander("Edit variant"):
                new_veo = st.text_area(
                    "Veo prompt",
                    value=v.get("veo_prompt_used", ""),
                    height=80,
                    key=f"{vkey}-veo",
                )
                new_hook = st.text_input(
                    "Hook",
                    value=v.get("hook", ""),
                    key=f"{vkey}-hook",
                )
                if v.get("post_caption"):
                    st.caption("Post caption:")
                    st.code(v["post_caption"])
                if st.button(
                    "Regenerate this variant",
                    key=f"{vkey}-go",
                    type="primary",
                ):
                    _shell_regen(
                        run_id, pid,
                        variant_label=v.get("label"),
                        veo_prompt=new_veo,
                        hook=new_hook,
                    )

            # Mark-as-posted, account pre-filled from the variant.
            _render_mark_as_posted_variant(entry, v, run_id)

    st.divider()


def _shell_regen(
    run_id: str,
    pid: str,
    nano_prompt: str | None = None,
    veo_prompt: str | None = None,
    hook: str | None = None,
    variant_label: str | None = None,
) -> None:
    """Shell out to scripts/regenerate_product.py and stream its log."""
    cmd = [
        sys.executable, str(REGEN_SCRIPT),
        "--run-id", run_id,
        "--product-id", str(pid),
    ]
    if nano_prompt is not None:
        cmd += ["--nano-prompt", nano_prompt]
    if veo_prompt is not None:
        cmd += ["--veo-prompt", veo_prompt]
    if hook is not None:
        cmd += ["--hook", hook]
    if variant_label is not None:
        cmd += ["--variant-label", variant_label]

    log_slot = st.empty()
    lines: list[str] = []
    with st.spinner(f"Regenerating {pid}..."):
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line.rstrip())
            log_slot.code("\n".join(lines[-50:]))
        proc.wait()
    if proc.returncode == 0:
        st.success("Regenerated. Refresh the page to see the new clip.")
    else:
        st.error(f"Regen exited with code {proc.returncode}.")


def _render_mark_as_posted_variant(entry: dict, variant: dict, run_id: str) -> None:
    """
    Mark-as-posted for a single variant. Account dropdown defaults to the
    variant's assigned account so the client just confirms the date.
    """
    pid = entry.get("id") or entry.get("slug") or "unknown"
    vlabel = variant.get("label") or "x"
    key_prefix = f"post-{run_id}-{pid}-{vlabel}"
    accounts = archive.load_accounts()

    with st.expander("Mark as posted"):
        if not accounts:
            st.info(
                "No accounts configured. Add handles to `config/accounts.yaml` "
                "and restart Streamlit."
            )
            return

        existing_chips: list[str] = []
        for acct in accounts:
            prior = archive.is_already_posted(run_id, str(pid), acct["handle"])
            for p in prior:
                existing_chips.append(f"✓ {acct['handle']} on {p['posted_at']}")
        if existing_chips:
            st.caption(" · ".join(existing_chips))

        # Pre-select the variant's assigned account.
        labels: list[str] = []
        label_to_account: dict[str, dict] = {}
        default_idx = 0
        for i, a in enumerate(accounts):
            lbl = a.get("display_name") or a["handle"]
            if a.get("platform") and not a.get("display_name"):
                lbl = f"{lbl} ({a['platform']})"
            labels.append(lbl)
            label_to_account[lbl] = a
            if a["handle"] == variant.get("account"):
                default_idx = i

        picked_label = st.selectbox(
            "Posted to",
            labels,
            index=default_idx,
            key=f"{key_prefix}-accounts",
        )
        posted_on = st.date_input(
            "Posted on",
            value=date.today(),
            key=f"{key_prefix}-date",
        )
        posted_url = st.text_input(
            "Posted URL (optional)",
            value="",
            key=f"{key_prefix}-url",
            placeholder="https://www.tiktok.com/@brand/video/...",
        )

        if st.button(
            "Save to archive",
            key=f"{key_prefix}-save",
            type="primary",
        ):
            # Build a flat product-entry dict the archive can read: it expects
            # captioned_video/video/staged paths at the top level.
            flat = {**entry, "video": variant.get("video"), "captioned_video": None,
                    "hook": variant.get("hook"),
                    "post_caption": variant.get("post_caption")}
            try:
                archive.record_post(
                    product_entry=flat,
                    account=label_to_account[picked_label],
                    posted_at=posted_on,
                    run_id=run_id,
                    posted_url=posted_url or None,
                )
                st.success(f"Archived variant {vlabel} → {label_to_account[picked_label]['handle']}.")
            except Exception as e:
                st.error(str(e))


def _render_mark_as_posted(entry: dict, run_id: str) -> None:
    """
    Per-card "Mark as posted" expander: lets the client log which account(s)
    a generated clip got posted to. Files copied + ledger appended via
    src.posters.archive.
    """
    pid = entry.get("id") or entry.get("slug") or "unknown"
    key_prefix = f"post-{run_id}-{pid}"
    accounts = archive.load_accounts()

    with st.expander("Mark as posted"):
        if not accounts:
            st.info(
                "No accounts configured. Add your TikTok/IG/etc. handles to "
                "`config/accounts.yaml` and restart Streamlit. See the comments "
                "in that file for the schema."
            )
            return

        # Show existing posts for this clip so the client knows what's already
        # archived without scrolling to the Posted archive section.
        existing_chips: list[str] = []
        for acct in accounts:
            prior = archive.is_already_posted(run_id, str(pid), acct["handle"])
            for p in prior:
                existing_chips.append(f"✓ {acct['handle']} on {p['posted_at']}")
        if existing_chips:
            st.caption(" · ".join(existing_chips))

        labels = [
            (a.get("display_name") or a["handle"]) + (
                f" ({a.get('platform')})" if a.get("platform") and not a.get("display_name") else ""
            )
            for a in accounts
        ]
        label_to_account = dict(zip(labels, accounts))
        picked_labels = st.multiselect(
            "Posted to",
            labels,
            key=f"{key_prefix}-accounts",
        )
        posted_on = st.date_input(
            "Posted on",
            value=date.today(),
            key=f"{key_prefix}-date",
        )
        posted_url = st.text_input(
            "Posted URL (optional)",
            value="",
            key=f"{key_prefix}-url",
            placeholder="https://www.tiktok.com/@brand/video/...",
        )

        if st.button(
            "Save to archive",
            key=f"{key_prefix}-save",
            type="primary",
            disabled=len(picked_labels) == 0,
        ):
            saved = 0
            errors: list[str] = []
            for lbl in picked_labels:
                acct = label_to_account[lbl]
                try:
                    archive.record_post(
                        product_entry=entry,
                        account=acct,
                        posted_at=posted_on,
                        run_id=run_id,
                        posted_url=posted_url or None,
                    )
                    saved += 1
                except Exception as e:
                    errors.append(f"{acct['handle']}: {e}")
            if saved:
                st.success(f"Archived {saved} post record(s).")
            for msg in errors:
                st.error(msg)


def _load_cost_config(backend_override: str | None = None) -> tuple[float, float, str]:
    """Return (image_cost_each, video_cost_each, backend_key).

    Pass `backend_override` to compute the cost for a backend other than the
    one currently in settings.yaml :: generation.video_backend — used by the
    dashboard's per-run backend radio so the cost estimate matches the choice."""
    settings_path = PROJECT_ROOT / "config/settings.yaml"
    try:
        with settings_path.open() as f:
            settings = yaml.safe_load(f) or {}
    except Exception:
        settings = {}
    costs = settings.get("costs", {})
    backend = backend_override or settings.get("generation", {}).get("video_backend", "veo_api_fast")
    img = float(costs.get("image_per_call_usd", 0.04))
    vid = float(costs.get("video_per_clip_usd", {}).get(backend, 1.20))
    return img, vid, backend

# Hard-coded option lists so the client can only pick values Kalodata actually
# accepts. If Kalodata adds a region/category, update these and restart.
REGIONS = ["US", "UK", "ID", "MY", "TH", "VN", "PH", "SG", "MX", "BR"]

CATEGORIES = [
    "All categories",
    "Beauty & Personal Care",
    "Womenswear & Underwear",
    "Menswear & Underwear",
    "Phones & Electronics",
    "Home Supplies",
    "Kitchenware",
    "Health",
    "Food & Beverages",
    "Sports & Outdoor",
    "Pet Supplies",
    "Toys & Hobbies",
    "Baby & Maternity",
    "Shoes",
    "Bags & Luggage",
    "Automotive & Motorcycle",
    "Tools & Hardware",
    "Books, Magazines & Audio",
    "Computers & Office Equipment",
    "Furniture",
    "Jewelry & Accessories",
    "Textiles & Soft Furnishings",
    "Household Appliances",
]

TIME_WINDOWS = {
    "Last 24 hours": "last_24_hours",
    "Last 7 days":   "last_7_days",
    "Last 30 days":  "last_30_days",
}

# Buckets match Kalodata's actual on-screen brackets (verified via
# scripts/dump_kalodata_dom.py). Each label → numeric Min passed to the
# scraper, which clicks the bracket whose floor that Min lands in.
REVENUE_BUCKETS = {
    "Any":          0,
    "<$100":        1,
    "$100-$1k":     100,
    "$1k-$10k":     1_000,
    ">$10k":        10_000,
}

ITEM_SOLD_BUCKETS = {
    "Any":      0,
    "0-50":     1,
    "50-500":   50,
    "500-5k":   500,
    "5k-10k":   5_000,
    ">10k":     10_000,
}

GROWTH_BUCKETS = {
    "Any":     0,
    ">0%":     1,
    ">30%":    30,
    ">70%":    70,
    ">100%":   100,
}

REVENUE_SOURCE_CONTENT_OPTIONS = ["Any", "Video", "LIVE", "Product Card"]
REVENUE_SOURCE_CHANNEL_OPTIONS = ["Any", "Creator", "Shop", "Mall", "Affiliate"]

AVG_UNIT_PRICE_BUCKETS = {
    "Any":      0,
    "$5+":      5,
    "$10+":     10,
    "$25+":     25,
    "$50+":     50,
    "$100+":    100,
}

COMMISSION_RATE_BUCKETS = {
    "Any":    0,
    "5%+":    5,
    "10%+":   10,
    "20%+":   20,
    "30%+":   30,
    "50%+":   50,
}

CREATOR_NUMBER_BUCKETS = {
    "Any":   0,
    "10+":   10,
    "50+":   50,
    "100+":  100,
    "500+":  500,
    "1k+":   1_000,
}

CREATOR_CONVERSION_BUCKETS = {
    "Any":    0,
    "1%+":    1,
    "2%+":    2,
    "5%+":    5,
    "10%+":   10,
}

SHIPPING_OPTIONS = ["Any", "Free Shipping", "Express", "Standard"]
AFFILIATE_OPTIONS = ["Any", "Yes", "No"]
LAUNCH_DATE_OPTIONS = ["Any", "Last 7 days", "Last 30 days", "Last 90 days", "Last 180 days"]


st.set_page_config(page_title="KaloData → Veo Pipeline", layout="wide")
st.title("KaloData → Nano Banana → Veo")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def _load_products_from_disk() -> list[Product]:
    """Re-hydrate the last scrape from disk so a tab refresh doesn't lose state."""
    products_json = SCRAPE_OUT_DIR / "products.json"
    if not products_json.exists():
        return []
    try:
        raw = json.loads(products_json.read_text())
    except Exception:
        return []
    allowed = {f for f in Product.__dataclass_fields__}
    out: list[Product] = []
    for d in raw:
        try:
            out.append(Product(**{k: v for k, v in d.items() if k in allowed}))
        except Exception:
            continue
    return out


def _latest_run_id() -> str | None:
    """Return the most recent run-id folder under outputs/ that has a manifest."""
    if not RUNS_ROOT.exists():
        return None
    candidates = sorted(
        (d for d in RUNS_ROOT.iterdir() if d.is_dir() and (d / "manifest.json").exists()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    return candidates[0].name if candidates else None


if "products" not in st.session_state:
    st.session_state.products = _load_products_from_disk()
if "selected_ids" not in st.session_state:
    st.session_state.selected_ids = set()
if "last_search_at" not in st.session_state:
    st.session_state.last_search_at = None
if "last_run_id" not in st.session_state:
    st.session_state.last_run_id = _latest_run_id()

# ---------------------------------------------------------------------------
# Sidebar — filters
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Search filters")
    search_clicked_top = st.button(
        "Search Kalodata",
        type="primary",
        use_container_width=True,
        key="search_top",
    )
    headed = st.checkbox(
        "Show browser window during search",
        value=True,
        help="Required the first time per Chrome profile so you can clear any Cloudflare challenge.",
    )
    max_results = st.slider("Max products to load", 5, 100, 50, step=5)
    st.markdown("---")

    region = st.selectbox("Region", REGIONS, index=0)
    time_window_label = st.selectbox("Time window", list(TIME_WINDOWS.keys()), index=2)
    time_window = TIME_WINDOWS[time_window_label]
    category = st.selectbox("Category", CATEGORIES, index=0)

    st.markdown("---")
    st.caption("**Revenue Filters**")
    revenue_label = st.selectbox("Revenue($)", list(REVENUE_BUCKETS.keys()), index=2)
    min_gmv = REVENUE_BUCKETS[revenue_label]
    item_sold_label = st.selectbox("Item Sold", list(ITEM_SOLD_BUCKETS.keys()), index=1)
    min_sales = ITEM_SOLD_BUCKETS[item_sold_label]
    rev_src_content = st.selectbox("Revenue Source(Content)", REVENUE_SOURCE_CONTENT_OPTIONS, index=0)
    rev_src_channel = st.selectbox("Revenue Source(Channel)", REVENUE_SOURCE_CHANNEL_OPTIONS, index=0)
    growth_label = st.selectbox("Revenue Growth Rate", list(GROWTH_BUCKETS.keys()), index=0)
    min_growth = GROWTH_BUCKETS[growth_label]

    st.markdown("---")
    st.caption("**Advanced**")
    avg_price_label = st.selectbox("Avg. Unit Price($)", list(AVG_UNIT_PRICE_BUCKETS.keys()), index=0)
    is_affiliate = st.selectbox("Is Affiliate Product", AFFILIATE_OPTIONS, index=0)
    creator_num_label = st.selectbox("Creator Number", list(CREATOR_NUMBER_BUCKETS.keys()), index=0)
    creator_conv_label = st.selectbox("Creator Conversion Ratio", list(CREATOR_CONVERSION_BUCKETS.keys()), index=0)
    shipping_opt = st.selectbox("Shipping Option", SHIPPING_OPTIONS, index=0)
    launch_date_opt = st.selectbox("Launch Date", LAUNCH_DATE_OPTIONS, index=0)
    commission_label = st.selectbox("Commission Rate", list(COMMISSION_RATE_BUCKETS.keys()), index=0)

    st.markdown("---")
    search_clicked_bottom = st.button(
        "Search Kalodata",
        type="primary",
        use_container_width=True,
        key="search_bottom",
    )

search_clicked = search_clicked_top or search_clicked_bottom


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _run_search() -> None:
    chrome_profile = PROJECT_ROOT / ".auth/chrome-profile"
    if not STORAGE_PATH.exists() and not chrome_profile.exists():
        st.error("No Kalodata session found. Run `python scripts/login_kalodata.py` first.")
        return

    SCRAPE_OUT_DIR.mkdir(parents=True, exist_ok=True)
    products_json = SCRAPE_OUT_DIR / "products.json"
    if products_json.exists():
        products_json.unlink()

    cmd = [
        sys.executable,
        str(SCRAPE_SCRIPT),
        "--region", region,
        "--time-window", time_window,
        "--min-gmv", str(int(min_gmv)),
        "--min-sales", str(int(min_sales)),
        "--min-growth", str(int(min_growth)),
        "--max-results", str(int(max_results)),
        "--out-dir", str(SCRAPE_OUT_DIR),
        "--no-open",
    ]
    if category and category != "All categories":
        cmd += ["--category", category]
    if not headed:
        cmd.append("--headless")

    st.info(
        "Driving Kalodata's filter rail now. Watch the headed Chrome window — "
        "if a click fails it shows up in the streaming log below and you can "
        "adjust that filter by hand before the table is scraped."
    )
    log_box = st.empty()
    progress_lines: list[str] = []

    with st.spinner(
        f"Running Kalodata search for up to {max_results} products… "
        "watch the headed Chrome window."
    ):
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            progress_lines.append(line.rstrip())
            log_box.code("\n".join(progress_lines[-200:]))
        proc.wait()

    if proc.returncode != 0 or not products_json.exists():
        st.error(
            f"Scraper exited with code {proc.returncode}. "
            "Full log is above."
        )
        return

    raw = json.loads(products_json.read_text())
    products: list[Product] = []
    for d in raw:
        try:
            products.append(Product(**d))
        except TypeError:
            # Tolerate extra/missing keys if Product gains fields.
            allowed = {f for f in Product.__dataclass_fields__}
            products.append(Product(**{k: v for k, v in d.items() if k in allowed}))

    st.session_state.products = products
    st.session_state.selected_ids = set()
    st.session_state.last_search_at = datetime.now()


if search_clicked:
    _run_search()


# ---------------------------------------------------------------------------
# Results grid
# ---------------------------------------------------------------------------
products: list[Product] = st.session_state.products

if not products:
    st.info("Set your filters in the sidebar and click **Search Kalodata** to load results.")
    st.stop()

ts = st.session_state.last_search_at
st.caption(
    f"Loaded **{len(products)}** products in Kalodata's current ranking order "
    f"(top of the results page = #1)"
    + (f" — searched at {ts.strftime('%H:%M:%S')}" if ts else "")
    + ". Tick the ones you want and click **Save selection** below."
)

# Bulk-select controls. Streamlit checkboxes with a `key=` use session_state
# as their source of truth — the `value=` arg is ignored after first render.
# So Select all / Clear must write directly into those per-checkbox keys; the
# selected_ids set is then DERIVED from them, never mutated independently.
c1, c2, c3, c4, _ = st.columns([1, 1, 1.4, 1, 4])
if c1.button("Select all"):
    for p in products:
        st.session_state[f"pick_{p.id}"] = True
if c2.button("Clear"):
    for p in products:
        st.session_state[f"pick_{p.id}"] = False
if c3.button("Clear search history", help="Wipes loaded products and the cached scrape on disk."):
    for p in products:
        st.session_state.pop(f"pick_{p.id}", None)
    st.session_state.products = []
    st.session_state.selected_ids = set()
    st.session_state.last_search_at = None
    products_json = SCRAPE_OUT_DIR / "products.json"
    if products_json.exists():
        products_json.unlink()
    st.rerun()

st.session_state.selected_ids = {
    p.id for p in products
    if st.session_state.get(f"pick_{p.id}", False)
}
c4.metric("Selected", len(st.session_state.selected_ids))

# Card grid — 4 columns
COLS = 4
for row_start in range(0, len(products), COLS):
    row = st.columns(COLS)
    for offset, (col, product) in enumerate(
        zip(row, products[row_start : row_start + COLS])
    ):
        rank = row_start + offset + 1
        with col:
            if product.photo_urls:
                img_url = html.escape(product.photo_urls[0])
                st.markdown(
                    f"<img src='{img_url}' "
                    f"referrerpolicy='no-referrer' "
                    f"style='width:100%; aspect-ratio:1/1; object-fit:cover; "
                    f"border-radius:6px; background:#f0f0f0;' />",
                    unsafe_allow_html=True,
                )
            st.markdown(
                f"**#{rank}** &nbsp; [{product.title[:55]}]({product.kalodata_url})",
                unsafe_allow_html=True,
            )
            if product.extras:
                stat_lines = [
                    f"<span style='font-weight:700;color:inherit'>{col}</span>"
                    f"&nbsp;<span style='color:inherit;opacity:0.95'>{val}</span>"
                    for col, val in product.extras.items()
                ]
                st.markdown(
                    "<div style='font-size:14px;line-height:1.7;"
                    "color:inherit;font-weight:500'>"
                    + "<br>".join(stat_lines)
                    + "</div>",
                    unsafe_allow_html=True,
                )

            # Widget state at session_state[f"pick_{id}"] is the source of
            # truth — set by the Select all / Clear buttons above, or by the
            # user toggling this checkbox directly. selected_ids is derived
            # at the top of the grid block, so we don't touch it here.
            st.checkbox(
                "Pick this one",
                key=f"pick_{product.id}",
            )


# ---------------------------------------------------------------------------
# Save selection
# ---------------------------------------------------------------------------
st.divider()
left, right = st.columns([1, 3])
with left:
    save_clicked = st.button(
        "Save selection",
        type="primary",
        disabled=len(st.session_state.selected_ids) == 0,
        use_container_width=True,
    )
with right:
    st.caption(
        "Writes the selected products (IDs, titles, cover URLs, stats) to "
        "`outputs/_selection/selection.json`. The Generate stage will read from there."
    )

if save_clicked:
    SELECTION_ROOT.mkdir(parents=True, exist_ok=True)
    picks = [p.to_dict() for p in products if p.id in st.session_state.selected_ids]
    out_path = SELECTION_ROOT / "selection.json"
    out_path.write_text(json.dumps(picks, indent=2, default=str))
    meta_path = SELECTION_ROOT / "meta.json"
    meta_path.write_text(json.dumps({
        "region": region,
        "time_window": time_window,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    st.success(f"Saved {len(picks)} picks ({region}) → `{out_path}`")


# ---------------------------------------------------------------------------
# Generate (Nano Banana + Veo)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Generate ad clips")

selection_file = SELECTION_ROOT / "selection.json"
if selection_file.exists():
    try:
        saved_picks = json.loads(selection_file.read_text())
    except Exception:
        saved_picks = []
else:
    saved_picks = []

BACKEND_OPTIONS = {
    "Flow (Playwright, ~$0.08/clip)":     "flow_playwright",
    "Veo API — Lite (~$0.40/clip)":       "veo_api_light",
    "Veo API — Fast (~$1.20/clip)":       "veo_api_fast",
    "Veo API — Standard (~$3.20/clip)":   "veo_api_standard",
}
backend_choice_label = st.radio(
    "Video backend",
    options=list(BACKEND_OPTIONS.keys()),
    index=0,
    horizontal=True,
    help=(
        "Flow drives Google Flow's UI via Playwright on your Ultra subscription "
        "(~$0.08 effective). The Veo API options bill per-clip via the public "
        "Gemini API. Both reuse the same Nano Banana restage step."
    ),
)
backend_key = BACKEND_OPTIONS[backend_choice_label]
is_flow_backend = backend_key == "flow_playwright"

img_each, vid_each, _ = _load_cost_config(backend_override=backend_key)
cap = float(os.environ.get("MAX_RUN_COST_USD", "10"))
n_picks = len(saved_picks)
VARIANTS_PER_PRODUCT = 2
img_total = img_each * n_picks
vid_total = vid_each * n_picks * VARIANTS_PER_PRODUCT
total_cost = img_total + vid_total
over_cap = total_cost > cap

c1, c2, c3 = st.columns([1, 1, 1])
c1.metric("Saved picks", n_picks)
c2.metric("Est. image cost", f"${img_total:.2f}", help=f"${img_each:.3f} × {n_picks}")
c3.metric(
    f"Est. video cost ({VARIANTS_PER_PRODUCT}× per product)",
    f"${vid_total:.2f}",
    help=f"${vid_each:.2f} × {n_picks} products × {VARIANTS_PER_PRODUCT} variants (backend: {backend_key})",
)

st.caption(
    f"Total estimated cost: **${total_cost:.2f}** · "
    f"MAX_RUN_COST_USD cap: ${cap:.2f}"
    + (" — **over cap, raise it in .env to proceed**" if over_cap else "")
)

gc1, gc2, gc3 = st.columns([1, 1, 4])
with gc1:
    gen_clicked = st.button(
        "Generate clips",
        type="primary",
        use_container_width=True,
        disabled=(n_picks == 0 or over_cap),
        help="Runs Nano Banana restage → Veo animate for each saved pick.",
    )
with gc2:
    skip_video = st.checkbox(
        "Restage only",
        value=False,
        help="Run Nano Banana restage but skip Veo/Flow. Cheap + fast for sanity-checking the boutique scenes.",
    )

if gen_clicked:
    # Region used at scrape time, not whatever the sidebar shows now.
    meta_path = SELECTION_ROOT / "meta.json"
    gen_region = region
    if meta_path.exists():
        try:
            gen_region = (json.loads(meta_path.read_text()) or {}).get("region") or region
        except Exception:
            pass
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{gen_region}"
    if is_flow_backend:
        # Flow runner takes the same --run-id / --region / --skip-video flags
        # but writes to outputs/<run-id>-flow/. Model comes from settings.yaml ::
        # generation.flow.model (currently veo-3.1-lite).
        cmd = [sys.executable, str(FLOW_PIPELINE_SCRIPT),
               "--run-id", run_id, "--region", gen_region]
    else:
        # API path. Pass --backend so pipeline.py uses the model matching the
        # radio choice (otherwise it falls back to settings.yaml's default).
        cmd = [sys.executable, str(PIPELINE_SCRIPT),
               "--run-id", run_id, "--region", gen_region,
               "--backend", backend_key]
    if skip_video:
        cmd.append("--skip-video")

    log_box = st.empty()
    progress_lines: list[str] = []
    if skip_video:
        label = "Restaging via Nano Banana…"
    elif is_flow_backend:
        label = "Driving Google Flow via Playwright (Chrome will pop; ~1-2 min/clip + RAI retries)…"
    else:
        label = "Running Nano Banana + Veo pipeline (videos can take 1-3 min each)…"
    with st.spinner(label):
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            progress_lines.append(line.rstrip())
            log_box.code("\n".join(progress_lines[-200:]))
        proc.wait()

    # Flow runs land in outputs/<run_id>-flow/; API runs in outputs/<run_id>/.
    # Honor the suffix so the success branch resolves and last_run_id updates.
    run_dir_name = f"{run_id}-flow" if is_flow_backend else run_id
    run_dir = RUNS_ROOT / run_dir_name
    manifest_path = run_dir / "manifest.json"
    if proc.returncode != 0:
        st.error(f"Pipeline exited with code {proc.returncode}. See log above.")
    elif not manifest_path.exists():
        st.error(f"Pipeline finished but no manifest at {manifest_path}.")
    else:
        manifest = json.loads(manifest_path.read_text())
        n_ok = sum(1 for p in manifest.get("products", []) if p.get("status") == "ok")
        st.success(f"Done — {n_ok}/{n_picks} succeeded. Manifest: `{manifest_path}`")
        st.session_state.last_run_id = run_dir_name

_last_run_id = st.session_state.get("last_run_id")
if _last_run_id:
    _render_results_gallery(RUNS_ROOT / _last_run_id / "manifest.json")


# ---------------------------------------------------------------------------
# Browse generations by date
# ---------------------------------------------------------------------------
st.divider()
st.subheader("Browse generations by date")


def _region_of_run(run_dir: Path) -> str:
    """Region preference: manifest.region > run-id suffix > '?'."""
    try:
        m = json.loads((run_dir / "manifest.json").read_text())
        r = m.get("region")
        if r:
            return str(r).upper()
    except Exception:
        pass
    parts = run_dir.name.split("-")
    if len(parts) >= 3 and parts[-1].isalpha() and 2 <= len(parts[-1]) <= 3:
        return parts[-1].upper()
    return "?"


def _videos_in_run(run_dir: Path) -> list[dict]:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text())
    except Exception:
        return []
    items: list[dict] = []
    for entry in manifest.get("products", []):
        if entry.get("status") != "ok":
            continue
        title = entry.get("title") or entry.get("slug") or "?"
        variants = entry.get("variants") or [{
            "label": "A",
            "video": entry.get("captioned_video") or entry.get("video"),
            "hook": entry.get("hook"),
            "account": None,
        }]
        for v in variants:
            if v.get("video"):
                items.append({
                    "title": title,
                    "video": v["video"],
                    "hook": v.get("hook"),
                    "account": v.get("account"),
                    "label": v.get("label"),
                    "run_id": manifest.get("run_id", run_dir.name),
                })
    return items


def _render_date_browser() -> None:
    """Group runs by date → country → video grid."""
    if not RUNS_ROOT.exists():
        st.caption("No generations yet.")
        return

    runs_by_date_region: dict[str, dict[str, list[Path]]] = {}
    for d in RUNS_ROOT.iterdir():
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if not (d / "manifest.json").exists():
            continue
        if len(d.name) >= 8 and d.name[:8].isdigit():
            ymd = d.name[:8]
            region = _region_of_run(d)
            runs_by_date_region.setdefault(ymd, {}).setdefault(region, []).append(d)

    if not runs_by_date_region:
        st.caption("No generations yet — run **Generate clips** to populate.")
        return

    for ymd in sorted(runs_by_date_region.keys(), reverse=True):
        try:
            pretty = datetime.strptime(ymd, "%Y%m%d").strftime("%A, %b %d, %Y")
        except ValueError:
            pretty = ymd
        by_region = runs_by_date_region[ymd]

        # Header summary: per-country video counts.
        region_counts: list[str] = []
        for rgn in sorted(by_region.keys()):
            n = sum(len(_videos_in_run(rd)) for rd in by_region[rgn])
            region_counts.append(f"{rgn} {n}")
        header = f"📅 {pretty} — " + " · ".join(region_counts)

        with st.expander(header, expanded=False):
            for rgn in sorted(by_region.keys()):
                run_dirs = sorted(by_region[rgn], reverse=True)
                rgn_videos = [v for rd in run_dirs for v in _videos_in_run(rd)]
                st.markdown(f"### 🌐 {rgn} — {len(rgn_videos)} video(s)")
                if not rgn_videos:
                    st.caption("_No videos in any run for this country today._")
                    continue
                COLS = 3
                for row_start in range(0, len(rgn_videos), COLS):
                    cols = st.columns(COLS)
                    for col, item in zip(cols, rgn_videos[row_start : row_start + COLS]):
                        with col:
                            vpath = PROJECT_ROOT / item["video"]
                            if vpath.exists():
                                st.video(str(vpath), width="stretch")
                            else:
                                st.caption(f"_Missing file: {item['video']}_")
                            st.markdown(f"**{item['title'][:60]}**")
                            meta = [f"run `{item['run_id']}`"]
                            if item.get("account"):
                                meta.append(item["account"])
                            if item.get("label"):
                                meta.append(f"variant {item['label']}")
                            st.caption(" · ".join(meta))
                            if item.get("hook"):
                                st.caption(f"Hook: *{item['hook']}*")
                st.divider()


_render_date_browser()


# ---------------------------------------------------------------------------
# Posted archive browser
# ---------------------------------------------------------------------------
st.divider()
with st.expander("Posted archive", expanded=False):
    all_posts = archive.list_posts()
    if not all_posts:
        st.caption(
            "Nothing archived yet. Mark a clip as posted in the Results "
            "gallery above and it'll show up here."
        )
    else:
        # Date-range filter spanning the full archive.
        post_dates = [
            date.fromisoformat(p["posted_at"])
            for p in all_posts
            if isinstance(p.get("posted_at"), str)
        ]
        min_d = min(post_dates) if post_dates else date.today()
        max_d = max(post_dates) if post_dates else date.today()
        rng = st.date_input(
            "Posted between",
            value=(min_d, max_d),
            min_value=min_d,
            max_value=date.today(),
            key="archive-daterange",
        )
        if isinstance(rng, tuple) and len(rng) == 2:
            d_from, d_to = rng
            filtered = archive.list_posts(date_from=d_from, date_to=d_to)
        else:
            filtered = all_posts

        # Group by account, newest-first inside each.
        by_account: dict[str, list[dict]] = {}
        for p in filtered:
            by_account.setdefault(p.get("account") or "(no account)", []).append(p)

        st.caption(f"{len(filtered)} post(s) across {len(by_account)} account(s)")

        for acct_handle in sorted(by_account.keys()):
            posts = by_account[acct_handle]
            with st.expander(f"{acct_handle} — {len(posts)} post(s)", expanded=False):
                cols_per_row = 3
                for row_start in range(0, len(posts), cols_per_row):
                    cols = st.columns(cols_per_row)
                    for col, post in zip(cols, posts[row_start : row_start + cols_per_row]):
                        with col:
                            st.markdown(
                                f"**{(post.get('title') or post.get('source_slug') or '?')[:50]}**"
                            )
                            st.caption(
                                f"{post.get('posted_at')} · "
                                f"{post.get('platform') or 'platform?'}"
                            )
                            vp = post.get("video_archive_path")
                            if vp and (PROJECT_ROOT / vp).exists():
                                st.video(str(PROJECT_ROOT / vp), width="stretch")
                            if post.get("hook"):
                                st.caption(f"Hook: *{post['hook']}*")
                            if post.get("posted_url"):
                                st.markdown(f"[Posted URL]({post['posted_url']})")
                            if post.get("post_caption"):
                                with st.expander("Caption"):
                                    st.code(post["post_caption"])
