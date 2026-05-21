"""
Google Flow video generator — Playwright-driven UI automation.

Why this exists: Flow has no public API, but its credit pricing on Google AI
Ultra is ~5–10x cheaper per Veo 3.1 Lite clip than the direct Gemini API
(~$0.08 effective on the $200/mo Ultra plan vs $0.40 on the public API).
We trade off API ergonomics (programmatic errors, parallelism, ToS clarity)
for cost.

Operational notes:
  - Runs headed real Chrome with the SHARED persistent profile under
    .auth/chrome-profile/ — the same profile Kalodata uses. One profile to
    maintain, one Google login. Trade-off: cannot run the Kalodata scraper
    and Flow generation simultaneously (Chrome locks user-data-dir while
    running). pipeline.py is serial, so this isn't a real constraint.
  - First run: invoke `python scripts/login_flow.py` to sign into Google
    inside that shared profile. Cookies live in the profile dir thereafter.
  - Sleep 15–60s between clips (configurable via settings.yaml :: generation.flow)
    to look human and stay under any rate gates.
  - One account, one machine — do not share credentials.
  - This file is INTENTIONALLY ISOLATED from src/pipeline.py. Reverting to
    the Veo API path is a zero-edit decision — just keep running
    `python src/pipeline.py` instead of `python scripts/run_flow_pipeline.py`.

UI selectors below were verified 2026-05-19 by probing the live DOM via the
Chrome extension. See plan file in .claude/plans/ for the full inventory.
"""

from __future__ import annotations

import base64
import random
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# Shared with Kalodata. See src/scraper/kalodata.py :: _open_context.
CHROME_PROFILE_DIR = Path(".auth/chrome-profile")
FLOW_URL = "https://labs.google/fx/tools/flow"

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 60 * 8  # 8 min hard cap per clip — Veo 3.1 Lite usually finishes in 1-3 min


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------

class FlowUIChanged(RuntimeError):
    """Raised when the Flow UI no longer matches the selectors this module
    expects. The caller should re-probe the live DOM (Chrome extension) and
    patch the selector table at the top of this file."""


class RAIBlocked(RuntimeError):
    """Raised when Veo's content / responsible-AI filter rejects the prompt.
    Distinct from generic errors so the caller can choose to sanitize-and-retry."""


# ---------------------------------------------------------------------------
# Browser / session
# ---------------------------------------------------------------------------

def _open_context(p, headed: bool = True) -> BrowserContext:
    """Real Chrome + persistent profile so the Google sign-in survives across runs.
    Mirrors src/scraper/kalodata.py :: _open_context — only diff is accept_downloads."""
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        stale = CHROME_PROFILE_DIR / name
        try:
            if stale.exists() or stale.is_symlink():
                stale.unlink()
        except OSError:
            pass
    return p.chromium.launch_persistent_context(
        user_data_dir=str(CHROME_PROFILE_DIR.resolve()),
        channel="chrome",
        headless=not headed,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
        accept_downloads=True,
    )


def ensure_logged_in(headed: bool = True) -> None:
    """Open Flow, verify we land on a signed-in page (avatar + 'New project' visible).
    Used by scripts/login_flow.py as the post-login sanity check. Blocks until the
    user closes the popped Chrome window."""
    with sync_playwright() as p:
        context = _open_context(p, headed=headed)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(FLOW_URL, wait_until="domcontentloaded")
        try:
            page.get_by_role("button", name="New project").wait_for(timeout=30_000)
            print("[flow] signed in — saw 'New project' tile")
        except PlaywrightTimeoutError:
            print(
                "[flow] not signed in. Use the open window to log into Google,\n"
                "       then close the window to persist the session."
            )
        try:
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# UI helpers (one function per UI block so retries / debug snapshots are localized)
# ---------------------------------------------------------------------------

def _settings_pill(page: Page):
    """Composer's settings pill — always present, always shows current settings.
    Match on Video/model family AND a numeric setting marker (count or duration)."""
    return page.locator("button").filter(
        has_text=re.compile(r"(Nano Banana|Veo|Omni|Video|Flash|Image)")
    ).filter(
        has_text=re.compile(r"x[1-4]|1x|[468]s|10s")
    ).first


def _composer_ready(page: Page, timeout_ms: int = 30_000) -> None:
    """The settings pill is always present in the composer — use it as the
    ready signal. Don't depend on the textarea, which is a contenteditable
    div and harder to anchor on reliably."""
    _settings_pill(page).wait_for(state="visible", timeout=timeout_ms)


def _ui_drift_probe(page: Page) -> None:
    """At startup, confirm the two anchor selectors we depend on most are
    findable. If not, the Flow UI probably changed and we should bail loudly
    instead of grinding through 8+ minutes of timeouts."""
    try:
        _settings_pill(page).wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError as e:
        raise FlowUIChanged(
            "Composer settings pill not found within 15s. Flow may have "
            "renamed/restructured the composer. Re-probe the live DOM via "
            "mcp__claude-in-chrome and patch the selector table in flow.py."
        ) from e


def _open_settings_popup(page: Page) -> None:
    _composer_ready(page)
    _settings_pill(page).click()
    page.get_by_text("Generating will use", exact=False).wait_for(timeout=10_000)


def _click_pill(page: Page, label: str, timeout_ms: int = 8_000) -> None:
    """Click a popup pill (Image/Video, Frames/Ingredients, 9:16/16:9, 1x/x2/...,
    4s/6s/8s/10s). Every such pill in Flow is rendered as
        <button role="tab" class="flow_tab_slider_trigger">
    with textContent = "<icon_ligature><label>" — so the icon font name appears
    inline as text (e.g. "play_circleVideo", "crop_freeFrames"). Use substring
    matching on the label and scope by class to avoid hitting the composer
    settings pill or other buttons that happen to share text."""
    escaped = label.replace('"', '\\"')
    sel = f'button.flow_tab_slider_trigger:has-text("{escaped}")'
    page.locator(sel).first.click(timeout=timeout_ms)


def _select_video_settings(
    page: Page,
    model_label: str,
    duration_seconds: int,
    aspect_ratio: str,
) -> int:
    """Configure Video / Frames / aspect / 1x / model / duration. Returns the
    credit estimate displayed at the bottom of the popup (useful for logging)."""
    # Output mode → Video. Re-renders popup (Frames/Ingredients appears, aspect
    # narrows from 5 options to 2, default model becomes Omni Flash or Veo 3.1).
    _click_pill(page, "Video")
    page.wait_for_timeout(800)
    _click_pill(page, "Frames")

    aspect_label = "9:16" if "9:16" in aspect_ratio else "16:9"
    _click_pill(page, aspect_label)
    _click_pill(page, "1x")

    # Model dropdown trigger — a button whose text reads "<current_model>arrow_drop_down"
    # (the arrow_drop_down icon ligature follows the model name). It is NOT a
    # .flow_tab_slider_trigger.
    model_combo = page.locator("button").filter(
        has_text="arrow_drop_down"
    ).filter(
        has_text=re.compile(r"Veo 3\.1|Omni|Nano Banana|Flash")
    ).first
    model_combo.wait_for(state="visible", timeout=5_000)
    model_combo.click()
    # Menu options are <div role="menuitem"> wrapping a <button>; text reads
    # "volume_up<MODEL_NAME>" (icon ligature + name). Substring match is enough.
    option = page.locator(f'[role="menuitem"]:has-text("{model_label}")').first
    option.click(timeout=5_000)

    _click_pill(page, f"{duration_seconds}s")

    # Credit estimate text — e.g. "Generating will use 10 credits"
    estimate = 0
    try:
        text = page.get_by_text("Generating will use", exact=False).inner_text(timeout=3_000)
        for tok in text.split():
            if tok.isdigit():
                estimate = int(tok)
                break
    except Exception:
        pass
    return estimate


def _close_settings_popup(page: Page) -> None:
    page.keyboard.press("Escape")
    try:
        page.get_by_text("Generating will use", exact=False).wait_for(
            state="hidden", timeout=5_000
        )
    except Exception:
        pass


def _attach_start_frame(page: Page, image_path: Path) -> None:
    """Click Start chip → set_input_files on the hidden file input → click
    'Add to Prompt' (the picker does NOT auto-close after upload)."""
    # Start chip is a <div> with exact text "Start" (no icon ligature). exact=True
    # avoids matching the "Start creating or drop media" placeholder text on the
    # empty canvas.
    start_chip = page.get_by_text("Start", exact=True).first
    start_chip.wait_for(state="visible", timeout=10_000)
    start_chip.click()
    file_input = page.locator("input[type='file']").first
    file_input.wait_for(state="attached", timeout=10_000)
    file_input.set_input_files(str(image_path))
    # After upload the picker shows the file in the library + a preview pane
    # on the right with an "Add to Prompt" CTA. We must click that to actually
    # attach the file as the Start frame — the picker does not auto-close.
    add_btn = page.get_by_text("Add to Prompt", exact=True).first
    add_btn.wait_for(state="visible", timeout=30_000)
    add_btn.click()
    # Wait for picker to close (Upload media button gone).
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if not page.get_by_text("Upload media", exact=False).first.is_visible():
                return
        except Exception:
            return
        time.sleep(0.3)
    # Picker didn't close — assume the click registered anyway and continue.


def _find_prompt_input(page: Page):
    """Flow's composer input is a <div contenteditable="true" role="textbox">,
    not a real <textarea>. The placeholder "What do you want to create?" is
    rendered as visible textContent of the div (with a zero-width-no-break-space
    appended), so get_by_placeholder does NOT bind."""
    return page.locator('[contenteditable="true"][role="textbox"]').first


def _enter_prompt_and_submit(page: Page, prompt: str) -> None:
    inp = _find_prompt_input(page)
    inp.wait_for(state="visible", timeout=10_000)
    inp.click()
    page.keyboard.type(prompt, delay=10)
    # Flow's submit button reads "arrow_forwardCreate" (icon ligature + "Create").
    submit = page.locator(
        'button:has-text("Create")'
    ).filter(
        has_text="arrow_forward"
    ).first
    try:
        submit.wait_for(state="visible", timeout=5_000)
        submit.click()
        return
    except Exception:
        pass
    # Fallback chain in case Flow renames the button.
    for cand in (
        page.get_by_role("button", name="Create"),
        page.get_by_role("button", name="Generate"),
        page.get_by_role("button", name="Send"),
        page.locator("button[aria-label*='create' i]"),
        page.locator("button[aria-label*='generate' i]"),
    ):
        try:
            cand.first.wait_for(state="visible", timeout=2_000)
            cand.first.click()
            return
        except Exception:
            continue
    inp.press("Enter")


# ---------------------------------------------------------------------------
# Result polling and download
# ---------------------------------------------------------------------------

_RAI_PHRASES = (
    "couldn't generate", "couldn’t generate",
    "failed to generate", "generation failed",
    "unable to generate",
    "violates", "policy", "safety",
    "try a different prompt", "try a different image",
    "something went wrong",
)


def _detect_clip_error(page: Page) -> str | None:
    """Look for any visible UI cue that Veo rejected/failed the clip. Returns
    the surfaced error text (truncated) or None."""
    for phrase in _RAI_PHRASES:
        try:
            loc = page.get_by_text(phrase, exact=False).first
            if loc.count() > 0 and loc.is_visible():
                return loc.inner_text(timeout=1_000).strip()[:200]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_MODEL_LABEL_MAP = {
    "veo-3.1-fast":    "Veo 3.1 - Fast",
    "veo-3.1-lite":    "Veo 3.1 - Lite",
    "veo-3.1-quality": "Veo 3.1 - Quality",
    "veo-3.1-lite-lp": "Veo 3.1 - Lite [Lower Priority]",
}


def _resolve_model_label(model: str) -> str:
    if model in _MODEL_LABEL_MAP:
        return _MODEL_LABEL_MAP[model]
    if model.startswith("Veo") or model.startswith("Omni") or model.startswith("Nano"):
        return model  # already a Flow label
    return "Veo 3.1 - Lite"  # safe default — cheapest


def _snap_factory(page: Page, debug_dir: Path, tag: str = ""):
    """Return a callable that screenshots `page` into `debug_dir` with a timestamped
    filename. Used by both the sequential and the parallel code paths."""
    def _snap(label: str) -> None:
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H%M%S")
            name = f"{stamp}-{tag}-{label}.png" if tag else f"{stamp}-{label}.png"
            path = debug_dir / name
            page.screenshot(path=str(path), full_page=True)
            print(f"[flow] debug snapshot: {path}")
        except Exception:
            pass
    return _snap


def open_project(
    context: BrowserContext,
    duration_seconds: int,
    aspect_ratio: str,
    model_label: str,
    debug_dir: Path,
    tag: str = "",
) -> dict:
    """Open a new tab → New Project. Video settings are NOT configured here —
    submit_into_project reconfigures on every submit (it reloads the page each
    time, which would otherwise drop the settings). The returned handle is
    reused for multiple submit_into_project / collect_one_from_project calls,
    so all clips land in this ONE Flow project.

    Returns a dict: {page, project_url, tag, debug_dir, collected_srcs,
    duration_seconds, aspect_ratio, model_label}."""
    page = context.new_page()
    _snap = _snap_factory(page, debug_dir, tag)

    page.goto(FLOW_URL, wait_until="domcontentloaded")
    try:
        page.get_by_role("button", name="New project").wait_for(timeout=20_000)
    except PlaywrightTimeoutError:
        _snap("not-signed-in")
        raise RuntimeError("Flow is not signed in. Run: python scripts/login_flow.py")

    print(f"[flow:{tag}] new project")
    page.get_by_role("button", name="New project").first.click()
    page.wait_for_url("**/flow/project/**", timeout=15_000)
    project_url = page.url
    _ui_drift_probe(page)

    return {
        "page": page,
        "project_url": project_url,
        "tag": tag,
        "debug_dir": debug_dir,
        "collected_srcs": [],
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "model_label": model_label,
    }


def submit_into_project(project: dict, staged_image: Path, prompt: str) -> None:
    """Within an already-open project: reload the project page (fresh composer,
    clears any RAI banner from a prior attempt), reconfigure video settings,
    upload `staged_image` as the Start frame, type `prompt`, click Create.
    Does NOT wait for the clip — pair with collect_one_from_project()."""
    page = project["page"]
    tag = project.get("tag", "")
    _snap = _snap_factory(page, project.get("debug_dir"), tag) if project.get("debug_dir") else (lambda _l: None)

    # Always reload the project URL: gives a clean composer, drops a stale RAI
    # banner, and survives the navigation that a prior collect left us on.
    page.goto(project["project_url"], wait_until="domcontentloaded")
    _composer_ready(page)

    try:
        print(f"[flow:{tag}] settings: model={project['model_label']} "
              f"duration={project['duration_seconds']}s aspect={project['aspect_ratio']}")
        _open_settings_popup(page)
        credits = _select_video_settings(
            page, project["model_label"], project["duration_seconds"],
            project["aspect_ratio"],
        )
        if credits:
            print(f"[flow:{tag}] credit estimate: {credits}")
        _close_settings_popup(page)
    except Exception:
        _snap("settings-failed")
        raise

    try:
        print(f"[flow:{tag}] upload start frame: {Path(staged_image).name}")
        _attach_start_frame(page, staged_image)
    except Exception:
        _snap("upload-failed")
        raise

    try:
        print(f"[flow:{tag}] submit prompt: {prompt[:80]!r}")
        _enter_prompt_and_submit(page, prompt)
    except Exception:
        _snap("submit-failed")
        raise


def collect_one_from_project(
    project: dict, dest: Path, timeout_s: int = POLL_TIMEOUT_S,
) -> Path:
    """Wait for the next NOT-YET-COLLECTED ready clip in this project's gallery,
    download it to `dest`, and navigate back to the project gallery so the next
    submit/collect works. Records the clip's src in project['collected_srcs'].

    Raises RAIBlocked on a content/RAI rejection. Does NOT close the page —
    the project stays open for further clips (use close_project when done)."""
    page = project["page"]
    tag = project.get("tag", "")
    debug_dir = project.get("debug_dir")
    _snap = _snap_factory(page, debug_dir, tag) if debug_dir else (lambda _l: None)
    context = page.context
    collected = project["collected_srcs"]

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Back to the gallery so video tiles are visible.
    if "/edit/" in page.url or not page.url.startswith(project["project_url"]):
        page.goto(project["project_url"], wait_until="domcontentloaded")

    deadline = time.time() + timeout_s
    last_log = 0.0
    clip_box = None
    while time.time() < deadline:
        try:
            clip_box = page.evaluate(
                """(collectedSrcs) => {
                    const vids = Array.from(document.querySelectorAll('video'));
                    for (const v of vids) {
                        const s = v.currentSrc || v.src || '';
                        if (!s.includes('media.')) continue;
                        if (collectedSrcs.includes(s)) continue;  // already downloaded
                        let el = v;
                        for (let i = 0; i < 8; i++) {
                            el = el.parentElement;
                            if (!el) break;
                            const r = el.getBoundingClientRect();
                            if (r.width >= 100 && r.height >= 100) {
                                return {x: r.x + r.width / 2, y: r.y + r.height / 2, src: s};
                            }
                        }
                    }
                    return null;
                }""",
                collected,
            )
            if clip_box:
                break
        except Exception:
            pass
        err = _detect_clip_error(page)
        if err:
            _snap("rai-blocked")
            raise RAIBlocked(err)
        if time.time() - last_log > 30:
            elapsed = int(time.time() - (deadline - timeout_s))
            print(f"[flow:{tag}] waiting for clip… {elapsed}s elapsed")
            last_log = time.time()
        time.sleep(POLL_INTERVAL_S)
    else:
        _snap("poll-timeout")
        raise TimeoutError(
            f"[flow:{tag}] clip did not finish within {timeout_s}s — "
            f"Veo backend may be saturated or the UI is stuck."
        )

    collected.append(clip_box["src"])

    # Click the tile → /edit/<clip-uuid> detail view → Download button.
    page.mouse.click(clip_box["x"], clip_box["y"])
    page.wait_for_url("**/flow/project/**/edit/**", timeout=15_000)

    download_btn = page.locator('button:has-text("Download")').filter(
        has_text="download"
    ).first
    download_btn.wait_for(state="visible", timeout=15_000)
    saved = False
    try:
        with page.expect_download(timeout=120_000) as dl_info:
            download_btn.click()
        dl_info.value.save_as(str(dest))
        print(f"[flow:{tag}] saved {dest}")
        saved = True
    except PlaywrightTimeoutError:
        _snap("download-event-timeout")
        print(f"[flow:{tag}] expect_download timed out — falling back to direct fetch")

    if not saved:
        video_src = page.evaluate(
            "() => { const v = document.querySelector('video'); return v ? (v.currentSrc || v.src || '') : ''; }"
        )
        if not video_src:
            _snap("no-video-src")
            raise RuntimeError(f"[flow:{tag}] no video src found in detail view")
        cookies = context.cookies()
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        response = context.request.get(
            video_src,
            headers={
                "Cookie": cookie_header,
                "Referer": page.url,
                "User-Agent": page.evaluate("() => navigator.userAgent"),
                "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
            },
        )
        if not response.ok:
            _snap("download-http-error")
            raise RuntimeError(f"[flow:{tag}] download HTTP {response.status}")
        data = response.body()
        if len(data) < 1024:
            _snap("download-too-small")
            raise RuntimeError(f"[flow:{tag}] downloaded MP4 is only {len(data)} bytes")
        dest.write_bytes(data)
        print(f"[flow:{tag}] saved {dest} ({len(data) // 1024}KB via fetch fallback)")

    # Back to the gallery for the next submit/collect.
    page.goto(project["project_url"], wait_until="domcontentloaded")
    return dest


def close_project(project: dict) -> None:
    """Close the project's browser tab."""
    try:
        project["page"].close()
    except Exception:
        pass


def _generate_one_clip(
    context: BrowserContext,
    staged_image: Path,
    prompt: str,
    dest: Path,
    duration_seconds: int,
    aspect_ratio: str,
    model_label: str,
    debug_dir: Path,
) -> Path:
    """Single clip in its own project — used by the standalone generate_video()
    and by test_flow.py."""
    project = open_project(
        context, duration_seconds, aspect_ratio, model_label, debug_dir, tag="solo",
    )
    try:
        submit_into_project(project, staged_image, prompt)
        print("[flow:solo] polling for ready clip…")
        collect_one_from_project(project, dest)
        return dest
    finally:
        close_project(project)


def generate_video(
    staged_image: Path,
    prompt: str,
    dest: Path,
    duration_seconds: int = 8,
    aspect_ratio: str = "9:16",
    model: str = "veo-3.1-lite",
    delay_seconds_min: int = 15,
    delay_seconds_max: int = 60,
) -> Path:
    """Drive Flow's UI to produce one Veo clip and download to dest.

    Standalone — opens a fresh Playwright context, runs one clip, closes it.
    For batch runs use _generate_one_clip with a shared context (see
    scripts/run_flow_pipeline.py).

    Raises RAIBlocked if Veo's content filter rejects the prompt. The caller
    can catch this, sanitize the prompt via hooks.sanitize_prompt_for_rai,
    and retry."""
    model_label = _resolve_model_label(model)
    staged_image = Path(staged_image)
    if not staged_image.exists():
        raise FileNotFoundError(f"staged image not found: {staged_image}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    debug_dir = dest.parent / "_debug"

    with sync_playwright() as p:
        context = _open_context(p, headed=True)
        try:
            _generate_one_clip(
                context, staged_image, prompt, dest,
                duration_seconds, aspect_ratio, model_label, debug_dir,
            )
            if delay_seconds_max > 0:
                time.sleep(random.uniform(delay_seconds_min, delay_seconds_max))
            return dest
        finally:
            try:
                context.close()
            except Exception:
                pass
