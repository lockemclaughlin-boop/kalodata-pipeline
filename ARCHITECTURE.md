# KaloData → Nano Banana → Veo Automation

End-to-end pipeline that scrapes trending TikTok Shop products from Kalodata, restages the product photos in a boutique scene with Nano Banana Pro (Gemini 2.5 Flash Image), and animates them with Veo 3 — producing short ad-style clips for the client to publish.

---

## 1. Pipeline at a glance

```
[Filter UI] → [Kalodata scraper] → [Review grid] → [Nano Banana Pro] → [Veo 3] → [Output folder]
   params       photos + specs       client picks      restaged image     5–10s clip    /Videos/<run-id>/
```

Five stages, four of them async, one human-in-the-loop checkpoint between scraping and image generation. The checkpoint is good practice (lets the client cut duds before spending), not a financial necessity — at Veo 3 Fast pricing a 50-product run is ~$60.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for Playwright + Google AI SDK + image work |
| Browser automation | Playwright (Chromium, headed by default) | Handles Kalodata's React UI; headed mode lets the client solve any login/captcha once per session |
| UI | Streamlit | Zero-frontend-code, runs locally, handles file uploads/downloads, good enough for filter forms + a review grid |
| Image gen | `google-genai` SDK → `gemini-2.5-flash-image` (Nano Banana Pro) | Direct API, supports image-in/image-out edits |
| Video gen | **Default: Google Flow via Playwright** (`veo-3.1-fast` model). Fallback: `google-genai` SDK direct API. | Flow has no public API but is ~5–10× cheaper per clip on AI Ultra. We drive it the same way we drive Kalodata. Direct API kept as a swappable backend in case Flow breaks. |
| Job state | SQLite (`runs.db`) | Tracks each run, each product, each generation; survives crashes |
| Packaging | `uv` venv + a `start.command` shell script | Client double-clicks to launch; no Terminal knowledge needed |

**Why local, not hosted:** single user, sensitive credentials (Kalodata cookie + Google API key), generated files belong on their machine, no cloud bill, no auth to build. If they ever need multi-user, the same Python core lifts to a FastAPI backend behind Streamlit.

---

## 3. Folder layout

```
KaloData/
├── ARCHITECTURE.md              ← this file
├── README.md                    ← client-facing "how to run it"
├── start.command                ← double-click launcher (mac)
├── start.bat                    ← windows equivalent
├── requirements.txt
├── .env.example                 ← GOOGLE_API_KEY, KALODATA_EMAIL, etc.
├── .env                         ← gitignored, real secrets
├── src/
│   ├── app.py                   ← Streamlit entrypoint
│   ├── scraper/
│   │   ├── kalodata.py          ← Playwright session + filters + product list
│   │   └── product_page.py      ← per-product extraction (photos, specs, link)
│   ├── selection.py             ← auto-pick top N + diff against client overrides
│   ├── generators/
│   │   ├── nano_banana.py       ← Gemini Image edit calls
│   │   └── veo.py               ← Veo 3 generation + polling + download
│   ├── pipeline.py              ← orchestrates a full run, writes to SQLite
│   └── db.py                    ← SQLite schema + helpers
├── prompts/
│   ├── nano_banana.txt          ← boutique restage prompt (editable)
│   └── veo.txt                  ← hand-poke camera-push prompt (editable)
├── config/
│   ├── presets.yaml             ← saved filter combos
│   └── settings.yaml            ← output folder, default top-N, model versions
├── runs.db                      ← SQLite, gitignored
├── .auth/                       ← Playwright storage state (Kalodata cookies), gitignored
└── outputs/
    └── <run-id>/                ← one folder per run
        ├── manifest.json        ← what was generated, with what params, cost
        ├── source/              ← raw scraped photos
        ├── staged/              ← Nano Banana outputs
        └── videos/              ← Veo MP4s, named <product-slug>.mp4
```

---

## 4. Stage details

### 4.1 Kalodata scraper

**Approach:** Playwright in headed Chromium with persistent storage state in `.auth/`. First run, the app opens a browser and asks the client to log in once; subsequent runs reuse the cookie until it expires.

**Filter UI maps to:**
- Category (multi-select)
- Region / country
- Time window (Last 24h / 7d / 30d / custom)
- Min GMV, Min sales, Min growth %

**Per product, extract:**
- Product title, ID, deep link
- All product photos (download to `outputs/<run-id>/source/<product-slug>/`)
- Specs (price, GMV, units sold, commission, category path, shop name)
- Persist to SQLite `products` table

**Anti-bot hygiene:**
- Headed browser, real user-agent, persistent context
- Random 2–6s delays between product page navigations
- Cap at ~150 products per session; longer runs split into batches
- Detect throttle/captcha and pause; surface in UI for the client to resolve

**Risk mitigations:** wrap every selector in a "selector still works?" smoke test that runs at the start of each session and warns the client immediately if Kalodata changed their DOM. Cheaper than discovering it mid-run.

### 4.2 Selection (human-in-the-loop)

Streamlit grid: thumbnail, title, GMV, sales, link out to Kalodata. Default state = top N by GMV pre-checked (configurable rule). Client can uncheck duds and check additional rows. Big "Generate" button at the bottom shows estimated cost: `selected_count × (image_cost + veo_cost_per_clip)`.

### 4.3 Nano Banana Pro (Gemini 2.5 Flash Image)

For each selected product, pick the cleanest source photo (largest dimensions, no overlay text — heuristic + optional manual swap). Call `gemini-2.5-flash-image` with the source photo + `prompts/nano_banana.txt`:

> *put a SINGULAR realistic display setup for the product here inside of `{environment}`. ensure it is the main focus and there isn't anything in close proximity to the product. ENSURE THERE ARE NO PRICE TAGS.*

The `{environment}` placeholder is filled per product. A small Gemini text call (`gemini-2.5-flash`, see §4.6) inspects the product title + Kalodata stats and returns a category-appropriate retail setting — e.g., appliances → *"a modern small-appliance showroom"*, pet supplies → *"a premium pet supplies store"*, hardware → *"a hardware store aisle"*. Falls back to *"a curated specialty retail store"* if the text call fails. This is one round-trip per product and is bundled with the hook+caption generation in §4.6, so no extra API call.

Save output to `outputs/<run-id>/staged/<product-slug>.png`. Retry once on a generation failure; mark and skip on second failure. Roughly $0.04 per image.

### 4.4 Veo animation — backend is swappable

Two implementations behind one interface. Pick via `video_backend` in `config/settings.yaml`.

**Backend A — Google Flow via Playwright (default, cheapest)**

Drive `labs.google/fx/tools/flow` in headed Chromium with persistent storage state in `.auth/flow.json`. Same login pattern as Kalodata: client signs in once, cookies persist.

For each staged image:
1. Open a new Flow project (or append to a daily project)
2. Upload the Nano Banana output as an image input
3. Paste the prompt from `prompts/veo.txt`
4. Pick model = `Veo 3.1 Fast`, duration = 8s, aspect = 9:16
5. Click Generate, wait for the queue to complete, download the MP4
6. Save to `outputs/<run-id>/videos/<product-slug>.mp4`

**Why this is cheapest:** Google AI Ultra ($249.99/mo, 25,000 credits) → Veo 3.1 Fast at ~10 credits/clip → ~$0.10/clip if you fill the credits, ~$0.42/clip at 600 clips/mo. Compare to direct API at $1.20/clip for the same model.

**Operational caveats — surface these to the client before they commit:**

- **ToS gray area.** Flow is a consumer Labs product; Google's terms generally prohibit automated/scripted use. Risk: account suspension. Mitigation: human-pace delays, headed browser, single-user account, don't share credentials.
- **Reliability.** Flow's UI changes with no notice. The selector smoke test from the Kalodata scraper applies here — run it on session start and warn on drift.
- **Throughput ceiling.** Flow has visible queue times (1–4 min/clip on Fast). Parallelism is limited by what one logged-in session can hold. Realistically ~10–15 clips/hour, not 4-in-parallel like the API.
- **No retries or programmatic error surface.** A failed generation in Flow is a UI state, not an HTTP code. Build retry as "if download didn't appear in N minutes, click regenerate."
- **Aspect ratio + audio defaults.** Confirm the Flow project template matches what the client wants before each batch.

**Backend B — Direct Veo API (fallback)**

Same `google-genai` SDK call we originally scoped. Reasons to switch to it:
- Flow account gets rate-limited or suspended
- Volume needs to scale past ~300/day (where API throughput beats UI throughput)
- Client wants audit-trail logs / structured error handling

Pricing reminder: Veo 3 Fast direct = $1.20/clip, Veo 3.1 Light direct = $0.40/clip.

### 4.5 Output

Manifest written at run end:

```json
{
  "run_id": "2026-05-12_1430_beauty-us-7d",
  "image_model": "gemini-2.5-flash-image",
  "video_model": "veo-3.0-fast-generate-001",
  "products": [
    {
      "id": "...", "title": "...", "slug": "...",
      "raw": "outputs/<run-id>/raw/<slug>.png",
      "staged": "outputs/<run-id>/staged/<slug>.png",
      "video": "outputs/<run-id>/videos/<slug>.mp4",
      "captioned_video": "outputs/<run-id>/captioned/<slug>.mp4",
      "environment": "a premium pet supplies store",
      "hook": "Sold out in 24 hours",
      "post_caption": "This pet bowl is going viral on TikTok... #pets #tiktokmademebuyit",
      "nano_prompt_used": "put a SINGULAR realistic display setup ... inside of a premium pet supplies store. ...",
      "veo_prompt_used": "bring the camera closer to the product ...",
      "status": "ok"
    }
  ]
}
```

Streamlit then shows a results gallery (see §4.7) with per-product preview, post-caption, and a regenerate panel.

### 4.6 Hook + post caption (Gemini text)

Before restage, one `gemini-2.5-flash` call per product turns the Kalodata row (title + Revenue + Item Sold + growth + price + creator count) into three things:

- `hook` — a 3-to-7-word punchy line burned into the MP4 as a top-centered overlay by ffmpeg (`src/generators/captions.py`). White text, heavy black outline, font size relative to video height so 9:16 and 1:1 both look right.
- `caption` — a 1–3-sentence post body with 4–6 hashtags. Passed verbatim to the multi-platform poster (Blotato is the leading candidate; see §5).
- `environment` — feeds the `{environment}` placeholder in the Nano Banana prompt (§4.3).

Cost is negligible (~$0.0001/product) and the same call is what makes the boutique scene category-appropriate.

### 4.7 Per-product regenerate

Restage often misses on the first try (wrong scene, price tag bleed-through, weird composition). Rather than rerun the whole batch:

- Each product entry persists `nano_prompt_used`, `veo_prompt_used`, and `hook` in the manifest.
- The Streamlit results gallery shows an **Edit prompts & regenerate** expander under every clip with editable fields for all three.
- Checkboxes pick which stages to redo (Image / Video / Caption). Skipping Video saves the ~$1.20 Veo cost when only the boutique scene was wrong.
- **Regenerate this product** shells out to `scripts/regenerate_product.py`, which deletes the targeted product's outputs, re-runs the selected stages with the overridden prompts, and rewrites that one manifest entry. The rest of the run is untouched.

This closes the most common failure mode (Nano Banana wrong scene) without forcing a full batch rerun.

---

## 5. Deployment recommendation: local Mac/PC app

You picked local — here's how to make it client-friendly:

1. **Installer**: a `start.command` (mac) / `start.bat` (windows) that:
   - Creates a `uv` venv if missing
   - Installs `requirements.txt`
   - Installs Playwright Chromium
   - Launches Streamlit on `localhost:8765` and opens the browser
2. **First-run wizard** in the Streamlit app: prompts for Google API key, opens the Kalodata login flow, picks an output folder.
3. **Updates**: keep the project in a private GitHub repo; an "Update" button in the UI runs `git pull && pip install -r requirements.txt`.

**When to graduate from local:**
- Multi-user / team access → Streamlit Cloud or Render with auth
- Runs that need to fire on a schedule overnight without their laptop open → small VPS or Mac mini
- Kalodata starts blocking residential IPs → cloud VM with rotating residential proxies

For now, local is right.

---

## 6. Cost model

Pricing as of May 2026 — verify against [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) and [Google AI plans](https://one.google.com/about/google-ai-plans/) before quoting.

**Per-clip economics at 600 videos/month (20/workday × 22 = 600/mo, the client's target):**

| Backend | Subscription | Effective $/clip | 600/mo total | Notes |
|---|---|---|---|---|
| **Flow (Playwright) — default** | Google AI Ultra $249.99/mo | **$0.42** | **$250 + ~$24 image gen = $274** | 25,000 credits ÷ ~10 cr/clip = 2,500 clips capped, well above 600 |
| Flow (Playwright) at scale | Same Ultra plan | $0.10 if you fill credits | — | Only if volume hits ~2,500/mo |
| Veo 3.1 Light direct API | none | $0.40 | $240 + $24 = $264 | Closest API match to Flow's per-clip price |
| Veo 3 Fast direct API | none | $1.20 | $720 + $24 = $744 | Best quality automation, no ToS risk |
| Veo 3 Standard direct API | none | $3.20 | $1,920 + $24 = $1,944 | Almost never worth it for batch ads |

Image gen (Nano Banana Pro) is ~$0.04/image regardless of video backend → ~$24/mo at 600 images.

**Recommendation per scenario:**

| Client priority | Pick |
|---|---|
| Lowest cost, ok with Playwright fragility | Flow + Playwright |
| Lowest cost, full API reliability | Veo 3.1 Light direct API |
| Best quality, full automation | Veo 3 Fast direct API |
| Highest possible quality, low volume | Veo 3 Standard direct API |

---

## 7. Build phases

**Phase 1 — Kalodata scraper proof of concept (highest risk, do first)**
- Playwright login + cookie persistence
- One filter combo → list of products → photos + specs → JSON dump
- Validates that scraping is feasible before writing any UI

**Phase 2 — Generation backbone**
- `nano_banana.py` and `veo.py` with hardcoded inputs
- Confirms API access works and output quality matches the prompts
- Produces 1–3 sample videos to show the client

**Phase 3 — Pipeline + SQLite**
- Glue Phase 1 + 2 with run state, retry, and the manifest

**Phase 4 — Streamlit UI**
- Filter form, review grid with cost preview, generate button, results page

**Phase 5 — Packaging**
- `start.command`, README, first-run wizard, error surfacing

Total: ~20–35 hours of build, depending on how aggressive Kalodata is about anti-bot. Phase 1 is the tell — if scraping works smoothly in headed Playwright, the rest is mechanical.

---

## 8. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Kalodata changes their DOM | High | Selector smoke test on session start; centralize selectors in one file |
| **Flow UI changes break the generator** | **High** | **Same selector smoke test pattern; keep `veo_api_fast` backend hot as fallback** |
| **Flow account flagged for automated use (ToS)** | **Medium** | **Headed browser, human-pace delays (15–60s between clips), single account per machine, no credential sharing. If suspended, switch to `veo_api_fast` backend.** |
| Kalodata anti-bot blocks the session | Medium | Headed browser + persistent cookies + human-pace delays; fallback to manual cookie injection |
| Cost overrun | Medium | Mandatory approval gate with cost estimate; per-run hard cap in `settings.yaml` |
| Nano Banana adds price tags / wrong scene | Medium | Per-product regenerate UI with editable prompts (§4.7); prompt engineering iterations; environment picked per product (§4.3) |
| Client API key leaks | Low | `.env` gitignored, never logged, never sent off-machine |
| Playwright breaks on Chrome update | Low | Pin Playwright version; client runs `playwright install` after updates |

---

## 9. Open questions for the client

1. Output aspect ratio — 9:16 vertical only, or also 1:1 / 16:9?
2. Per-run budget cap (hard ceiling that aborts further generations)?
3. Where should the output folder live by default? (Desktop / Dropbox / Google Drive sync folder)
4. ~~Do they want a "regenerate this one" button on individual videos, or fire-and-forget?~~ — **Built.** Per-product regenerate with editable prompts is live (§4.7).
5. Naming convention for output files — product slug, GMV rank, date?
6. Posting backend — TikTok Content Posting API direct, or Blotato multi-platform? (See §4.6 for where the post caption is generated.)
