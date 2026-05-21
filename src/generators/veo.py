"""
Veo 3 video generation — animate a staged product image with the hand-poke
camera-push prompt. Veo is a long-running operation: submit → poll → download.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from google import genai
from google.genai import types


POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 60 * 10  # 10 min hard cap per clip


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def generate_video(
    staged_image: Path,
    prompt: str,
    dest: Path,
    duration_seconds: int = 8,
    aspect_ratio: str = "9:16",
    model: str = "veo-3.0-fast-generate-001",
) -> Path:
    """
    Submit a Veo 3 image-to-video job, poll until done, download MP4 to dest.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing from environment / .env")

    staged_image = Path(staged_image)
    if not staged_image.exists():
        raise FileNotFoundError(f"staged image not found: {staged_image}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    client = genai.Client(api_key=api_key)

    image_input = types.Image(
        image_bytes=staged_image.read_bytes(),
        mime_type=_mime_for(staged_image),
    )
    config = types.GenerateVideosConfig(
        aspect_ratio=aspect_ratio,
        duration_seconds=duration_seconds,
        number_of_videos=1,
    )

    operation = client.models.generate_videos(
        model=model,
        prompt=prompt,
        image=image_input,
        config=config,
    )

    deadline = time.time() + POLL_TIMEOUT_S
    while not operation.done:
        if time.time() > deadline:
            raise TimeoutError(
                f"Veo job did not complete within {POLL_TIMEOUT_S}s"
            )
        time.sleep(POLL_INTERVAL_S)
        operation = client.operations.get(operation)

    if operation.error:
        raise RuntimeError(f"Veo job failed: {operation.error}")

    response = operation.response
    generated = getattr(response, "generated_videos", None) or []
    if not generated:
        # Veo completed "successfully" with an empty list ⇒ RAI / safety filter.
        # The response carries filter counts and reasons — surface them so the
        # caller can tell trademark hits from face/brand hits from prompt blocks.
        n_filt = getattr(response, "rai_media_filtered_count", None)
        reasons = getattr(response, "rai_media_filtered_reasons", None) or []
        if reasons:
            raise RuntimeError(
                f"Veo blocked by safety filter ({n_filt or len(reasons)}): "
                + "; ".join(str(r) for r in reasons)
            )
        raise RuntimeError(
            "Veo returned no generated videos (likely RAI filter, no reason field). "
            f"Response repr: {response!r}"[:500]
        )

    video_ref = generated[0].video
    # google-genai returns a Video object; download via the files API.
    client.files.download(file=video_ref)
    video_ref.save(str(dest))
    return dest
