"""
Nano Banana Pro (Gemini 2.5 Flash Image) — restage a product photo into a
women's boutique scene.

restage_product() reads the source image, sends it to Gemini 2.5 Flash Image
with the boutique-restage prompt, writes the returned image to `dest`, and
returns the path. Retries once on transient failures.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from google import genai
from google.genai import types


_TRANSIENT_HINTS = (
    "503", "deadline", "timeout", "RESOURCE_EXHAUSTED", "UNAVAILABLE",
    "internal error", "Internal error", "rate limit",
)


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/png")


def _is_transient(err: Exception) -> bool:
    s = str(err)
    return any(h in s for h in _TRANSIENT_HINTS)


def restage_product(
    source_image: Path,
    prompt: str,
    dest: Path,
    model: str = "gemini-2.5-flash-image",
    aspect_ratio: str = "9:16",
) -> Path:
    """
    Call Gemini with source_image + prompt, save the returned image to dest.
    Retries once on transient failure. Raises on second failure.

    `aspect_ratio` is passed to Gemini's image_config so the output frame
    matches what Veo will animate (avoiding letterbox bars later). Supported
    by gemini-2.5-flash-image: "1:1", "9:16", "16:9", "4:3", "3:4".
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing from environment / .env")

    source_image = Path(source_image)
    if not source_image.exists():
        raise FileNotFoundError(f"source image not found: {source_image}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)
    source_bytes = source_image.read_bytes()
    mime = _mime_for(source_image)

    # Build config defensively — fall through to no image_config if the
    # installed google-genai version doesn't expose ImageConfig yet.
    config = None
    try:
        config = types.GenerateContentConfig(
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        )
    except (AttributeError, TypeError):
        config = None

    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            response = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=source_bytes, mime_type=mime),
                    prompt,
                ],
                config=config,
            )
            for cand in (response.candidates or []):
                for part in (cand.content.parts or []):
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        dest.write_bytes(inline.data)
                        return dest
            raise RuntimeError("Gemini returned no inline image data")
        except Exception as e:
            last_err = e
            if attempt == 1 and _is_transient(e):
                time.sleep(2)
                continue
            raise

    assert last_err is not None
    raise last_err
