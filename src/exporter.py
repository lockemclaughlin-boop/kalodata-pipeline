"""
Export finished run videos into a clean, human-facing Generations/ tree.

The pipeline's working files (manifest, raw covers, staged images, debug
screenshots) stay under outputs/<run-id>-flow/ — the dashboard and the
regenerate/assign features all read from there. This module produces the
flat, readable copy the client actually browses:

    Generations/
      2026-05-19 1430 US/
        Variation 1 - Electric Bike.mp4
        Variation 1 - Robot Vacuum.mp4
        Variation 2 - Electric Bike.mp4
        Variation 2 - Robot Vacuum.mp4

All "Variation 1" clips sort above all "Variation 2" clips in Finder, so the
first variations come first, then the next — as requested.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GENERATIONS_ROOT = PROJECT_ROOT / "Generations"

# Variant label → human "Variation N" name (drives the filename sort order).
_VARIATION_NAMES = {"A": "Variation 1", "B": "Variation 2",
                    "C": "Variation 3", "D": "Variation 4"}


def _safe_title(title: str) -> str:
    """Make a product title safe for a filename while keeping it readable
    (spaces and capitals preserved — not slugified)."""
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:80] or "product"


def _run_folder_label(manifest: dict, run_dir_name: str) -> str:
    """Turn a run-id like '20260519-143000-US' into '2026-05-19 1430 US'."""
    rid = manifest.get("run_id") or run_dir_name
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", str(rid))
    if m:
        y, mo, d, hh, mm = m.groups()
        label = f"{y}-{mo}-{d} {hh}{mm}"
    else:
        label = str(rid)
    region = manifest.get("region")
    if region and region != "?":
        label = f"{label} {region}"
    return label


def export_to_generations(manifest_path: Path) -> Path:
    """Copy every finished variant video into Generations/<label>/ named
    'Variation N - <Title>.mp4'. Idempotent — re-running overwrites. Returns
    the run's Generations folder path."""
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    label = _run_folder_label(manifest, manifest_path.parent.name)
    dest_dir = GENERATIONS_ROOT / label
    dest_dir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    n_copied = 0
    for product in manifest.get("products", []):
        title = _safe_title(product.get("title"))
        for v in product.get("variants", []):
            rel = v.get("video")
            if not rel:
                continue
            src = PROJECT_ROOT / rel
            if not src.exists() or src.stat().st_size == 0:
                continue
            vlabel = str(v.get("label") or "A").upper()
            variation = _VARIATION_NAMES.get(vlabel, f"Variation {vlabel}")
            base = f"{variation} - {title}"
            name = f"{base}.mp4"
            # Disambiguate if two products share a title.
            counter = 2
            while name in used:
                name = f"{base} ({counter}).mp4"
                counter += 1
            used.add(name)
            shutil.copy2(src, dest_dir / name)
            n_copied += 1

    print(f"[export] {n_copied} clip(s) → {dest_dir}", flush=True)
    return dest_dir
