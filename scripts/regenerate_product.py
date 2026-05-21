#!/usr/bin/env python3
"""
Regenerate a single product (or one variant of it) within an existing run,
using overridden prompts. Used by the Streamlit dashboard's per-variant
"Regenerate this variant" button.

Backend-aware: reads `video_backend` from the run's manifest and routes the
video step to Flow (Playwright) or the Veo API accordingly. Resolves the run
folder whether it's outputs/<run-id>/ (API) or outputs/<run-id>-flow/ (Flow).

Re-runs, for the targeted variant(s):
  1. Nano Banana restage   (only if --nano-prompt is given)
  2. Video animation       (always — Flow or Veo API per the manifest)

Overwrites existing files for the requested variant(s) and updates only the
matching entry inside manifest.json.

Usage (from the project root):
  python scripts/regenerate_product.py \\
    --run-id 20260519-1500-US-flow \\
    --product-id 1732268320365777584 \\
    --variant-label A \\
    --veo-prompt "slow dolly-in on the product, soft studio light" \\
    --hook "Cleans tile in seconds"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.generators.hooks import (  # noqa: E402
    generate_hook_and_caption,
    sanitize_prompt_for_rai,
)
from src.generators.nano_banana import restage_product  # noqa: E402
from src.posters.archive import load_accounts  # noqa: E402


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_settings() -> dict:
    with (PROJECT_ROOT / "config/settings.yaml").open() as f:
        return yaml.safe_load(f) or {}


def _resolve_run_root(run_id: str) -> Path:
    """Find the run folder. Accepts either the exact directory name or a bare
    run-id (in which case the -flow variant is tried too)."""
    candidates = [
        PROJECT_ROOT / "outputs" / run_id,
        PROJECT_ROOT / "outputs" / f"{run_id}-flow",
    ]
    for c in candidates:
        if (c / "manifest.json").exists():
            return c
    raise SystemExit(
        f"No manifest found for run {run_id!r} "
        f"(looked in {', '.join(str(c) for c in candidates)})"
    )


def _veo_api_model(settings: dict) -> str:
    gen = settings.get("generation", {})
    api_cfg = gen.get("api", {})
    backend = gen.get("video_backend", "veo_api_fast")
    if backend == "veo_api_light":
        return api_cfg.get("light_model", "veo-3.1-light")
    if backend == "veo_api_standard":
        return api_cfg.get("standard_model", "veo-3.0-generate-001")
    return api_cfg.get("fast_model", "veo-3.0-fast-generate-001")


def _flow_model(settings: dict) -> str:
    return (
        settings.get("generation", {}).get("flow", {}).get("model")
        or "veo-3.1-lite"
    )


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


def _variant_staged_path(entry: dict, variant: dict, staged_dir: Path) -> Path:
    """Resolve a variant's staged image path. Flow runs store it per-variant
    on the variant dict; older API runs store one shared path on the entry."""
    rel = variant.get("staged") or entry.get("staged")
    if rel:
        return PROJECT_ROOT / rel
    slug = entry.get("slug") or entry.get("id")
    low = str(variant.get("label") or "a").lower()
    return staged_dir / f"{slug}-{low}.png"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True,
                    help="Run folder name (e.g. 20260519-1500-US-flow) or bare run-id.")
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--variant-label", default=None,
                    help="Only this variant (e.g. 'A'). Omit to redo all variants.")
    ap.add_argument("--nano-prompt", default=None,
                    help="Override the Nano Banana prompt — triggers a re-restage.")
    ap.add_argument("--veo-prompt", default=None,
                    help="Override the video prompt for the targeted variant(s).")
    ap.add_argument("--hook", default=None,
                    help="Override the hook text for the targeted variant(s).")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")

    run_root = _resolve_run_root(args.run_id)
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    products = manifest.get("products", [])
    idx = next(
        (i for i, p in enumerate(products) if str(p.get("id")) == args.product_id),
        None,
    )
    if idx is None:
        raise SystemExit(f"Product id {args.product_id!r} not found in {manifest_path}")
    entry = products[idx]
    slug = entry.get("slug") or entry.get("id")

    settings = _load_settings()
    gen_cfg = settings.get("generation", {})
    image_model = gen_cfg.get("image_model", "gemini-2.5-flash-image")
    duration_s = int(gen_cfg.get("video_duration_seconds", 8))
    aspect_ratio = gen_cfg.get("video_aspect_ratio", "9:16")

    # Backend routing — driven by the run's own manifest, not settings.yaml,
    # so a regenerate always matches how the run was originally produced.
    backend = manifest.get("video_backend", "veo_api_fast")
    is_flow = backend == "flow_playwright"
    if is_flow:
        from src.generators.flow import generate_video as _gen_video, RAIBlocked
        video_model = _flow_model(settings)
        _log(f"[regen] backend=Flow model={video_model}")
    else:
        from src.generators.veo import generate_video as _gen_video
        RAIBlocked = ()  # type: ignore  (never raised by the API path)
        video_model = _veo_api_model(settings)
        _log(f"[regen] backend=Veo API model={video_model}")

    raw_dir = run_root / "raw"
    staged_dir = run_root / "staged"
    videos_dir = run_root / "videos"
    for d in (raw_dir, staged_dir, videos_dir):
        d.mkdir(parents=True, exist_ok=True)

    accounts = load_accounts()

    variants = entry.get("variants") or []
    if not variants:
        # Legacy single-variant entry.
        variants = [{
            "label": "A",
            "account": None,
            "video": entry.get("video"),
            "hook": entry.get("hook"),
            "post_caption": entry.get("post_caption"),
            "veo_prompt_used": entry.get("veo_prompt_used"),
            "staged": entry.get("staged"),
        }]
        entry["variants"] = variants

    if args.variant_label:
        targets = [v for v in variants if str(v.get("label")) == args.variant_label]
        if not targets:
            raise SystemExit(
                f"variant_label={args.variant_label!r} not found in product {slug}"
            )
    else:
        targets = variants

    raw_path = None  # lazy — only needed if we re-restage

    for v in targets:
        vlabel = str(v.get("label") or "x")
        staged_path = _variant_staged_path(entry, v, staged_dir)

        # 1. Re-restage this variant's image if a new Nano Banana prompt is given.
        if args.nano_prompt:
            if raw_path is None:
                raw_path = _ensure_raw(entry, raw_dir)
            v["nano_prompt_used"] = args.nano_prompt
            if staged_path.exists():
                staged_path.unlink()
            _log(f"[regen] {slug} {vlabel}: restage via {image_model} @{aspect_ratio}")
            t0 = time.time()
            restage_product(
                raw_path, args.nano_prompt, staged_path,
                model=image_model, aspect_ratio=aspect_ratio,
            )
            v["staged"] = str(staged_path.relative_to(PROJECT_ROOT))
            _log(f"[regen] {slug} {vlabel}: restage done in {time.time()-t0:.1f}s")
            # Refresh hook/caption to match the new scene unless explicitly set.
            if args.hook is None:
                try:
                    t = generate_hook_and_caption(
                        {"title": entry.get("title"), "extras": entry.get("extras") or {}}
                    )
                    v["hook"] = t["hook"]
                    v["post_caption"] = t["caption"]
                except Exception as e:
                    _log(f"[regen] {slug} {vlabel}: hook refresh failed: {e}")

        if not staged_path.exists():
            raise SystemExit(
                f"No staged image at {staged_path} and no --nano-prompt to recreate it."
            )

        if args.hook is not None:
            v["hook"] = args.hook

        # 2. Regenerate the video.
        if args.veo_prompt is not None:
            v["veo_prompt_used"] = args.veo_prompt
        veo_prompt = v.get("veo_prompt_used") or ""
        if not veo_prompt:
            raise SystemExit(f"No video prompt for {slug} {vlabel}.")

        video_path = videos_dir / f"{slug}-{vlabel.lower()}.mp4"
        if video_path.exists():
            video_path.unlink()
        _log(f"[regen] {slug} {vlabel}: animate via {video_model}")
        t0 = time.time()
        try:
            try:
                _gen_video(
                    staged_path, veo_prompt, video_path,
                    duration_seconds=duration_s,
                    aspect_ratio=aspect_ratio,
                    model=video_model,
                )
            except RAIBlocked as e:  # Flow only — sanitize + one retry.
                _log(f"[regen] {slug} {vlabel}: RAI — sanitizing + retrying")
                sanitized = sanitize_prompt_for_rai(
                    veo_prompt, product_name=v.get("product_name", "product"),
                )
                _gen_video(
                    staged_path, sanitized, video_path,
                    duration_seconds=duration_s,
                    aspect_ratio=aspect_ratio,
                    model=video_model,
                )
                v["prompt_sanitized"] = sanitized
                v["rai_retry"] = True
            v["video"] = str(video_path.relative_to(PROJECT_ROOT))
            v.pop("error", None)
            _log(f"[regen] {slug} {vlabel}: done in {time.time()-t0:.1f}s")
        except Exception as e:
            _log(f"[regen] {slug} {vlabel}: FAILED — {e}")
            v["error"] = str(e)

    # Recompute product status.
    entry["status"] = "ok" if any(v.get("video") for v in variants) else "error"
    products[idx] = entry
    manifest["products"] = products
    manifest_path.write_text(json.dumps(manifest, indent=2))
    _log(f"[regen] done — manifest updated at {manifest_path}")

    # Refresh the clean Generations/ copy so the regenerated clip shows there.
    try:
        from src.exporter import export_to_generations
        export_to_generations(manifest_path)
    except Exception as e:
        _log(f"[regen] export to Generations/ failed: {e}")


if __name__ == "__main__":
    main()
