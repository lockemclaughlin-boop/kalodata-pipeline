# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# KaloData → Nano Banana → Veo Pipeline

Local desktop automation: scrapes trending TikTok Shop products from Kalodata, restages each cover photo into a category-appropriate retail scene with **Gemini 2.5 Flash Image** (Nano Banana Pro), then animates it with **Veo 3** image-to-video (via the public API) or via Google Flow's UI driven by Playwright — producing short ad-style clips for a client.

**Full design lives in `ARCHITECTURE.md`. Read that first for any non-trivial change.**

---

## Current state

- **Phase 1** (Kalodata scraper) — working. Real selectors verified via `scripts/dump_kalodata_dom.py`. Streamlit dashboard drives filters, results grid with images, save selection.
- **Phase 2** (generators) — working. `nano_banana.py` (Gemini image), `veo.py` (Veo 3 API), `hooks.py` (hook+caption+environment text + RAI prompt sanitization).
- **Phase 3** (pipeline orchestrator) — working. `src/pipeline.py` chains the stages and writes `outputs/<run-id>/manifest.json`. Resumable (skips outputs that already exist).
- **Phase 4** (Streamlit UI) — working. Search → grid → save selection → cost-gated Generate → per-product regenerate with editable prompts.
- **Phase 5** (Flow Playwright backend) — `src/generators/flow.py` still a stub. Pipeline silently uses `veo_api_fast` regardless of `settings.yaml :: video_backend`.
- **db.py** — never built. State lives in `outputs/<run-id>/manifest.json` per run, not SQLite.

---

## Commands

All commands run from project root with the venv active (`source .venv/bin/activate`) or via `.venv/bin/...`.

```bash
# Launch the dashboard (primary UI for everything end-to-end)
.venv/bin/streamlit run src/app.py --browser.gatherUsageStats=false

# CLI smoke test of the scraper (writes outputs/_smoke/products.json + HTML preview)
.venv/bin/python scripts/scrape_test.py --max-results 50

# One-shot DOM dump for selector discovery (writes outputs/_inspect/dom.json)
# Run this when Kalodata's UI changes and the scraper breaks.
.venv/bin/python scripts/dump_kalodata_dom.py

# Refresh the Kalodata session (popped browser, human signs in)
.venv/bin/python scripts/login_kalodata.py

# Run the full generation pipeline directly (bypasses Streamlit)
.venv/bin/python src/pipeline.py --run-id 20260516-1500          # full Nano Banana + Veo
.venv/bin/python src/pipeline.py --skip-video                    # restage only (cheap/fast test)

# Regenerate a single product within a finished run with edited prompts
.venv/bin/python scripts/regenerate_product.py \
  --run-id 20260516-1500 --product-id 1732268320365777584 \
  --nano-prompt "..." --hook "Cleans tile in seconds" \
  --no-regen-video    # only re-restage and re-burn, skip the $1.20 Veo cost
```

No test suite exists yet. Verification is done via the smoke test + the Streamlit dashboard end-to-end.

---

## Architecture in one paragraph

(1) Playwright scrapes Kalodata's product table using the **persistent Chrome profile** at `.auth/chrome-profile/`. Filters (region, time window, category, revenue, item sold, growth) are applied by clicking the actual filter rail in the headed browser and then clicking the blue **Submit** button. Pagination is real Ant Design pagination, not infinite scroll. (2) Streamlit shows the products in a 4-col grid; user picks ones, `selection.json` is written. (3) For each pick, one `gemini-2.5-flash` text call returns `{hook, caption, environment, product_name}`. (4) `nano_banana.py` calls `gemini-2.5-flash-image` with the cover photo + the boutique prompt with `{environment}` substituted. (5) Video generation runs through one of two backends: `veo.py` submits to the Veo 3 API (`veo-3.0-fast-generate-001`) and polls until done, OR `flow.py` drives Google Flow's UI via Playwright on the shared `.auth/chrome-profile/` profile (Veo 3.1 Lite via the Flow gallery; Gemini-sanitized retry for RAI rejections). (6) All artifacts land in `outputs/<run-id>/{raw,staged,videos}/<slug>.*` (API path) or `outputs/<run-id>-flow/{raw,staged,videos}/<slug>-<variant>.mp4` (Flow path) with `manifest.json` recording prompts used, hook, post caption, environment, and any RAI retry metadata.

---

## Conventions and gotchas

- **Python 3.11+**, single venv at `.venv/`. `start.command` builds it on first launch.
- **Streamlit shells out to subprocesses** for any Playwright work. The sync Playwright API breaks inside Streamlit's worker thread (signal-handler thread mismatch). Pattern: `subprocess.Popen([...])` + line-streaming stdout to a `st.empty()` log box. See `_run_search` and the Generate button in `src/app.py`.
- **Chrome profile lock**: `launch_persistent_context` writes `SingletonLock` in `.auth/chrome-profile/`. If a previous Chrome was killed mid-run, the next launch errors with *"Failed to create a ProcessSingleton"*. `_open_context` in `src/scraper/kalodata.py` auto-deletes stale `SingletonLock` / `SingletonCookie` / `SingletonSocket` before launching. Don't bypass that.
- **Selectors live in one place** (`src/scraper/selectors.py`). Centralize anything Kalodata-DOM-related there.
- **Kalodata's paywall overlay** (`Component-MemberListMask` + `Component-MemberItemLock` + `tablePaginationMemberListMask`) sits on top of the pagination area and intercepts clicks on Next + page-size changer. Workaround: `_kill_paywall_overlay` in `kalodata.py` removes those nodes via JS before each click, with `element.click()` JS-dispatch as fallback. On the **free tier the result cap is enforced server-side at 10 products per query** regardless — clicking Next returns the same 10 IDs. The scraper bails after 2 empty pages and logs that the account is capped.
- **Reuse existing pages, don't open new tabs.** Persistent contexts restore prior tabs; `search_products` reuses `context.pages[0]` instead of creating an extra `about:blank` tab that Playwright would then drive while the real Kalodata tab sits idle.
- **Don't re-navigate if already on the page.** `page.goto(kalodata.com/product)` while already there triggers a Cloudflare blank flash — `search_products` skips the goto if the URL is already correct.
- **Human-pace delays everywhere** touching Kalodata. 2–6s between page loads. Filter clicks have small 0.4–1.0s pauses.
- **Never fill credentials programmatically.** Login flows always pop a headed browser. Hard rule.
- **Cost gating is mandatory.** `MAX_RUN_COST_USD` in `.env` is a hard ceiling enforced in `src/pipeline.py` before any API call.
- **Prompts are dynamic, not static**. `prompts/nano_banana.txt` uses `{environment}` placeholder filled per product. The Gemini text call in `hooks.py` produces the environment alongside the hook + post caption — one round-trip, ~$0.0001/product.

---

## Common tasks (what the user asks → what to do)

| User says | You do |
|---|---|
| "run the runbook" / "test phase 1" / "/test-scraper" | Follow `RUN_PHASE_1.md` step by step (still valid for selector verification flow) |
| "the scraper isn't finding products" / "selectors are off" | Run `scripts/dump_kalodata_dom.py`, read `outputs/_inspect/dom.json`, patch `src/scraper/selectors.py` + the click helpers in `kalodata.py` (`_set_time_window`, `_set_category`, `_set_min_bucket`), re-run scrape |
| "only loading 10 products" | That's the paywall — Kalodata caps the free tier server-side. Verify by checking the `[scrape] page N: rows=10, new=0` pattern in the log. Either upgrade the Kalodata plan, run multiple filter combos and stitch results, or accept 10 |
| "images aren't loading in the dashboard" | The dashboard uses raw `<img>` tags (not `st.image`) with `referrerpolicy="no-referrer"` — see `app.py` grid render. If URLs in `outputs/_dashboard/products.json` are guessed `kalocdn.com/tiktok.product/<id>/cover.png`, `_extract_cover_url` is failing to find the real URL in the row DOM — extend its JS sweep |
| "switch to a different Veo model" | Edit `config/settings.yaml :: generation.video_backend` to `veo_api_fast` (default), `veo_api_light` ($0.40/clip), or `veo_api_standard` ($3.20/clip) |
| "regenerate this clip with a different prompt" | Use the per-product **Edit prompts & regenerate** expander in the dashboard's Results gallery (calls `scripts/regenerate_product.py` under the hood) |
| "Kalodata session expired" | `python scripts/login_kalodata.py` to refresh the Chrome profile cookies |
| "set up Blotato posting" | Not built yet — open question in ARCHITECTURE.md §9. Post captions are already in each manifest entry (`post_caption`), so the Blotato poster just needs to read manifest + POST to their API |

---

## What NOT to do

- Don't run Playwright **inside** the Streamlit script. It must be a subprocess (see `_run_search`). The threading bug is silent — the click does nothing, no error logged.
- Don't enter Kalodata or Google credentials in code. Login is always human-driven.
- Don't commit `.auth/`, `.env`, `outputs/` — already in `.gitignore`.
- Don't switch `video_backend` silently — the per-clip cost differs 3–8×.
- Don't add new dependencies without `pip install --dry-run -r requirements.txt` first.
- Don't trust the smoke-test summary print (GMV/Sales/Growth columns) — its regex-based parsing picks the wrong cells. The truth is in each product's `extras` dict (column-name → cell-text), which is what the dashboard and manifest use.
- Don't bring back the scroll-to-load-more fallback in the scrape loop. Kalodata is real pagination; scrolling does nothing and just burns time.

---

## When things break

Likelihood order, based on what's actually broken in this codebase before:

1. **Kalodata DOM changes** → `dump_kalodata_dom.py` → patch `selectors.py` + click helpers. 15-min fix.
2. **Chrome profile locked** → kill stray Chrome processes (`pkill -f "user-data-dir=.*KaloData/.auth"`), the auto-cleanup in `_open_context` handles the lock files on next launch.
3. **Kalodata "Warm Reminder — Page loading failed"** → unauthenticated session. Re-run `scripts/login_kalodata.py`.
4. **Gemini / Veo API errors** → check `GOOGLE_API_KEY` in `.env`. Both `nano_banana.py` and `veo.py` raise clear errors on missing key. Veo's 10-min poll timeout is the second most common failure — bump `POLL_TIMEOUT_S` in `veo.py` if your account has long queues.
5. **Cost overrun** → `MAX_RUN_COST_USD` in `.env` should have stopped it pre-flight. Check the pipeline's "[pipeline] N products | est image ... + video ..." log line for the estimate.
