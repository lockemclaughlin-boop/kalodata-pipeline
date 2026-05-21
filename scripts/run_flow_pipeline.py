#!/usr/bin/env python3
"""
Flow + Playwright pipeline runner — one Flow project per product.

A SEPARATE entry point from src/pipeline.py (Veo API). Reads the same
outputs/_selection/selection.json the dashboard writes, generates videos via
Flow's UI, and writes to outputs/<run-id>-flow/.

Per-product structure (the contract):
  - Two restaged images per product, each in a different retail scene.
  - One video generated from each image → two videos.
  - Both images' clips go into ONE Flow project (one project per product).
  - Products run concurrently in windows of `max_concurrent`.

Pipeline phases:
  A) Sequential prep per product
       - Download cover
       - For each of 2 variants: Gemini hook/caption/environment/product_name,
         then Nano Banana restage into that variant's scene
  B/C) Windowed submit + collect
       - For a window of up to max_concurrent products:
           pass 1: open a project per product, submit clip A
           pass 2: collect clip A (sanitize+retry on RAI), submit clip B
           pass 3: collect clip B (sanitize+retry on RAI), close project
       - Clip A's of all windowed products generate concurrently on Veo's
         backend; same for clip B's. Each product keeps both clips in one
         project, collected in submission order so A/B labels stay correct.

Usage from project root:
    python scripts/run_flow_pipeline.py
    python scripts/run_flow_pipeline.py --run-id flow_smoke
    python scripts/run_flow_pipeline.py --max-concurrent 5
    python scripts/run_flow_pipeline.py --skip-video  # Nano Banana only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.generators import flow as flow_backend  # noqa: E402
from src.generators.hooks import (  # noqa: E402
    generate_hook_and_caption,
    sanitize_prompt_for_rai,
)
from src.generators.nano_banana import restage_product  # noqa: E402
from src.pipeline import (  # noqa: E402
    PROMPT_NANO, PROMPT_VEO, SELECTION_PATH,
    VARIANTS_PER_PRODUCT, _download_cover, _ext_from_url,
    _load_settings, _log, _pick_account_for_variant, _slug,
)
from src.posters.archive import load_accounts  # noqa: E402


# Small sleep between back-to-back submits so we don't slam Flow's queue.
SUBMIT_THROTTLE_S = 3.0


def _resolve_flow_model(settings: dict) -> str:
    return (
        settings.get("generation", {}).get("flow", {}).get("model")
        or "veo-3.1-lite"
    )


def _open_flow_context_or_die(p, headed: bool = True):
    """Open the persistent-profile context and confirm we're signed in."""
    context = flow_backend._open_context(p, headed=headed)
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(flow_backend.FLOW_URL, wait_until="domcontentloaded")
    try:
        page.get_by_role("button", name="New project").wait_for(timeout=20_000)
    except Exception as e:
        try:
            context.close()
        except Exception:
            pass
        raise SystemExit(
            "Flow is not signed in. Run: python scripts/login_flow.py"
        ) from e
    try:
        page.close()  # drop the home tab; project tabs are opened per product
    except Exception:
        pass
    return context


def _prep_product(
    idx: int,
    total: int,
    product: dict,
    accounts: list,
    nano_prompt_template: str,
    veo_prompt: str,
    image_model: str,
    aspect_ratio: str,
    raw_dir: Path,
    staged_dir: Path,
) -> dict:
    """Phase A — prep one product. Downloads the cover, then for each of the
    VARIANTS_PER_PRODUCT variants runs its own Gemini hook/env call + its own
    Nano Banana restage into a different retail scene. Each variant therefore
    carries its own staged image."""
    slug = _slug(product)
    title = (product.get("title") or "")[:60]
    photo_urls = product.get("photo_urls") or []
    entry: dict = {
        "id": product.get("id"),
        "slug": slug,
        "title": title,
        "kalodata_url": product.get("kalodata_url"),
        "price_usd": product.get("price_usd"),
        "category_path": product.get("category_path"),
        "variants": [],
    }
    if not photo_urls:
        _log(f"[flow-pipeline] {idx}/{total} {slug}: no photo URL, skipping")
        entry["status"] = "error"
        entry["error"] = "no_photo_url"
        return entry

    ext = _ext_from_url(photo_urls[0])
    raw_path = raw_dir / f"{slug}{ext}"
    _log(f"[flow-pipeline] {idx}/{total} {slug}: download cover")
    _download_cover(photo_urls[0], raw_path)
    entry["raw"] = str(raw_path.relative_to(PROJECT_ROOT))

    variant_labels = ["A", "B", "C", "D"][:VARIANTS_PER_PRODUCT]
    for vi, vlabel in enumerate(variant_labels):
        account = _pick_account_for_variant(accounts, vi)
        low = vlabel.lower()
        # One Gemini call per variant → its own hook, caption, environment,
        # product_name. The environment differs per variant, so each variant's
        # restage lands in a different retail scene.
        try:
            vt = generate_hook_and_caption(product, account=account)
            v_hook = vt["hook"]
            v_caption = vt["caption"]
            v_env = vt["environment"]
            v_product = vt["product_name"]
        except Exception as e:
            _log(f"[flow-pipeline] {idx}/{total} {slug} {vlabel}: gemini call failed: {e}")
            v_hook = (title or slug)[:40]
            v_caption = title or slug
            v_env = "a curated specialty retail store"
            v_product = "product"

        nano_prompt = nano_prompt_template.replace("{environment}", v_env)
        staged_path = staged_dir / f"{slug}-{low}.png"
        if staged_path.exists() and staged_path.stat().st_size > 0:
            _log(f"[flow-pipeline] {idx}/{total} {slug} {vlabel}: staged image cached")
        else:
            _log(f"[flow-pipeline] {idx}/{total} {slug} {vlabel}: restage → {v_env!r}")
            t0 = time.time()
            try:
                restage_product(
                    raw_path, nano_prompt, staged_path,
                    model=image_model, aspect_ratio=aspect_ratio,
                )
                _log(f"[flow-pipeline] {idx}/{total} {slug} {vlabel}: "
                     f"restage done in {time.time()-t0:.1f}s")
            except Exception as e:
                _log(f"[flow-pipeline] {idx}/{total} {slug} {vlabel}: restage failed: {e}")

        veo_prompt_for_variant = veo_prompt.replace("{product}", v_product)
        entry["variants"].append({
            "label": vlabel,
            "account": account["handle"] if account else None,
            "platform": account.get("platform") if account else None,
            "hook": v_hook,
            "post_caption": v_caption,
            "environment": v_env,
            "product_name": v_product,
            "nano_prompt_used": nano_prompt,
            "staged": str(staged_path.relative_to(PROJECT_ROOT)),
            "veo_prompt_used": veo_prompt_for_variant,
        })
    return entry


def _persist(manifest: dict, manifest_path: Path) -> None:
    manifest_path.write_text(json.dumps(manifest, indent=2))


def _collect_with_rai_retry(
    project: dict,
    variant: dict,
    dest: Path,
    manifest: dict,
    manifest_path: Path,
) -> None:
    """Collect one clip for `variant` from an open project. On RAI rejection,
    sanitize the prompt via Gemini, resubmit into the SAME project, collect
    again. Mutates `variant` with video / rai_retry / prompt_sanitized / error."""
    tag = project.get("tag", "")
    try:
        flow_backend.collect_one_from_project(project, dest)
        variant["video"] = str(dest.relative_to(PROJECT_ROOT))
        _log(f"[flow-pipeline] {tag} {variant['label']}: clip done")
        _persist(manifest, manifest_path)
        return
    except flow_backend.RAIBlocked as e:
        _log(f"[flow-pipeline] {tag} {variant['label']}: RAI — sanitizing + retrying")
        variant["rai_first_attempt"] = str(e)[:200]
    except Exception as e:
        _log(f"[flow-pipeline] {tag} {variant['label']}: collect FAILED: {e}")
        variant["error"] = f"collect_failed: {e}"
        _persist(manifest, manifest_path)
        return

    # RAI retry: sanitize prompt, resubmit into the same project, collect again.
    try:
        sanitized = sanitize_prompt_for_rai(
            variant["veo_prompt_used"], product_name=variant.get("product_name", "product"),
        )
        _log(f"[flow-pipeline] {tag} {variant['label']}: sanitized → {sanitized!r}")
        flow_backend.submit_into_project(
            project, PROJECT_ROOT / variant["staged"], sanitized,
        )
        flow_backend.collect_one_from_project(project, dest)
        variant["video"] = str(dest.relative_to(PROJECT_ROOT))
        variant["rai_retry"] = True
        variant["prompt_sanitized"] = sanitized
        _log(f"[flow-pipeline] {tag} {variant['label']}: retry clip done")
    except flow_backend.RAIBlocked as e:
        _log(f"[flow-pipeline] {tag} {variant['label']}: RAI after sanitize — giving up")
        variant["error"] = f"rai_blocked_after_sanitize: {e}"
    except Exception as e:
        _log(f"[flow-pipeline] {tag} {variant['label']}: retry FAILED: {e}")
        variant["error"] = f"retry_failed: {e}"
    _persist(manifest, manifest_path)


def run(
    run_id: str | None = None,
    output_root: Path | None = None,
    skip_video: bool = False,
    region: str | None = None,
    max_concurrent: int | None = None,
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
    duration_s = int(gen.get("video_duration_seconds", 8))
    aspect_ratio = gen.get("video_aspect_ratio", "9:16")
    flow_cfg = gen.get("flow", {})
    if max_concurrent is None:
        max_concurrent = int(flow_cfg.get("parallel_video_jobs", 5))
    max_concurrent = max(1, min(max_concurrent, 20))
    model_short = _resolve_flow_model(settings)
    model_label = flow_backend._resolve_model_label(model_short)

    nano_prompt_template = PROMPT_NANO.read_text().strip()
    veo_prompt = PROMPT_VEO.read_text().strip()

    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = Path(output_root or PROJECT_ROOT / "outputs") / f"{run_id}-flow"
    raw_dir = output_root / "raw"
    staged_dir = output_root / "staged"
    videos_dir = output_root / "videos"
    debug_dir = output_root / "_debug"
    for d in (raw_dir, staged_dir, videos_dir, debug_dir):
        d.mkdir(parents=True, exist_ok=True)

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
        "video_model": model_short,
        "video_backend": "flow_playwright",
        "max_concurrent": max_concurrent,
        "products": [],
    }

    accounts = load_accounts()
    _log(
        f"[flow-pipeline] run_id={run_id} output={output_root}; "
        f"{len(accounts)} account(s), {VARIANTS_PER_PRODUCT} variant(s)/product, "
        f"model={model_short} ({model_label}), max_concurrent={max_concurrent}"
    )

    # ---- Phase A: prep all products (cover, per-variant restage + hooks) ----
    _log("[flow-pipeline] phase A: prep (2 restaged scenes per product)")
    for idx, product in enumerate(picks, start=1):
        entry = _prep_product(
            idx, len(picks), product, accounts,
            nano_prompt_template, veo_prompt, image_model, aspect_ratio,
            raw_dir, staged_dir,
        )
        manifest["products"].append(entry)
        _persist(manifest, manifest_path)

    if skip_video:
        _log("[flow-pipeline] skip_video=True — finished after phase A")
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _persist(manifest, manifest_path)
        return manifest_path

    # Each pending product → list of (variant, dest) still needing a clip.
    product_jobs: list[dict] = []
    for entry in manifest["products"]:
        if entry.get("status") == "error":
            continue
        slug = entry["slug"]
        pending: list[tuple[dict, Path]] = []
        for variant in entry["variants"]:
            staged_rel = variant.get("staged")
            if not staged_rel or not (PROJECT_ROOT / staged_rel).exists():
                variant["error"] = "staged_image_missing"
                continue
            dest = videos_dir / f"{slug}-{variant['label'].lower()}.mp4"
            if dest.exists() and dest.stat().st_size > 0:
                _log(f"[flow-pipeline] skip cached clip {dest.name}")
                variant["video"] = str(dest.relative_to(PROJECT_ROOT))
                continue
            pending.append((variant, dest))
        if pending:
            product_jobs.append({"entry": entry, "slug": slug, "pending": pending})

    if not product_jobs:
        _log("[flow-pipeline] nothing to generate — all clips cached")
        manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _persist(manifest, manifest_path)
        return manifest_path

    total_clips = sum(len(j["pending"]) for j in product_jobs)
    _log(f"[flow-pipeline] phase B/C: {len(product_jobs)} product(s), "
         f"{total_clips} clip(s), windows of {max_concurrent}")

    pw_mgr = sync_playwright()
    p = pw_mgr.__enter__()
    context = None
    try:
        context = _open_flow_context_or_die(p)

        # Process products in windows. Within a window every product gets ONE
        # project; its clips are submitted A-then-B and collected A-then-B so
        # the labels stay correct. Clip A's across the window generate
        # concurrently on Veo's backend; likewise clip B's.
        for w_start in range(0, len(product_jobs), max_concurrent):
            window = product_jobs[w_start : w_start + max_concurrent]
            _log(f"[flow-pipeline] window {w_start // max_concurrent + 1}: "
                 f"{len(window)} product(s)")

            # Pass 1: open a project per product, submit clip A.
            for job in window:
                slug = job["slug"]
                try:
                    job["project"] = flow_backend.open_project(
                        context, duration_s, aspect_ratio, model_label,
                        debug_dir, tag=slug[:24],
                    )
                    variant, _dest = job["pending"][0]
                    flow_backend.submit_into_project(
                        job["project"], PROJECT_ROOT / variant["staged"],
                        variant["veo_prompt_used"],
                    )
                    time.sleep(SUBMIT_THROTTLE_S)
                except Exception as e:
                    _log(f"[flow-pipeline] {slug}: open/submit-A FAILED: {e}")
                    job["project"] = None
                    job["pending"][0][0]["error"] = f"submit_failed: {e}"
                    _persist(manifest, manifest_path)

            # Pass 2: collect clip A, then submit clip B (if any).
            for job in window:
                if not job.get("project"):
                    continue
                variant_a, dest_a = job["pending"][0]
                _collect_with_rai_retry(job["project"], variant_a, dest_a,
                                        manifest, manifest_path)
                if len(job["pending"]) > 1:
                    variant_b, _dest_b = job["pending"][1]
                    try:
                        flow_backend.submit_into_project(
                            job["project"], PROJECT_ROOT / variant_b["staged"],
                            variant_b["veo_prompt_used"],
                        )
                        time.sleep(SUBMIT_THROTTLE_S)
                    except Exception as e:
                        _log(f"[flow-pipeline] {job['slug']}: submit-B FAILED: {e}")
                        variant_b["error"] = f"submit_failed: {e}"
                        _persist(manifest, manifest_path)

            # Pass 3: collect clip B, close each project.
            for job in window:
                proj = job.get("project")
                if not proj:
                    continue
                if len(job["pending"]) > 1:
                    variant_b, dest_b = job["pending"][1]
                    if not variant_b.get("error"):
                        _collect_with_rai_retry(proj, variant_b, dest_b,
                                                manifest, manifest_path)
                flow_backend.close_project(proj)
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        try:
            pw_mgr.__exit__(None, None, None)
        except Exception:
            pass

    # Mark products ok/error based on whether any variant landed a video.
    for entry in manifest["products"]:
        if entry.get("status") == "error":
            continue
        has_any = any(v.get("video") for v in entry.get("variants", []))
        entry["status"] = "ok" if has_any else "error"
        if not has_any and "error" not in entry:
            entry["error"] = "all_variants_failed"

    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _persist(manifest, manifest_path)

    n_ok = sum(1 for p in manifest["products"] if p.get("status") == "ok")
    n_clips = sum(1 for p in manifest["products"]
                  for v in p.get("variants", []) if v.get("video"))
    _log(f"[flow-pipeline] done — {n_ok}/{len(picks)} products, "
         f"{n_clips} clip(s) → {output_root}")
    return manifest_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--output-root", default=None)
    ap.add_argument("--skip-video", action="store_true",
                    help="Run phase A only (Nano Banana restage) — skip Flow.")
    ap.add_argument("--region", default=None,
                    help="Country code stamped into manifest (US, UK, ...)")
    ap.add_argument("--max-concurrent", type=int, default=None,
                    help="Max products processed at once. Defaults to "
                         "settings.yaml :: generation.flow.parallel_video_jobs.")
    args = ap.parse_args()
    run(
        run_id=args.run_id,
        output_root=Path(args.output_root) if args.output_root else None,
        skip_video=args.skip_video,
        region=args.region,
        max_concurrent=args.max_concurrent,
    )


if __name__ == "__main__":
    main()
