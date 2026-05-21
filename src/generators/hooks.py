"""
Hook + caption generator. Given a product (title, GMV, units sold, growth,
category, etc.), returns a short on-screen hook and a longer social caption.

The hook is the punchy on-screen line the posting flow uses as an overlay or
metadata field — it has to be very short so it reads on a phone screen in <1
second. The caption is what gets posted alongside the video (TikTok/Reels/Shorts
body text), longer form with hashtags.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai
from google.genai import types


_TEXT_MODEL = "gemini-2.5-flash"


SYSTEM_PROMPT = """\
You write ad copy + a retail setting + a concrete noun for short-form product
videos (TikTok / Reels / YT Shorts). Output STRICT JSON with four keys:

  hook        — 3 to 7 words. Burned into the video as on-screen text. No
                emojis, no hashtags, no quotes, no period at the end. Should
                feel like a scroll-stopping headline. Examples:
                  "Sold out in 24 hours"
                  "Why everyone is buying this"
                  "TikTok made me buy it"
                  "This is selling out fast"

  caption     — 1 to 3 sentences for the post body. Include 4-6 relevant
                hashtags at the end. May include 1-2 tasteful emojis. No price
                (TikTok rules), no spammy claims.

  environment — A short noun phrase naming the retail setting where this
                specific product would be sold and displayed, starting with
                "a" or "an". Be category-accurate, not generic. Examples:
                  small appliance → "a modern small-appliance showroom"
                  pet food       → "a premium pet supplies store"
                  beauty serum   → "a clean, well-lit cosmetics boutique"
                  power tool     → "a hardware store aisle"
                  jewelry        → "a fine jewelry boutique"
                  kids toy       → "a bright toy store display"

  product_name — A 1-to-4-word concrete noun naming THIS product, used as a
                literal reference in the Veo motion prompt (e.g. "have a
                hand poke the <product_name>"). Strip brand names and SKU
                jargon; keep it short, specific, singular, no quotes.
                Examples:
                  "SHAPERX Shapewear for Women Tummy Control Bodysuits Zip-Up Crotch"
                    → "shapewear bodysuit"
                  "Toplux Magnesium Complex 8 Essential Magnesium Supplement 1000mg"
                    → "supplement bottle"
                  "Shark PowerPro Flex Reveal Plus Cordless Vacuum IZ382H"
                    → "cordless vacuum"
                  "Wavytalk Blowout Boost Ionic Thermal Brush"
                    → "thermal hair brush"

Do not output anything outside the JSON object. Do not wrap it in markdown
code fences."""


def _build_user_prompt(
    product: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> str:
    title = product.get("title") or "(no title)"
    bits: list[str] = [f"Product: {title}"]
    extras = product.get("extras") or {}
    # Fold the most useful Kalodata stats into the prompt context.
    for key in (
        "Revenue", "Item Sold", "Revenue Growth Rate",
        "Avg. Unit Price", "Commission Rate", "Creator Count",
    ):
        if key in extras:
            bits.append(f"{key}: {extras[key]}")
    if account and account.get("handle"):
        bits.append(
            f"Target account: {account['handle']}"
            + (f" on {account['platform']}" if account.get("platform") else "")
        )
    return "\n".join(bits)


def _extract_json(raw: str) -> dict:
    """Pull a JSON object out of Gemini's response even if it wraps with prose."""
    raw = raw.strip()
    # Strip ```json fences if present.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Last-resort: greediest {...} match.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"Could not parse JSON from Gemini response: {raw[:200]}")


def _sanitize_hook(hook: str) -> str:
    """Strip emoji/quotes/trailing punctuation so the ffmpeg overlay reads cleanly."""
    hook = hook.strip().strip('"').strip("'")
    # Drop emoji + non-printable
    hook = re.sub(r"[^\x20-\x7E]", "", hook)
    hook = re.sub(r"[.!]+$", "", hook).strip()
    # Collapse internal whitespace.
    hook = re.sub(r"\s+", " ", hook)
    return hook


_SANITIZE_SYSTEM_PROMPT = """\
You rewrite video-generation prompts to remove anything that trips a strict
content-safety / RAI filter. Strip ALL references to humans, hands, fingers,
body parts, faces, people, and any physical interaction with a person ("touch",
"poke", "grab", "hold"). Replace such interactions with pure camera motion
(dolly-in, orbit, push, pan, rack focus) and/or product behavior (rotate,
levitate slightly, surface glints, lighting shift).

Preserve the camera technique and the product subject. Keep it under 240
characters. Return ONLY the rewritten prompt — no preamble, no quotes, no
markdown, no explanation."""


def sanitize_prompt_for_rai(original_prompt: str, product_name: str = "product") -> str:
    """Rewrite a video prompt to remove person/hand/body references that trip
    Veo's RAI filter. One Gemini text call, ~$0.0001."""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing from environment / .env")

    client = genai.Client(api_key=api_key)
    user = (
        f"Product subject: {product_name}\n"
        f"Original prompt:\n{original_prompt.strip()}"
    )
    thinking_cfg = None
    try:
        thinking_cfg = types.ThinkingConfig(thinking_budget=0)
    except (AttributeError, TypeError):
        thinking_cfg = None
    cfg_kwargs = dict(temperature=0.7, max_output_tokens=256)
    if thinking_cfg is not None:
        cfg_kwargs["thinking_config"] = thinking_cfg

    response = client.models.generate_content(
        model=_TEXT_MODEL,
        contents=[
            types.Part.from_text(text=_SANITIZE_SYSTEM_PROMPT),
            types.Part.from_text(text=user),
        ],
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    out = (response.text or "").strip()
    out = out.strip('"').strip("'").strip()
    # Strip any "Rewritten:" prefix the model might emit despite instructions.
    out = re.sub(r"^(rewritten|prompt|here.s)\s*[:\-]\s*", "", out, flags=re.IGNORECASE)
    if not out:
        # Fallback: hard-strip person/hand language with regex so we never
        # return an empty string and stall the retry.
        out = re.sub(
            r"\b(have a |with a )?hand[s]? (poke|touch|grab|hold|reach)[a-z ]*?(the )?",
            "",
            original_prompt,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\bas if the person[a-z ,]*?(touched|saw|held) it\b",
            "",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def generate_hook_and_caption(
    product: dict[str, Any],
    account: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Returns {"hook": "...", "caption": "...", "environment": "..."}. Raises
    on hard failure. Passing `account` shifts the copy slightly toward that
    account's platform / handle audience.
    """
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY missing from environment / .env")

    client = genai.Client(api_key=api_key)
    # Structured output schema — forces Gemini to return valid JSON with these
    # exact three string fields. Combined with response_mime_type this is far
    # more reliable than free-form parsing.
    response_schema = {
        "type": "object",
        "properties": {
            "hook":         {"type": "string"},
            "caption":      {"type": "string"},
            "environment":  {"type": "string"},
            "product_name": {"type": "string"},
        },
        "required": ["hook", "caption", "environment", "product_name"],
        "propertyOrdering": ["hook", "caption", "environment", "product_name"],
    }
    # Disable thinking so the token budget all goes to the response — otherwise
    # gemini-2.5-flash can spend the whole budget reasoning and emit truncated
    # JSON ("{\n  \"" was the entire response we got on a failing run).
    thinking_cfg = None
    try:
        thinking_cfg = types.ThinkingConfig(thinking_budget=0)
    except (AttributeError, TypeError):
        # Older google-genai versions don't have ThinkingConfig; skip.
        thinking_cfg = None

    cfg_kwargs = dict(
        temperature=0.9,
        max_output_tokens=1024,
        response_mime_type="application/json",
        response_schema=response_schema,
    )
    if thinking_cfg is not None:
        cfg_kwargs["thinking_config"] = thinking_cfg

    response = client.models.generate_content(
        model=_TEXT_MODEL,
        contents=[
            types.Part.from_text(text=SYSTEM_PROMPT),
            types.Part.from_text(text=_build_user_prompt(product, account)),
        ],
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    raw = (response.text or "").strip()
    if not raw:
        raise RuntimeError("Gemini returned empty hook response")

    data = _extract_json(raw)
    hook = _sanitize_hook(str(data.get("hook", "")))
    caption = str(data.get("caption", "")).strip()
    environment = str(data.get("environment", "")).strip().rstrip(".").strip()
    if environment and not environment.lower().startswith(("a ", "an ")):
        environment = "a " + environment
    product_name = str(data.get("product_name", "")).strip().strip(".").strip('"').strip("'")
    if not hook:
        raise RuntimeError(f"Gemini returned no hook in: {raw[:200]}")
    return {
        "hook": hook,
        "caption": caption,
        "environment": environment or "a curated specialty retail store",
        "product_name": product_name or "product",
    }
