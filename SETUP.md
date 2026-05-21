# Setup Guide

Walkthrough for getting the KaloData → ad-clip pipeline running on a new
machine. Designed to be followed live over Zoom / TeamViewer.

The whole thing is roughly: install 3 prerequisites → clone the repo →
paste one API key → double-click to build → sign in to two sites → fill in
3 affiliate codes. ~20–30 minutes including the build.

---

## 0. Prerequisites — install these first

| Need | How | Check |
|---|---|---|
| **Python 3.11+** | macOS: download from [python.org](https://www.python.org/downloads/). Windows: same, and tick **"Add Python to PATH"** in the installer. | Terminal/Command Prompt: `python3 --version` (Mac) / `python --version` (Windows) — must be 3.11 or higher |
| **Google Chrome** | [google.com/chrome](https://www.google.com/chrome/) | The app drives the real Chrome browser — it must be installed |
| **Git** | macOS: it installs itself the first time you run `git` (accept the popup). Windows: [git-scm.com](https://git-scm.com/download/win) | `git --version` |

Two accounts the client must already have:
- A **Kalodata** account (for scraping trending products)
- A **Google account with an AI subscription that includes Flow / Veo**
  (Google AI Ultra recommended — the pipeline budgets ~12k–20k credits/month)

---

## 1. Get the code

Open Terminal (Mac) or Command Prompt (Windows), then:

```
cd ~/Desktop
git clone https://github.com/lockemclaughlin-boop/kalodata-pipeline.git
cd kalodata-pipeline
```

This creates a `kalodata-pipeline` folder on the Desktop. Everything lives there.

---

## 2. Add the Gemini API key

1. Get a key at **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)** (free to create; usage is billed to that Google account).
2. In the `kalodata-pipeline` folder, copy `.env.example` to a new file named `.env`:
   - Mac: `cp .env.example .env`
   - Windows: `copy .env.example .env`
3. Open `.env` in TextEdit / Notepad and paste the key after `GOOGLE_API_KEY=`:
   ```
   GOOGLE_API_KEY=AIza...your-key...
   ```
4. Save and close. (`.env` is private — it never leaves the machine.)

---

## 3. First launch — builds the app

**Double-click `start.command`** (Mac) or **`start.bat`** (Windows).

- First run takes 2–3 minutes — it builds a private Python environment and
  installs everything (Playwright, etc.). A terminal window shows progress.
- When it finishes, the dashboard opens in your browser at
  `http://localhost:8765`.
- macOS may warn "unidentified developer" the first time — right-click
  `start.command` → **Open** → **Open**.

Leave the dashboard for now — close it or just switch away. Next we sign in.

---

## 4. Sign in to Kalodata and Flow

**Double-click `login.command`** (Mac) or **`login.bat`** (Windows).

It runs two sign-ins back to back:

1. **Kalodata** — a Chrome window opens. The client signs into Kalodata
   (handle any 2FA / "verify you're human" check). When the products page
   is visible, **close that Chrome window**.
2. **Google / Flow** — a second Chrome window opens. The client signs into
   the Google account with the AI subscription. When
   `labs.google/fx/tools/flow` shows the **New project** tile,
   **close that window**.

> The client should type their own passwords. On a screen-share, have them
> do this part themselves so passwords aren't exposed.

Both sessions are saved locally — this only needs doing once (until a login
expires, then just run `login.command` again).

---

## 5. Fill in the affiliate codes

Open `config/accounts.yaml` in a text editor. Fill in the three
`affiliate_tag:` lines with the TikTok Shop affiliate codes for each
account:

```yaml
  - handle: "uk"
    affiliate_tag: "PASTE-UK-CODE-HERE"
  - handle: "us1"
    affiliate_tag: "PASTE-US1-CODE-HERE"
  - handle: "us2"
    affiliate_tag: "PASTE-US2-CODE-HERE"
```

Save. (Without these, generated clips still work — the affiliate link field
in the export just stays blank.)

---

## 6. Daily use

1. Double-click **`start.command`** / **`start.bat`** → dashboard opens.
2. **Search** Kalodata for trending products, **star** the ones you want.
3. Click **Save selection**.
4. In the **Generate** panel, pick the **Flow** backend, click **Generate clips**.
5. When it finishes:
   - Finished videos: the **`Generations/`** folder — one dated subfolder per
     run, "Variation 1 …" then "Variation 2 …" clips inside.
   - In the dashboard, **Assign run to account** (UK / US 1 / US 2) copies the
     clips + posting data into `outputs/_posted/<account>/`.

---

## 7. Getting updates later

When there's a new version, in the `kalodata-pipeline` folder run:

```
git pull
```

Then launch as usual. If `git pull` reports a problem, contact the developer.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `start.command` won't open ("unidentified developer") | Right-click it → **Open** → **Open** |
| "Python not found" / wrong version | Reinstall Python 3.11+; on Windows tick "Add to PATH" |
| Dashboard says "Flow is not signed in" | Run `login.command` again |
| Kalodata shows "Page loading failed" | Session expired — run `login.command` again |
| A clip fails with a content / policy message | Normal — the pipeline auto-rewrites the prompt and retries once |
| Only ~10 products load from a search | Kalodata's free tier caps results server-side — that's expected |

For anything else, screenshot it and send it to the developer.
