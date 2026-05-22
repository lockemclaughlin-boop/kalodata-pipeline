#!/usr/bin/env python3
"""
End-to-end Phase 1 smoke test. Runs one Kalodata search with hardcoded filters,
prints the products found, writes an HTML preview that loads images straight
from Kalodata's CDN (no bytes saved locally), and optionally downloads photos.

Usage from the project root:
    python scripts/scrape_test.py                # search + open HTML preview
    python scripts/scrape_test.py --no-open      # write preview but don't open browser
    python scripts/scrape_test.py --download 3   # also download top 3 products' photos

Output:
    Console: a summary table of products
    File:    outputs/_smoke/products.json
    File:    outputs/_smoke/preview.html  (references remote image URLs)
    Photos:  outputs/_smoke/<product-slug>/*.jpg (only if --download)
"""

import argparse
import html
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scraper.kalodata import (
    Filters,
    fetch_product_assets,
    search_products,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="US")
    ap.add_argument(
        "--category",
        default="",
        help="Kalodata category title to filter on. Empty string = no category filter.",
    )
    ap.add_argument(
        "--time-window",
        default="last_7_days",
        choices=["last_24_hours", "last_7_days", "last_30_days"],
    )
    ap.add_argument("--min-gmv", type=float, default=10000)
    ap.add_argument("--min-sales", type=int, default=100)
    ap.add_argument("--min-growth", type=float, default=0)
    ap.add_argument("--min-creators", type=int, default=0,
                    help="Creator Number filter — minimum creators promoting (0 = no filter).")
    ap.add_argument("--max-results", type=int, default=50)
    ap.add_argument(
        "--out-dir",
        default="outputs/_smoke",
        help="Directory to write products.json and (optional) photos into.",
    )
    ap.add_argument(
        "--download",
        type=int,
        default=0,
        help="Download photos for the top N products (0 = skip downloads, the default)",
    )
    ap.add_argument("--headless", action="store_true", help="Run without showing the browser")
    ap.add_argument(
        "--no-open",
        action="store_true",
        help="Write the HTML preview but don't auto-open it in the browser",
    )
    args = ap.parse_args()

    # The login flow stores the session in a persistent Chrome profile, not a
    # storage-state JSON. Check for that profile directory.
    storage = Path(".auth/kalodata.json")  # legacy arg, still passed for back-compat
    profile_dir = Path(".auth/chrome-profile")
    if not profile_dir.exists():
        sys.exit(
            f"No Kalodata session at {profile_dir}. Run scripts/login_kalodata.py first."
        )

    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    filters = Filters(
        region=args.region,
        category=args.category if args.category else None,
        time_window=args.time_window,
        min_gmv_usd=args.min_gmv,
        min_sales=args.min_sales,
        min_growth_pct=args.min_growth,
        min_creators=args.min_creators,
    )
    print(f"[smoke] filters: {filters}")

    products = search_products(
        filters=filters,
        storage_state_path=storage,
        max_results=args.max_results,
        headed=not args.headless,
    )

    print(f"\n[smoke] found {len(products)} products")
    print(f"{'#':>3}  {'GMV':>10}  {'Sales':>7}  {'Growth':>7}  Title")
    print("-" * 80)
    for i, p in enumerate(products[:30]):
        gmv = f"${p.gmv_usd:,.0f}" if p.gmv_usd else "—"
        sales = f"{p.units_sold:,}" if p.units_sold else "—"
        growth = f"{p.growth_pct:+.0f}%" if p.growth_pct else "—"
        print(f"{i:>3}  {gmv:>10}  {sales:>7}  {growth:>7}  {p.title[:50]}")

    json_path = out_root / "products.json"
    json_path.write_text(json.dumps([p.to_dict() for p in products], indent=2))
    print(f"\n[smoke] wrote {json_path}")

    preview_path = out_root / "preview.html"
    preview_path.write_text(_render_preview_html(products, filters))
    print(f"[smoke] wrote {preview_path} (opens images directly from Kalodata CDN)")
    if not args.no_open and products:
        webbrowser.open(preview_path.resolve().as_uri())

    if args.download > 0:
        print(f"\n[smoke] downloading photos for top {args.download} products...")
        for p in products[: args.download]:
            saved = fetch_product_assets(
                product=p,
                storage_state_path=storage,
                dest_dir=out_root,
                headed=not args.headless,
            )
            print(f"  - {p.title[:40]}: {len(saved)} photos -> {out_root / p.slug()}")

    print("\n[smoke] done.")


def _render_preview_html(products, filters) -> str:
    """
    Build a self-contained HTML page that shows each product as a card with the
    cover image, title, and stats. The <img> tags point at Kalodata's CDN URLs,
    so the browser fetches the images on view — no bytes hit local disk.
    """
    cards = []
    for i, p in enumerate(products):
        img = html.escape(p.photo_urls[0]) if p.photo_urls else ""
        title = html.escape(p.title or "(no title)")
        url = html.escape(p.kalodata_url)
        gmv = f"${p.gmv_usd:,.0f}" if p.gmv_usd else "—"
        sales = f"{p.units_sold:,}" if p.units_sold else "—"
        growth = f"{p.growth_pct:+.0f}%" if p.growth_pct is not None else "—"
        cards.append(f"""
        <div class="card">
          <a href="{url}" target="_blank" rel="noopener">
            <img src="{img}" loading="lazy" referrerpolicy="no-referrer" alt="">
          </a>
          <div class="meta">
            <div class="num">#{i + 1}</div>
            <div class="title"><a href="{url}" target="_blank" rel="noopener">{title}</a></div>
            <div class="stats">GMV {gmv} · {sales} sold · {growth}</div>
          </div>
        </div>""")
    filter_summary = html.escape(
        f"region={filters.region} · category={filters.category} · "
        f"window={filters.time_window} · min_gmv=${filters.min_gmv_usd:,.0f} · "
        f"min_sales={filters.min_sales}"
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Kalodata preview</title>
<style>
  body {{ font: 14px -apple-system, system-ui, sans-serif; margin: 24px; background: #fafafa; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .sub {{ color: #666; margin-bottom: 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border: 1px solid #e5e5e5; border-radius: 8px; overflow: hidden; }}
  .card img {{ width: 100%; aspect-ratio: 1 / 1; object-fit: cover; display: block; background: #f0f0f0; }}
  .meta {{ padding: 10px 12px; }}
  .num {{ color: #999; font-size: 12px; }}
  .title a {{ color: #111; text-decoration: none; font-weight: 500; }}
  .title a:hover {{ text-decoration: underline; }}
  .stats {{ color: #666; font-size: 12px; margin-top: 4px; }}
</style></head><body>
<h1>Kalodata preview — {len(products)} products</h1>
<div class="sub">{filter_summary}</div>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


if __name__ == "__main__":
    main()
