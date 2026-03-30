# Daily Tip Allocation Dashboard

Small **local** web app: pull payments from the **Clover REST API**, enter employee shift blocks, **split tips** by who was on the clock at each tipped transaction, then **confirm** days, **persist** to SQLite, and **email** employees and the manager.

## Setup

### 1. Environment variables

**Clover (required)**

| Variable | Example |
|----------|---------|
| `CLOVER_BASE_URL` | `https://api.clover.com` or `https://api.clover.com/v3` |
| `CLOVER_MERCHANT_ID` | Your 13-character merchant id |
| `CLOVER_API_TOKEN` | Bearer token (private / OAuth access token for that merchant) |

**Business day & shifts (important on Render)**

| Variable | Example | Purpose |
|----------|---------|--------|
| `APP_TIMEZONE` | `America/New_York` | [IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for “which calendar day is this?” and for interpreting shift `HH:MM`. **Set this on Render** — the server defaults to **UTC**, so without it your shifts won’t line up with real payment times and tips can show **Unassigned**. |

**Email — pick one transport**

| Mode | Variables | When to use |
|------|-----------|-------------|
| **SMTP** | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` | **Local / laptop** (Gmail app password, etc.) |
| **Resend API** | `RESEND_API_KEY`, `RESEND_FROM_EMAIL` (or reuse `SMTP_FROM_EMAIL`) | **Render free tier** — Render [blocks outbound SMTP](https://render.com/changelog/free-web-services-will-no-longer-allow-outbound-traffic-to-smtp-ports) on free web services; HTTPS APIs still work. Sign up at [resend.com](https://resend.com), create an API key, verify a domain or use their test sender. |

If **`RESEND_API_KEY`** is set, the app **uses Resend only** and ignores SMTP for sending.

**Gmail SMTP:** use an [App password](https://support.google.com/accounts/answer/185833) for `SMTP_PASSWORD` if 2-step verification is on. After editing `.env`, **restart uvicorn** so new variables load.

See **`tip_dashboard/.env.example`** for a template.

Optional: put a `.env` file in **`tip_dashboard/`** or in the parent **`CLOVER_Tips/`** folder. On startup the app loads **both** (parent first, then `tip_dashboard/` so local values win).

**Optional — cloud / Render (same names in Render → Environment):**

| Variable | Purpose |
|----------|---------|
| `MANAGER_EMAIL` | If set (non-empty), overwrites the manager address in SQLite **on each server start** (handy when you do not rely on the UI to set it). |
| `TEST_MODE_EMAIL_ONLY` | If this variable **exists**, forces test mode on startup: `true` / `1` / `yes` / `on` = on; anything else = off. If **omitted**, the database value (or UI) is left as-is. |

> The UI still stores **employee** emails and **test mode** in SQLite. `MANAGER_EMAIL` / `TEST_MODE_EMAIL_ONLY` are optional **startup overrides** for hosting.

### 2. Install and run

```bash
cd "/path/to/CLOVER_Tips/tip_dashboard"
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** in your browser.

### 3. SQLite database

Confirmed days and email settings live in **`tip_dashboard/tip_board.db`** (created on first run). The file is listed in `.gitignore` so it is not committed.

On first run, the manager email is seeded to **`CATHYZHANG0404@GMAIL.COM`**; change it under **Employee Email Settings** and click **Save**.

> **Cloud note (e.g. Render free tier):** The server disk is usually **ephemeral**. SQLite works for trying the app online, but **`tip_board.db` can be wiped** on redeploy, sleep, or instance replacement. For long-term production history, plan on **Postgres** (or another database) or a **paid persistent disk** — not required for this minimal deploy.

## Deploy on Render (free tier)

Minimal steps: push this app to GitHub, create a **Web Service**, point Render at the **`tip_dashboard`** folder if your repo is larger than this app, set env vars, deploy.

### A. Push code to GitHub

1. Create a new repository on GitHub (empty, no README needed).
2. In a terminal, from your machine (adjust paths if needed):

```bash
cd "/path/to/CLOVER_Tips/tip_dashboard"
git init
git add .
git commit -m "Tips Board for Render"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

If your Git repo is the whole **`ds_projects`** (or **`CLOVER_Tips`**) folder instead, that is fine — in Render you will set **Root Directory** to **`CLOVER_Tips/tip_dashboard`** so build/start run in the right place.

### B. Create the Web Service on Render

1. Log in at [render.com](https://render.com) and connect GitHub.
2. **New +** → **Web Service**.
3. Select your repository.
4. Configure:

| Field | Value |
|--------|--------|
| **Name** | Anything (e.g. `tips-board`) |
| **Region** | Choose closest to you |
| **Branch** | `main` (or your default branch) |
| **Root Directory** | `CLOVER_Tips/tip_dashboard` **only if** the repo root is **not** already `tip_dashboard`. If the repo **is** only this app, leave **Root Directory empty**. |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |

5. **Instance type:** **Free**. (On Render, **free** web services need a **public** GitHub repo; private repos usually require a paid instance type.)

6. **Environment** → **Add Environment Variable** for each secret (names must match exactly):

   - `CLOVER_BASE_URL`
   - `CLOVER_MERCHANT_ID`
   - `CLOVER_API_TOKEN`
   - **`APP_TIMEZONE`** (e.g. `America/New_York`) — **required** for correct tips on Render (UTC default)
   - **On Render free:** `RESEND_API_KEY` + `RESEND_FROM_EMAIL` (SMTP to Gmail **will not work** — ports blocked).
   - **Local / paid Render:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`
   - Optional: `MANAGER_EMAIL`, `TEST_MODE_EMAIL_ONLY` (see table above)

   Use **Secret** / **mask** toggles for tokens and passwords.

7. **Create Web Service** and wait for the first deploy to finish.

8. Open the **`.onrender.com`** URL Render shows. The homepage should load. Optional check: `https://YOUR-SERVICE.onrender.com/health` should return `{"ok":true}`.

**Cold starts:** Free services **sleep** after inactivity; the first request after sleep can take ~30–60 seconds.

### C. Optional: Blueprint (`render.yaml`)

This repo includes **`render.yaml`**. You can use **New +** → **Blueprint** and connect the repo, or ignore it and use the manual Web Service steps above. If Render’s Blueprint UI asks for **Root Directory**, set it the same way as in the table.

### D. Deployment checklist

- [ ] App runs locally with `uvicorn main:app --reload --host 127.0.0.1 --port 8000`
- [ ] `requirements.txt` is up to date (committed)
- [ ] Code pushed to GitHub
- [ ] Render Web Service created with correct **Root Directory** (if monorepo)
- [ ] **Build Command:** `pip install -r requirements.txt`
- [ ] **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
- [ ] All required env vars set on Render (Clover + SMTP at minimum)
- [ ] Deploy succeeded (green live)
- [ ] Homepage loads on the Render URL
- [ ] **Refresh Clover data** works on the Daily tab
- [ ] **Confirm & Send** (save + send emails) works (SMTP / Resend configured)

## UI tabs

| Tab | Purpose |
|-----|---------|
| **Daily Tips Board** | Date, Clover refresh, shifts, manual splits, **Calculate**, exports |
| **Confirm & Send** | Preview, **Confirm day (save)**, then **Send emails** (two steps) |
| **Weekly Summary** | **Monday–Sunday** week; pick any day in the week — hours by employee (confirmed only) + CSV |
| **Two-Week Summary** | Two full **Mon–Sun** weeks from the Monday of the week you pick + CSV |
| **Employee Email Settings** | Manager email, per-employee emails, **Test mode** toggle |

**Test mode:** When enabled, a yellow banner appears. All employee and manager messages go to the **manager** inbox only; subjects are prefixed with `[TEST]` and include the intended employee name. When test mode is off, mail goes to each employee’s saved address; empty employee addresses **fall back to the manager** so you can test before everyone’s email is filled in.

## Daily workflow

1. On **Daily Tips Board**: pick date, **Refresh Clover data**, enter shifts, optional manual splits, **Calculate**.
2. Open **Confirm & Send**, set **Work date** (syncs from the daily date when you switch tabs), optional **Load preview**.
3. **Confirm day (save only)** — writes SQLite; no email. If that date was already saved, **409** → browser asks to **overwrite**.
4. **Send emails** — sends from **saved** data for that work date. If emails were already sent, **409** → choose **resend** to send again.  
   - Only employees with **at least one shift block** receive an email (even if tips are $0).  
   - One **manager summary** is sent with per-employee lines plus totals and reconciliation figures.

## API (for debugging)

**Clover & allocation**

- `GET /api/payments?date=YYYY-MM-DD`
- `POST /api/calculate` — body `{ "date", "shifts", "manual_rules" }`
- `POST /api/export/employees` — same body; CSV
- `POST /api/export/transactions` — same body; CSV

**Settings & confirm**

- `GET /api/settings` — employees, manager email, `test_mode`
- `POST /api/settings` — save (validates email format)
- `GET /api/confirm/status?date=YYYY-MM-DD`
- `POST /api/confirm/preview` — same body as calculate; preview JSON
- `POST /api/confirm/save` — same body as calculate + `overwrite`; saves SQLite only (no mail)
- `POST /api/confirm/send-emails` — body `{ "work_date": "YYYY-MM-DD", "resend": false }`; sends mail from saved rows; `resend: true` if already sent

**Summaries & CSV** (weeks are **Monday–Sunday**; any date in the week is normalized to that week’s Monday)

- `GET /api/summary/weekly?week_start=YYYY-MM-DD`
- `GET /api/summary/two-week?period_start=YYYY-MM-DD`
- `GET /api/export/weekly.csv?week_start=YYYY-MM-DD`
- `GET /api/export/two-week.csv?period_start=YYYY-MM-DD`

## Notes

- Clover `createdTime` is in **milliseconds**; the backend uses **`APP_TIMEZONE`** if set, otherwise the **machine’s local timezone**, for calendar-day bounds and shift matching.
- Comparisons use **minute** precision; shift start/end are **inclusive** for overlap.
- Tip splitting uses **whole cents**; remainder cents are assigned deterministically (alphabetical by name).
- Email sending is in **`email_service.py`** (Resend API if `RESEND_API_KEY`, else SMTP). If one employee send fails, others still run; the UI lists failures.
