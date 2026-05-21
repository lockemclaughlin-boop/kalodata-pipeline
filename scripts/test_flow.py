#!/usr/bin/env python3
"""
End-to-end smoke test for the Flow Playwright backend.

Default target: the staged electric-bike image from the 2026-05-19 run with
the hand-poke camera-push prompt (which is known to trip Veo's RAI filter).
That makes this test exercise both the happy path AND the sanitize-and-retry
fallback in one run.

Usage from project root:
    python scripts/test_flow.py
    python scripts/test_flow.py --image PATH --prompt "..." --duration 4
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.generators.flow import RAIBlocked, generate_video
from src.generators.hooks import sanitize_prompt_for_rai


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = (
    PROJECT_ROOT / "outputs/20260519-130417-US/staged"
    / "1731365355800203265-voltvogue-electric-bike-peak-power-1500w-full-suspension-top.png"
)
DEFAULT_PROMPT_FILE = PROJECT_ROOT / "prompts/veo.txt"


def _default_prompt() -> str:
    raw = DEFAULT_PROMPT_FILE.read_text().strip()
    return raw.replace("{product}", "electric bike")


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    ap.add_argument("--prompt", type=str, default=None,
                    help="Override the default 'hand poke' prompt.")
    ap.add_argument("--product-name", type=str, default="electric bike",
                    help="Used when sanitizing the prompt after RAI failure.")
    ap.add_argument("--duration", type=int, default=8, choices=[4, 6, 8])
    ap.add_argument("--aspect", type=str, default="9:16", choices=["9:16", "16:9"])
    ap.add_argument("--model", type=str, default="veo-3.1-lite")
    ap.add_argument(
        "--dest",
        type=Path,
        default=PROJECT_ROOT / f"outputs/_flow_test/flow_{int(time.time())}.mp4",
    )
    ap.add_argument("--no-retry", action="store_true",
                    help="Skip the sanitize-and-retry fallback on RAI failure.")
    args = ap.parse_args()

    if not args.image.exists():
        raise SystemExit(f"image not found: {args.image}")
    prompt = args.prompt or _default_prompt()

    print(f"[test-flow] image:  {args.image}")
    print(f"[test-flow] prompt: {prompt}")
    print(f"[test-flow] dest:   {args.dest}")
    t0 = time.time()
    try:
        generate_video(
            staged_image=args.image,
            prompt=prompt,
            dest=args.dest,
            duration_seconds=args.duration,
            aspect_ratio=args.aspect,
            model=args.model,
            delay_seconds_min=0,
            delay_seconds_max=0,
        )
        print(f"[test-flow] done in {time.time()-t0:.1f}s → {args.dest}")
        return
    except RAIBlocked as e:
        print(f"[test-flow] RAI rejected the original prompt: {e}")
        if args.no_retry:
            raise SystemExit(1)

    print("[test-flow] sanitizing prompt via Gemini and retrying once…")
    sanitized = sanitize_prompt_for_rai(prompt, product_name=args.product_name)
    print(f"[test-flow] sanitized prompt: {sanitized}")
    generate_video(
        staged_image=args.image,
        prompt=sanitized,
        dest=args.dest,
        duration_seconds=args.duration,
        aspect_ratio=args.aspect,
        model=args.model,
        delay_seconds_min=0,
        delay_seconds_max=0,
    )
    print(f"[test-flow] done in {time.time()-t0:.1f}s (after retry) → {args.dest}")


if __name__ == "__main__":
    main()
