# Phase 1 Verification Runbook

**For Claude Code.** Open this project in Claude Code and say "run RUN_PHASE_1.md" (or just type `/test-scraper` if the slash command is installed). This document tells Claude Code exactly what to do, what to check at each step, and when to hand control back to the human.

---

## Goal

Verify that the Kalodata scraper works end-to-end against the live site:
1. Dependencies install cleanly
2. Login flow saves a reusable session cookie
3. `search_products` returns a populated table
4. `fetch_product_assets` downloads real photos to disk

If any step fails, fix it (mostly by patching `src/scraper/selectors.py`) before moving to the next.

---

## Step 0 — Sanity check the environment

Run from the project root:

```bash
pwd                                  # confirm we're in KaloData/
python3 --version                    # need 3.11+
ls -la .env 2>/dev/null || echo "no .env yet"
```

If `python3` is < 3.11, stop and tell the human to install a newer Python (e.g. `brew install python@3.12`).

If `.env` is missing, copy from the example:

```bash
cp .env.example .env
```

For Phase 1 we don't need any keys — just confirm the file exists.

---

## Step 1 — Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

This takes 2–4 minutes the first time. Watch for:
- **Resolver conflicts:** if pip complains about incompatible versions, stop and report the exact conflict to the human.
- **Playwright install failures:** the most common one is missing system libs. On macOS you usually don't need anything extra; on Linux run `python -m playwright install-deps chromium`.

Verify the install worked:

```bash
python -c "import playwright, httpx, streamlit; print('imports ok')"
```

---

## Step 2 — One-time Kalodata login (HUMAN STEP)

```bash
python scripts/login_kalodata.py
```

A Chromium window opens. **Stop here and tell the human:** "Please sign into Kalodata in the browser that just opened. The script will detect when you're in and save the session." The script polls for up to 5 minutes.

When the script prints `[auth] login detected, saving session`, confirm `.auth/kalodata.json` exists:

```bash
ls -la .auth/kalodata.json
```

If login times out, ask the human if they need a longer window and re-run with a higher timeout (edit `manual_login_timeout_s` in `scripts/login_kalodata.py`).

---

## Step 3 — Search-only smoke test

```bash
python scripts/scrape_test.py --max-results 5 --category Beauty --region US
```

Expected output: a table of up to 5 products with GMV / Sales / Growth / Title columns, then `[smoke] wrote outputs/_smoke/products.json`.

### What to do based on what you see

**Empty table (`[smoke] found 0 products`):**
The selectors in `src/scraper/selectors.py` don't match Kalodata's current DOM. Go to Step 4.

**Browser opens but hangs on filter application (`[filters] could not set ...`):**
Same — selectors need updating. Go to Step 4.

**Captcha message (`Kalodata is showing a captcha`):**
Tell the human, ask them to solve it manually in the browser, then re-run the smoke test.

**Browser shows login page instead of products:**
Cookie expired. Go back to Step 2.

**Table looks wrong (titles are URLs, GMV is missing, etc.):**
Selectors partially match. Read `outputs/_smoke/products.json` to see what got captured, identify which fields are off, then go to Step 4 and fix the relevant selectors.

**Table looks right:**
Move to Step 5.

---

## Step 4 — Patch selectors interactively (loops with Step 3)

```bash
python scripts/inspect_kalodata.py
```

This opens Chromium with the Playwright Inspector attached. **Stop and tell the human:** "Use the Inspector's 'Pick locator' button (top-left) to click on whichever element is broken. Copy the suggested locator into `src/scraper/selectors.py`."

Common patches:

| If broken | Where to fix in `src/scraper/selectors.py` |
|---|---|
| Filter dropdowns don't open | `FILTER_REGION_LABEL`, `FILTER_CATEGORY_LABEL`, `FILTER_TIME_WINDOW_LABEL` |
| Min-GMV / Min-sales fields don't fill | `FILTER_MIN_GMV_PLACEHOLDER`, `FILTER_MIN_SALES_PLACEHOLDER` |
| Apply button click misses | `APPLY_FILTERS_BUTTON_TEXT` |
| Zero rows captured | `PRODUCT_ROW`, `PRODUCT_LINK_IN_ROW` |
| Pagination breaks | `NEXT_PAGE_BUTTON` |
| Photos not found on detail page | `PRODUCT_PHOTO_IMAGES` |

After each edit, re-run Step 3 to verify. Loop until the table looks right.

When the human is done with the Inspector, they close the Chromium window and the script exits.

---

## Step 5 — Photo download test

```bash
python scripts/scrape_test.py --download 3 --category Beauty
```

Expected: search runs, then for each of the top 3 products you see something like:

```
  - Some Product Title: 5 photos -> outputs/_smoke/<slug>
```

Verify the files:

```bash
find outputs/_smoke -type f -name '*.jpg' -o -name '*.png' -o -name '*.webp' | head -20
```

If `0 photos` for everything, `PRODUCT_PHOTO_IMAGES` in `selectors.py` needs a fix — go back to Step 4 with focus on the product detail page.

If photos are present but tiny / corrupted, check whether Kalodata returns lazy-loaded images (the `src` attribute might be a placeholder). In that case the selector might need to read `data-src` instead of `src` — patch `_extract_product_id` / asset fetching logic in `src/scraper/kalodata.py` and re-test.

---

## Step 6 — Done check

Phase 1 is verified when **both** of these are true:
- `python scripts/scrape_test.py --max-results 10` prints a 10-row table with non-null GMV and Title for most rows
- `python scripts/scrape_test.py --download 3` produces real .jpg/.png files on disk

When that's the case, report to the human: "Phase 1 verified. Ready to start Phase 2 (Nano Banana + Flow generators)."

If you got here with selector edits, **commit them** so the next run starts clean:

```bash
git add src/scraper/selectors.py
git commit -m "Phase 1: live-site selector patches"
```

(If the project isn't a git repo yet, skip the commit and just tell the human which lines you changed.)

---

## Failure modes worth flagging to the human upfront

1. **Kalodata starts requiring 2FA every login** — increases manual friction. Workaround: keep the browser session alive longer (Playwright's `storage_state` already does this, but the cookie's TTL is set by Kalodata).
2. **Cloudflare bot challenge appears** — the headed browser usually passes it, but if it sticks, we may need to add a "solve challenge" pause before scraping.
3. **Numeric specs don't parse** — Kalodata uses K/M/B suffixes ("$12.5K GMV"). The regex in `kalodata.py` doesn't handle those yet. If you see lots of `None` GMVs, patch `_first_currency` to handle suffixes.

For any of these, stop and tell the human what you saw — don't try to work around them silently.
