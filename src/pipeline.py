"""
Pipeline orchestrator — reads outputs/_selection/selection.json, downloads
each product's cover image, restages it with Nano Banana, animates it with
Veo, and writes per-run output + a manifest.json.

Designed to be called from the Streamlit Generate button as a subprocess so
its stdout can be streamed to the UI.

Resumable: skips steps where the output file already exists.
Cost-gated: aborts before each API call if estimated total exceeds
MAX_RUN_COST_USD from .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from slugify import slugify


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.generators.hooks import generate_hook_and_caption  # noqa: E402
from src.generators.nano_banana import restage_product  # noqa: E402
from src.generators.veo import generate_video  # noqa: E402
from src.posters.archive import load_accounts  # noqa: E402


VARIANTS_PER_PRODUCT = 2


SELECTION_PATH = PROJECT_ROOT / "outputs/_selection/selection.json"
SETTINGS_PATH = PROJECT_ROOT / "config/settings.yaml"
PROMPT_NANO = PROJECT_ROOT / "prompts/nano_banana.txt"
PROMPT_VEO = PROJECT_ROOT / "prompts/veo.txt"


def _load_settings() -> dict:
    with SETTINGS_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _download_cover(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def _slug(product: dict) -> str:
    base = f"{product.get('id', '')}-{product.get('title', 'product')}"
    return slugify(base)[:80] or "product"


def _ext_from_url(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if lower.endswith(ext):
            return ext
    return ".png"


def _estimated_cost(
    n_products: int,
    settings: dict,
    variants_per_product: int = VARIANTS_PER_PRODUCT,
    backend_override: str | None = None,
) -> tuple[float, float, float]:
    """Returns (image_cost, video_cost, total). Image cost is one Nano Banana
    call per product; video cost is one Veo call per VARIANT per product.

    Pass `backend_override` to compute the cost for a backend other than the
    one in settings.yaml — used when the dashboard's per-run radio picks a
    backend that differs from the file's default."""
    gen = settings.get("generation", {})
    costs = settings.get("costs", {})
    backend = backend_override or gen.get("video_backend", "veo_api_fast")
    img_each = float(costs.get("image_per_call_usd", 0.04))
    vid_each = float(
        costs.get("video_per_clip_usd", {}).get(backend, 1.20)
    )
    img_total = img_each * n_products
    vid_total = vid_each * n_products * variants_per_product
    return img_total, vid_total, img_total + vid_total


def _pick_account_for_variant(
    accounts: list[dict], variant_idx: int
) -> dict | None:
    """
    Map variant index → target account. 2 generations is the contract:
      - 2+ accounts configured: variants[0]→accounts[0], variants[1]→accounts[1]
      - 1 account: both variants point at it (A/B testing same audience)
      - 0 accounts: returns None (variant rendered with no account label)
    """
    if not accounts:
        return None
    if variant_idx < len(accounts):
        return accounts[variant_idx]
    return accounts[0]


def run(
    run_id: str | None = None,
    output_root: Path | None = None,
    skip_video: bool = False,
    region: str | None = None,
    backend: str | None = None,
) -> Path:
    load_dotenv(PROJECT_ROOT / ".env")

    if not SELECTION_PATH.exists():
        raise SystemExit(
            f"No selection.json at {SELECTION_PATH}. Click 'Save selection' "
            f"in the dashboard first."
        )
    picks = json.loads(SELECTION_PATH.read_text())
    if not picks:
        raise SystemExit("selection.json is empty — nothing to generate.")

    settings = _load_settings()
    gen = settings.get("generation", {})
    image_model = gen.get("image_model", "gemini-2.5-flash-image")
    # CLI/env override > settings.yaml default. Lets the dashboard's per-run
    # backend radio pick veo_api_light/fast/standard without rewriting the file.
    video_backend = backend or gen.get("video_backend", "veo_api_fast")
    api_cfg = gen.get("api", {})
    if video_backend == "veo_api_light":
        video_model = api_cfg.get("light_model", "veo-3.1-light")
    elif video_backend == "veo_api_standard":
        video_model = api_cfg.get("standard_model", "veo-3.0-generate-001")
    else:
        # veo_api_fast or any unknown → safe default
        video_model = api_cfg.get("fast_model", "veo-3.0-fast-generate-001")
    duration_s = int(gen.get("video_duration_seconds", 8))
    aspect_ratio = gen.get("video_aspect_ratio", "9:16")

    img_cost, vid_cost, total_cost = _estimated_cost(
        len(picks), settings, backend_override=video_backend
    )
    cap = float(os.environ.get("MAX_RUN_COST_USD", "10"))
    _log(
        f"[pipeline] {len(picks)} products | "
        f"est image ${img_cost:.2f} + video ${vid_cost:.2f} = ${total_cost:.2f} | "
        f"cap ${cap:.2f}"
    )
    if total_cost > cap:
        raise SystemExit(
            f"Estimated cost ${total_cost:.2f} exceeds MAX_RUN_COST_USD ${cap:.2f}. "
            f"Reduce selection or raise the cap in .env."
        )

    nano_prompt_template = PROMPT_NANO.read_text().strip()
    veo_prompt = PROMPT_VEO.read_text().strip()

    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = Path(output_root or PROJECT_ROOT / "outputs") / run_id
    raw_dir = output_root / "raw"
    staged_dir = output_root / "staged"
    videos_dir = output_root / "videos"
    for d in (raw_dir, staged_dir, videos_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Region preference order: explicit arg > selection meta sidecar > "?"
    if not region:
        meta_path = SELECTION_PATH.parent / "meta.json"
        if meta_path.exists():
            try:
                region = (json.loads(meta_path.read_text()) or {}).get("region")
            except Exception:
                region = None
    region = (region or "?").upper()

    manifest_path = output_root / "manifest.json"
    manifest: dict = {
        "run_id": run_id,
        "region": region,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "image_model": image_model,
        "video_model": video_model,
        "video_backend": video_backend,
        "products": [],
    }

    accounts = load_accounts()
    _log(
        f"[pipeline] run_id={run_id} output={output_root}; "
        f"{len(accounts)} account(s) configured, {VARIANTS_PER_PRODUCT} variant(s) per product"
    )

    for idx, product in enumerate(picks, start=1):
        slug = _slug(product)
        title = (product.get("title") or "")[:60]
        photo_urls = product.get("photo_urls") or []
        if not photo_urls:
            _log(f"[pipeline] {idx}/{len(picks)} {slug}: no photo URL, skipping")
            continue

        entry: dict = {
            "id": product.get("id"),
            "slug": slug,
            "title": title,
            "kalodata_url": product.get("kalodata_url"),
            "price_usd": product.get("price_usd"),
            "category_path": product.get("category_path"),
            "variants": [],
        }
        try:
            ext = _ext_from_url(photo_urls[0])
            raw_path = raw_dir / f"{slug}{ext}"
            _log(f"[pipeline] {idx}/{len(picks)} {slug}: download cover")
            _download_cover(photo_urls[0], raw_path)
            entry["raw"] = str(raw_path.relative_to(PROJECT_ROOT))

            # Pick the retail environment + product noun once (shared across
            # variants since the staged image is shared). Use no account
            # context for this call — these are functions of the product,
            # not the audience.
            try:
                _log(f"[pipeline] {idx}/{len(picks)} {slug}: pick env + product_name via Gemini")
                base_text = generate_hook_and_caption(product)
                entry["environment"] = base_text["environment"]
                entry["product_name"] = base_text["product_name"]
                _log(
                    f"[pipeline] {idx}/{len(picks)} {slug}: "
                    f"env = {base_text['environment']!r}, "
                    f"product = {base_text['product_name']!r}"
                )
            except Exception as e:
                _log(f"[pipeline] {idx}/{len(picks)} {slug}: env/product pick failed: {e}")
                entry["environment"] = "a curated specialty retail store"
                entry["product_name"] = "product"

            nano_prompt = nano_prompt_template.replace(
                "{environment}", entry["environment"]
            )
            entry["nano_prompt_used"] = nano_prompt

            staged_path = staged_dir / f"{slug}.png"
            if staged_path.exists() and staged_path.stat().st_size > 0:
                _log(f"[pipeline] {idx}/{len(picks)} {slug}: staged image exists, skipping restage")
            else:
                _log(f"[pipeline] {idx}/{len(picks)} {slug}: restage via {image_model} @{aspect_ratio}")
                t0 = time.time()
                restage_product(
                    raw_path, nano_prompt, staged_path,
                    model=image_model, aspect_ratio=aspect_ratio,
                )
                _log(f"[pipeline] {idx}/{len(picks)} {slug}: restage done in {time.time()-t0:.1f}s")
            entry["staged"] = str(staged_path.relative_to(PROJECT_ROOT))

            # Generate N variants. Each gets its own Gemini-written hook +
            # caption (tailored to the assigned account) and its own Veo call.
            variant_labels = ["A", "B", "C", "D"][:VARIANTS_PER_PRODUCT]
            for vi, vlabel in enumerate(variant_labels):
                account = _pick_account_for_variant(accounts, vi)
                vslug = f"{slug}-{vlabel.lower()}"

                # Hook + caption per variant.
                try:
                    _log(
                        f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                        f"hook+caption (account={account['handle'] if account else 'unassigned'})"
                    )
                    vt = generate_hook_and_caption(product, account=account)
                    v_hook, v_caption = vt["hook"], vt["caption"]
                except Exception as e:
                    _log(
                        f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                        f"hook gen failed: {e}"
                    )
                    v_hook = (title or slug)[:40]
                    v_caption = title or slug

                # Substitute the concrete product noun into the Veo prompt so
                # the motion model doesn't get confused by a generic "product"
                # reference (Veo otherwise sometimes animates the wrong object
                # in the frame).
                veo_prompt_for_variant = veo_prompt.replace(
                    "{product}", entry["product_name"]
                )

                variant: dict = {
                    "label": vlabel,
                    "account": account["handle"] if account else None,
                    "platform": account.get("platform") if account else None,
                    "hook": v_hook,
                    "post_caption": v_caption,
                    "veo_prompt_used": veo_prompt_for_variant,
                }

                if skip_video:
                    _log(
                        f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                        f"skip_video=True, no clip"
                    )
                    variant["video"] = None
                else:
                    video_path = videos_dir / f"{vslug}.mp4"
                    if video_path.exists() and video_path.stat().st_size > 0:
                        _log(
                            f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                            f"clip exists, skipping"
                        )
                    else:
                        _log(
                            f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                            f"animate via {video_model} (1-3 min)"
                        )
                        t0 = time.time()
                        try:
                            generate_video(
                                staged_path, veo_prompt_for_variant, video_path,
                                duration_seconds=duration_s,
                                aspect_ratio=aspect_ratio,
                                model=video_model,
                            )
                            _log(
                                f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                                f"clip done in {time.time()-t0:.1f}s"
                            )
                        except Exception as e:
                            _log(
                                f"[pipeline] {idx}/{len(picks)} {slug} variant {vlabel}: "
                                f"Veo failed: {e}"
                            )
                            variant["error"] = str(e)
                    variant["video"] = (
                        str(video_path.relative_to(PROJECT_ROOT))
                        if video_path.exists() else None
                    )

                entry["variants"].append(variant)
                # Persist incrementally so a crash mid-run doesn't lose the
                # variants we already finished.
                manifest_path.write_text(
                    json.dumps({**manifest, "products": manifest["products"] + [entry]}, indent=2)
                )

            entry["status"] = "ok"
        except Exception as e:
            _log(f"[pipeline] {idx}/{len(picks)} {slug}: ERROR {e}")
            entry["status"] = "error"
            entry["error"] = str(e)

        manifest["products"].append(entry)
        manifest_path.write_text(json.dumps(manifest, indent=2))

    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    manifest_path.write_text(json.dumps(manifest, indent=2))

    n_ok = sum(1 for p in manifest["products"] if p.get("status") == "ok")
    _log(f"[pipeline] done — {n_ok}/{len(picks)} succeeded → {output_root}")
    return manifest_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--skip-video", action="store_true",
                    help="Restage with Nano Banana only — skip Veo for fast/cheap testing.")
    ap.add_argument("--region", default=None,
                    help="Country code (US, UK, ...) — stamped into manifest for the date browser.")
    ap.add_argument("--backend", default=None,
                    choices=["veo_api_fast", "veo_api_light", "veo_api_standard"],
                    help="Override settings.yaml :: generation.video_backend for this run.")
    args = ap.parse_args()
    run(
        run_id=args.run_id,
        output_root=Path(args.output_root) if args.output_root else None,
        skip_video=args.skip_video,
        region=args.region,
        backend=args.backend,
    )


if __name__ == "__main__":
    main()
