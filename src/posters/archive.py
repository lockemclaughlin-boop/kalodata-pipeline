"""
Posted-video archive.

When the client manually posts a generated clip to TikTok / Instagram / etc.,
the dashboard's "Mark as posted" button funnels through this module:

  1. Copy the captioned MP4 into outputs/_posted/<account-slug>/<date>/<slug>.mp4
  2. Write a per-post sidecar JSON next to the MP4 (so a Finder browse is
     self-explanatory)
  3. Append a record to the master ledger outputs/_posted/posts_log.json

Later, when Blotato (or a TikTok-API poster) lands, it can call record_post()
with its own arguments and the rest of the pipeline doesn't need to change.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml
from slugify import slugify


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_PATH = PROJECT_ROOT / "config/accounts.yaml"
POSTED_ROOT = PROJECT_ROOT / "outputs/_posted"
LEDGER_PATH = POSTED_ROOT / "posts_log.json"


def account_slug(handle: str) -> str:
    """Folder-safe slug for an account handle. '@brand_a' → 'brand-a'."""
    return slugify(handle.lstrip("@")) or "unknown-account"


def load_accounts() -> list[dict]:
    """
    Read config/accounts.yaml. Returns [] if the file is missing or empty.
    Each entry is a dict with at least `handle`; `platform` and `display_name`
    are optional but recommended.
    """
    if not ACCOUNTS_PATH.exists():
        return []
    try:
        data = yaml.safe_load(ACCOUNTS_PATH.read_text()) or {}
    except yaml.YAMLError:
        return []
    accounts = data.get("accounts") or []
    return [a for a in accounts if isinstance(a, dict) and a.get("handle")]


def _read_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {"posts": []}
    try:
        data = json.loads(LEDGER_PATH.read_text())
        if not isinstance(data.get("posts"), list):
            data["posts"] = []
        return data
    except (json.JSONDecodeError, OSError):
        return {"posts": []}


def _write_ledger_atomic(data: dict) -> None:
    POSTED_ROOT.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".posts_log.", suffix=".json", dir=str(POSTED_ROOT)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, LEDGER_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _resolve_source_video(product_entry: dict) -> Path:
    """Pick the best available output for archiving (captioned > video > staged)."""
    for key in ("captioned_video", "video", "staged"):
        rel = product_entry.get(key)
        if not rel:
            continue
        candidate = PROJECT_ROOT / rel
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    raise FileNotFoundError(
        f"No captioned_video/video/staged file found on disk for product "
        f"{product_entry.get('slug') or product_entry.get('id')!r}"
    )


def is_already_posted(
    source_run_id: str, source_product_id: str, account_handle: str
) -> list[dict]:
    """
    Return any existing ledger entries matching (run, product, account). Empty
    list means this clip has never been marked posted to that account. The UI
    uses this to show "✓ Already posted on <date>" chips above the form.
    """
    ledger = _read_ledger()
    return [
        p for p in ledger["posts"]
        if str(p.get("source_run_id")) == str(source_run_id)
        and str(p.get("source_product_id")) == str(source_product_id)
        and p.get("account") == account_handle
    ]


def record_post(
    product_entry: dict,
    account: dict,
    posted_at: date,
    run_id: str,
    posted_url: Optional[str] = None,
) -> dict:
    """
    Copy the clip into the archive folder + write the sidecar JSON + append a
    new entry to the ledger. Returns the new ledger entry.

    `product_entry` is one element of manifest.json's `products` list.
    `account` is one entry from accounts.yaml.
    """
    handle = str(account.get("handle") or "").strip()
    if not handle:
        raise ValueError("account.handle is required")

    source_path = _resolve_source_video(product_entry)
    slug = product_entry.get("slug") or product_entry.get("id") or "product"
    date_str = posted_at.isoformat()
    acct_slug = account_slug(handle)
    dest_dir = POSTED_ROOT / acct_slug / date_str
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_video = dest_dir / f"{slug}{source_path.suffix or '.mp4'}"
    shutil.copy2(source_path, dest_video)

    record = {
        "id": str(uuid.uuid4()),
        "posted_at": date_str,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "account": handle,
        "platform": account.get("platform"),
        "posted_url": (posted_url or "").strip() or None,
        "source_run_id": run_id,
        "source_product_id": product_entry.get("id"),
        "source_slug": slug,
        "title": product_entry.get("title"),
        "hook": product_entry.get("hook"),
        "post_caption": product_entry.get("post_caption"),
        "environment": product_entry.get("environment"),
        "video_archive_path": str(dest_video.relative_to(PROJECT_ROOT)),
        "source_video_path": (
            str(source_path.relative_to(PROJECT_ROOT))
            if source_path.is_relative_to(PROJECT_ROOT)
            else str(source_path)
        ),
    }

    # Sidecar JSON next to the MP4 — same content, single-post view.
    sidecar = dest_dir / f"{slug}.json"
    sidecar.write_text(json.dumps(record, indent=2))

    ledger = _read_ledger()
    ledger["posts"].append(record)
    _write_ledger_atomic(ledger)
    return record


def _affiliate_link(account: dict, product_entry: dict) -> str:
    """Build the affiliate link from the account's template + product info.
    Returns empty string if template/tag is missing."""
    template = (account.get("affiliate_url_template") or "").strip()
    tag = (account.get("affiliate_tag") or "").strip()
    pid = str(product_entry.get("id") or "").strip()
    if not template or not tag or not pid:
        return ""
    try:
        return template.format(
            product_id=pid,
            affiliate_tag=tag,
            kalodata_url=product_entry.get("kalodata_url") or "",
        )
    except (KeyError, IndexError):
        return ""


def assign_run_to_account(manifest_path: Path, account: dict) -> dict:
    """Copy every successful variant in a run into the account's folder, write
    a sidecar .json per variant with all posting data + the computed affiliate
    link.

    Layout: outputs/_posted/<account-slug>/<run-id>/<slug>-<variant>.{mp4,json}

    Returns {"n_copied", "dest_dir", "skipped"}."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    handle = str(account.get("handle") or "").strip()
    if not handle:
        raise ValueError("account.handle is required")

    manifest = json.loads(manifest_path.read_text())
    run_id = manifest.get("run_id") or manifest_path.parent.name
    acct_slug = account_slug(handle)
    dest_dir = POSTED_ROOT / acct_slug / manifest_path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)

    n_copied = 0
    skipped: list[str] = []
    for product in manifest.get("products", []):
        if product.get("status") != "ok":
            skipped.append(f"{product.get('slug')}: status={product.get('status')}")
            continue
        slug = product.get("slug") or product.get("id") or "product"
        affiliate_link = _affiliate_link(account, product)
        for v in product.get("variants", []):
            video_rel = v.get("captioned") or v.get("video")
            if not video_rel:
                skipped.append(f"{slug}-{v.get('label')}: no video")
                continue
            source = PROJECT_ROOT / video_rel
            if not source.exists() or source.stat().st_size == 0:
                skipped.append(f"{slug}-{v.get('label')}: source missing")
                continue

            variant_slug = f"{slug}-{(v.get('label') or 'a').lower()}"
            dest_video = dest_dir / f"{variant_slug}{source.suffix or '.mp4'}"
            shutil.copy2(source, dest_video)

            sidecar = {
                "account": handle,
                "account_display_name": account.get("display_name"),
                "platform": account.get("platform"),
                "affiliate_link": affiliate_link,
                "affiliate_tag": account.get("affiliate_tag"),
                "run_id": run_id,
                "product_id": product.get("id"),
                "product_slug": slug,
                "product_title": product.get("title"),
                "kalodata_url": product.get("kalodata_url"),
                "price_usd": product.get("price_usd"),
                "category_path": product.get("category_path"),
                "variant_label": v.get("label"),
                "hook": v.get("hook"),
                "post_caption": v.get("post_caption"),
                "environment": product.get("environment"),
                "video_path": str(dest_video.relative_to(PROJECT_ROOT)),
                "source_video_path": str(source.relative_to(PROJECT_ROOT)),
                "assigned_at": datetime.now().isoformat(timespec="seconds"),
            }
            (dest_dir / f"{variant_slug}.json").write_text(json.dumps(sidecar, indent=2))
            n_copied += 1

    # One run-level summary for quick scanning in Finder.
    (dest_dir / "_run_summary.json").write_text(json.dumps({
        "run_id": run_id,
        "account": handle,
        "account_display_name": account.get("display_name"),
        "platform": account.get("platform"),
        "affiliate_tag_set": bool((account.get("affiliate_tag") or "").strip()),
        "n_variants_copied": n_copied,
        "n_skipped": len(skipped),
        "skipped": skipped,
        "assigned_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))

    return {
        "n_copied": n_copied,
        "dest_dir": dest_dir,
        "skipped": skipped,
    }


def list_posts(
    account: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
) -> list[dict]:
    """
    Return ledger entries, newest-first. Optional filters narrow by account
    handle and/or inclusive date range. Posts without a parseable date are
    still returned (sorted to the end) when no date filter is set.
    """
    ledger = _read_ledger()
    posts = ledger["posts"]

    def _to_date(s: object) -> Optional[date]:
        if not isinstance(s, str):
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None

    if account:
        posts = [p for p in posts if p.get("account") == account]
    if date_from is not None or date_to is not None:
        filtered: list[dict] = []
        for p in posts:
            d = _to_date(p.get("posted_at"))
            if d is None:
                continue
            if date_from is not None and d < date_from:
                continue
            if date_to is not None and d > date_to:
                continue
            filtered.append(p)
        posts = filtered

    return sorted(
        posts,
        key=lambda p: (_to_date(p.get("posted_at")) or date.min, p.get("recorded_at") or ""),
        reverse=True,
    )
