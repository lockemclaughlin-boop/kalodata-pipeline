#!/usr/bin/env python3
"""
Regenerate a single product within an existing run using overridden prompts.
Used by the Streamlit dashboard's per-product 'Regenerate' button.

Re-runs:
  1. Nano Banana restage (if --regen-image)
  2. Veo animation     (if --regen-video)
  3. ffmpeg caption burn (if --regen-caption)

All three are on by default. Overwrites existing files for the requested
product. Updates the matching entry inside manifest.json — every other
product in the manifest is left untouched.

Usage (from the project root):
  python scripts/regenerate_product.py \\
    --run-id 20260516-013005 \\
    --product-id 1732268320365777584 \\
    --nano-prompt "put the product on a polished marble counter ..." \\
    --hook "Cleans tile in seconds"
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.generators.hooks import generate_hook_and_caption  # noqa: E402
from src.generators.nano_banana import restage_product  # noqa: E402
from src.generators.veo import generate_video  # noqa: E402
from src.posters.archive import load_accounts  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_settings() -> dict:
    with (PROJECT_ROOT / "config/settings.yaml").open() as f:
        return yaml.safe_load(f) or {}


def _video_model_from_settings(settings: dict) -> str:
    gen = settings.get("generation", {})
    api_cfg = gen.get("api", {})
    backend = gen.get("video_backend", "veo_api_fast")
    if backend == "veo_api_light":
        return api_cfg.get("light_model", "veo-3.1-light")
    if backend == "veo_api_standard":
        return api_cfg.get("standard_model", "veo-3.0-generate-001")
    return api_cfg.get("fast_model", "veo-3.0-fast-generate-001")


def _ensure_raw(entry: dict, raw_dir: Path) -> Path:
    """Make sure the raw cover image exists on disk; redownload if missing."""
    raw_rel = entry.get("raw")
    if raw_rel:
        path = PROJECT_ROOT / raw_rel
        if path.exists() and path.stat().st_size > 0:
            return path
    photo_urls = entry.get("photo_urls") or []
    if not photo_urls:
        raise RuntimeError(f"No raw image and no photo_urls for {entry.get('slug')}")
    slug = entry.get("slug") or entry.get("id")
    dest = raw_dir / f"{slug}.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        r = client.get(photo_urls[0])
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def _pick_account_for_variant(accounts: list[dict], variant_idx: int) -> dict | None:
    if not accounts:
        return None
    if variant_idx < len(accounts):
        return accounts[variant_idx]
    return accounts[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--variant-label", default=None,
                    help="If set, only this one variant (e.g. 'A' or 'B') is regenerated. "
                         "If omitted, all variants under the product are regenerated.")
    ap.add_argument("--nano-prompt", default=None,
                    help="Override the shared Nano Banana prompt. Triggers re-restage.")
    ap.add_argument("--veo-prompt", default=None,
                    help="Override Veo prompt for the targeted variant(s).")
    ap.add_argument("--hook", default=None,
                    help="Override the hook text for the targeted variant(s).")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    run_root = PROJECT_ROOT / "outputs" / args.run_id
    manifest_path = run_root / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"No manifest at {manifest_path}")

    manifest = json.loads(manifest_path.read_text())
    products = manifest.get("products", [])
    idx = next(
        (i for i, p in enumerate(products) if str(p.get("id")) == args.product_id),
        None,
    )
    if idx is None:
        raise SystemExit(f"Product id {args.product_id!r} not found.")
    entry = products[idx]
    slug = entry.get("slug") or entry.get("id")

    settings = _load_settings()
    gen_cfg = settings.get("generation", {})
    image_model = gen_cfg.get("image_model", "gemini-2.5-flash-image")
    video_model = _video_model_from_settings(settings)
    duration_s = int(gen_cfg.get("video_duration_seconds", 8))
    aspect_ratio = gen_cfg.get("video_aspect_ratio", "9:16")

    raw_dir = run_root / "raw"
    staged_dir = run_root / "staged"
    videos_dir = run_root / "videos"
    for d in (raw_dir, staged_dir, videos_dir):
        d.mkdir(parents=True, exist_ok=True)

    raw_path = _ensure_raw(entry, raw_dir)
    accounts = load_accounts()

    # 1. Re-restage if a new Nano Banana prompt was supplied.
    if args.nano_prompt:
        nano_prompt = args.nano_prompt
        entry["nano_prompt_used"] = nano_prompt
        staged_path = staged_dir / f"{slug}.png"
        if staged_path.exists():
            staged_path.unlink()
        _log(f"[regen] {slug}: restage via {image_model} @{aspect_ratio}")
        t0 = time.time()
        restage_product(
            raw_path, nano_prompt, staged_path,
            model=image_model, aspect_ratio=aspect_ratio,
        )
        _log(f"[regen] {slug}: restage done in {time.time()-t0:.1f}s")
        entry["staged"] = str(staged_path.relative_to(PROJECT_ROOT))
    else:
        staged_path = PROJECT_ROOT / entry.get("staged", "")
        if not staged_path.exists():
            raise SystemExit("No staged image and no --nano-prompt to recreate it.")

    # 2. Pick which variants to redo. If a label is specified, only that one.
    variants = entry.get("variants") or []
    if not variants:
        # Legacy entry: synthesise a single variant from top-level fields so
        # the script still works on pre-multi-variant runs.
        variants = [{
            "label": "A",
            "account": None,
            "platform": None,
            "video": entry.get("video"),
            "hook": entry.get("hook"),
            "post_caption": entry.get("post_caption"),
            "veo_prompt_used": entry.get("veo_prompt_used"),
        }]
        entry["variants"] = variants

    if args.variant_label:
        targets = [v for v in variants if str(v.get("label")) == args.variant_label]
        if not targets:
            raise SystemExit(
                f"variant_label={args.variant_label!r} not found in product {slug}"
            )
    else:
        targets = variants  # all of them

    veo_prompt_default = (
        variants[0].get("veo_prompt_used")
        or entry.get("veo_prompt_used")
        or ""
    )

    for v in targets:
        vlabel = v.get("label") or "x"
        v_idx = next((i for i, vv in enumerate(variants) if vv is v), 0)
        account = (
            next((a for a in accounts if a["handle"] == v.get("account")), None)
            or _pick_account_for_variant(accounts, v_idx)
        )

        # If the user supplied a hook/veo override, apply it. Otherwise, if we
        # re-restaged (image changed) we also regenerate hook+caption from
        # Gemini so the copy matches the new scene.
        if args.hook is not None:
            v["hook"] = args.hook
        elif args.nano_prompt:
            try:
                t = generate_hook_and_caption(
                    {"title": entry.get("title"), "extras": entry.get("extras") or {}},
                    account=account,
                )
                v["hook"] = t["hook"]
                v["post_caption"] = t["caption"]
            except Exception as e:
                _log(f"[regen] {slug} variant {vlabel}: hook gen failed: {e}")

        if args.veo_prompt is not None:
            v["veo_prompt_used"] = args.veo_prompt
        veo_prompt = v.get("veo_prompt_used") or veo_prompt_default
        if not veo_prompt:
            raise SystemExit("No Veo prompt available (manifest or override).")

        video_path = videos_dir / f"{slug}-{vlabel.lower()}.mp4"
        if video_path.exists():
            video_path.unlink()
        _log(f"[regen] {slug} variant {vlabel}: animate via {video_model} (1-3 min)")
        t0 = time.time()
        try:
            generate_video(
                staged_path, veo_prompt, video_path,
                duration_seconds=duration_s,
                aspect_ratio=aspect_ratio,
                model=video_model,
            )
            v["video"] = str(video_path.relative_to(PROJECT_ROOT))
            v.pop("error", None)
            _log(f"[regen] {slug} variant {vlabel}: done in {time.time()-t0:.1f}s")
        except Exception as e:
            _log(f"[regen] {slug} variant {vlabel}: Veo failed: {e}")
            v["error"] = str(e)

    products[idx] = entry
    manifest["products"] = products
    manifest_path.write_text(json.dumps(manifest, indent=2))
    _log(f"[regen] done — manifest updated at {manifest_path}")


if __name__ == "__main__":
    main()
