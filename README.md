# KaloData → Nano Banana → Veo

Local desktop pipeline that pulls trending TikTok Shop products from Kalodata, restages each product photo into a boutique scene with Gemini 2.5 Flash Image (Nano Banana Pro), and animates it with Veo 3.

See `ARCHITECTURE.md` for the full design.

## Setup (one time)

1. Install Python 3.11+ and Google Chrome.
2. Copy `.env.example` to `.env` and fill in your `GOOGLE_API_KEY`.
3. Double-click `start.command` (Mac) or `start.bat` (Windows). First launch installs everything; subsequent launches are instant.
4. The app opens in your browser at `http://localhost:8765`. Log into Kalodata once when prompted — the cookie is saved to `.auth/`.

## Daily use

1. Pick a saved preset or set custom filters (category, region, time window, GMV/sales/growth).
2. Click **Search** — review the results grid.
3. Adjust the auto-selected products (check/uncheck rows).
4. Confirm the cost estimate and click **Generate**.
5. Finished MP4s land in `outputs/<run-id>/videos/`.

## Files you can edit without touching code

- `prompts/nano_banana.txt` — the boutique restage prompt
- `prompts/veo.txt` — the camera/hand motion prompt
- `config/presets.yaml` — saved filter combos
- `config/settings.yaml` — defaults (top-N, parallelism, cost estimates)

## Project status

**Phase 1 ready to test.** Kalodata scraper is implemented. Selectors are best-guess and will likely need a one-pass fix on the live site — that's expected for any scraper.

### Testing Phase 1 on your Mac

```bash
# 1. One-time setup
double-click start.command           # creates venv, installs Playwright, opens Streamlit
                                     # (or run the commands inside start.command manually)
cp .env.example .env                 # then fill in GOOGLE_API_KEY (optional for Phase 1)

# 2. Log into Kalodata once
python scripts/login_kalodata.py
# A Chromium window opens — sign in normally. Cookies save to .auth/kalodata.json

# 3. Run the smoke test (search only)
python scripts/scrape_test.py --category Beauty --region US --max-results 10
# Prints a table of products and writes outputs/_smoke/products.json

# 4. If selectors are off, find the right ones interactively
python scripts/inspect_kalodata.py
# Use the Playwright Inspector's "Pick locator" button to click broken elements,
# then update src/scraper/selectors.py with the suggested locators.

# 5. Once search works, test the asset download path
python scripts/scrape_test.py --category Beauty --download 3
# Pulls photos for the top 3 products into outputs/_smoke/<slug>/
```

### Phase progress

- [x] Phase 0 — Scaffold (this commit)
- [x] Phase 1 — Kalodata scraper (login + search + asset download)
- [ ] Phase 2 — Generator backbone (Nano Banana + Veo/Flow)
- [ ] Phase 3 — Pipeline + SQLite state
- [ ] Phase 4 — Streamlit UI (filters, review grid, generate button)
- [ ] Phase 5 — Packaging (start.command polish, first-run wizard)

See `ARCHITECTURE.md` § 7 for the full plan.
