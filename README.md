# Bill OCR → Excel Service

API-only backend for turning photos of textile trade bills into a
per-supplier Excel book matching the shop's existing ledger (one sheet per
company/mill, each bill a dated block of product/pcs/rate rows). Uses
**Google Gemini** or **Groq** (Qwen Vision) to read the bill; ingested bills
persist in Postgres (Neon) so the workbook can always be rebuilt or
re-downloaded later.

The frontend is a separate React app — **billOCR-ui** — in a sibling repo.
This service has no UI of its own; see `FRONTEND.md` for the API contract it
implements.

---

## 🐣 Setup for beginners (step-by-step)

If you've never run a Python web service before, follow these steps exactly, in order, from a terminal opened in this project folder.

### 1. Check Python is installed

```bash
python3 --version
```

You need Python 3.10 or newer. If this command fails, install Python from [python.org](https://www.python.org/downloads/) (or `brew install python3` on macOS).

### 2. Create a virtual environment

A virtual environment keeps this project's packages separate from everything else on your machine.

```bash
python3 -m venv venv
```

This creates a `venv/` folder in the project.

### 3. Activate the virtual environment

```bash
source venv/bin/activate      # macOS/Linux
venv\Scripts\activate         # Windows (Command Prompt)
```

Your terminal prompt should now start with `(venv)`. You'll need to run this activate command every time you open a new terminal to work on the project.

> **Troubleshooting (macOS + Homebrew)**: if step 2 fails with an error mentioning `ensurepip` or `pyexpat`/`XML_SetAllocTrackerActivationThreshold`, your Homebrew Python build is broken. Use a different installed version instead, e.g. `python3.12 -m venv venv` (check what's available with `ls /opt/homebrew/bin/python3.*`), then continue from step 3.

### 4. Install the project's dependencies

```bash
pip install -r requirements.txt
```

This reads `requirements.txt` and installs FastAPI, the Gemini and Groq SDKs, the Excel generation library, etc.

### 5. Set up your Gemini or Groq API key

Get a free key from [Google AI Studio](https://aistudio.google.com/apikey). You have two options:

- **Easiest**: skip this step and paste your key directly into the frontend's settings panel when it's running — it's saved in your browser and sent per-request, never stored server-side.
- **Or**, create a `.env` file so the server always has it:
  ```bash
  cp .env.example .env
  ```
  Then open `.env` and replace `your_api_key_here` with your real key.

### 5b. Set the app password (required)

Every endpoint requires an `X-App-Password` header. Without `APP_PASSWORD` set,
the service deliberately returns 503 to every request rather than serving the
whole book of record to anyone who finds the URL. Add a line to your `.env`:

```bash
APP_PASSWORD=pick-something-long-and-random
```

This is the password shop staff use. It is separate from the admin password
below, which gates base price — see `app/auth.py`.

### 5c. (Optional) Set an admin password for confidential search results

`GET /api/bills/search` never reveals base price (`rate`) — the shop's actual
cost — unless the request carries a matching `X-Admin-Password` header. This
is off by default. To turn it on, add a line to your `.env`:

```bash
ADMIN_PASSWORD=pick-something-only-you-know
```

Leave it unset (or absent) to keep admin search disabled entirely.

### 5d. Point it at a database (required)

The service has no local-file fallback — it needs Postgres. The free Neon plan
is enough (1GB storage, 100 compute-hours/month, no card). Copy the **pooled**
connection string (the hostname containing `-pooler`) into `.env`:

```bash
DATABASE_URL=postgresql://user:password@ep-xxxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require
```

Without it the app raises at startup rather than writing somewhere unexpected.

### 6. Run the server

```bash
uvicorn app.main:app --reload --port 8000
```

You should see output like `Uvicorn running on http://127.0.0.1:8000`. Keep this terminal window open — closing it stops the server. The schema is created in your Postgres database on first run, and the service writes no files to disk.

### 7. Run the frontend

This backend has no browsable UI of its own — clone and run **billOCR-ui**
(the sibling frontend repo) separately, pointed at `http://localhost:8000`.
See that repo's README for its own setup steps.

To stop this server, press `Ctrl+C` in the terminal.

---

## How it works

```mermaid
graph LR
    A["billOCR-ui (React, sibling repo)"] -->|Upload images + supplier| B["FastAPI Backend"]
    B -->|Vision extraction| C["Gemini / Groq"]
    C -->|"{supplier, bill_no, bill_date, articles[]}"| D["Postgres book of record"]
    D -->|Per-company sheets| E["Excel Generator"]
    E -->|.xlsx Download| A
```

1. You upload one or more bill images through billOCR-ui, optionally naming the supplier up front (see `HANDOVER.md`: supplier is chosen by the user, not read off the image, when given).
2. The backend sends each image to Gemini or Groq, asking it to split the bill's DESCRIPTION column into `company` (mill/brand) and `product` (design name), and return `{supplier, bill_no, bill_date, articles: [{company, product, pcs, rate}]}` — see `output-format.md` for the full contract.
3. Extracted bills are shown as a preview table before anything is saved.
4. On download, each bill is ingested into Postgres keyed by `(supplier, bill_no)` — re-ingesting the same bill replaces its lines rather than duplicating them — and the supplier's full workbook is rebuilt from the database: one sheet per company, each bill a dated block of `product | pcs | rate` rows, plus three optional per-line pricing fields set during review (`tax_pct`, `margin_pct`, `final_price`) that fill in columns D/E/F when present — see `ARCHITECTURE.md` for the schema and `output-format.md` for the column mapping.
5. Any ingested line can later be found again with `GET /api/bills/search` — by product name and/or price, across every supplier. Base price (`rate`) is confidential: it's included only for a request carrying a valid admin password.

## API

- `GET /api/bills/providers` — which OCR providers are configured, for the UI dropdown.
- `POST /api/bills/preview` — multipart form (`files`, optional `supplier`, `api_key`, `provider`) → JSON array of extracted bills. Runs no persistence.
- `POST /api/bills/ingest` — JSON body `{results, supplier}` (`results` from `/preview`) → persists into the book of record, returns `{ingested, suppliers}`.
- `GET /api/bills/workbook?supplier=NAME` → streams that supplier's full `.xlsx`, rebuilt from the database.
- `GET /api/bills/search` — query `name`/`min_final_price`/`max_final_price` (everyone) and `min_base_price`/`max_base_price` (admin only) → matching lines across every supplier. Send header `X-Admin-Password: <ADMIN_PASSWORD>` to also get `rate` (base price) back per result — omitted entirely otherwise. 401 on a wrong password, 403 if a base-price filter is sent without one.
- `POST /api/bills/extract` — multipart form, same as `/preview` plus ingestion → streams the resulting workbook directly. One-shot convenience path for scripts; requires the batch to resolve to exactly one supplier.

billOCR-ui uses `/preview` then `/ingest` + `/workbook`, so previewing costs a single extraction and the workbook always reflects everything ever ingested for that supplier, not just the current upload.

## Connecting the frontend

The simplest wiring, and the one that avoids CORS entirely: give **billOCR-ui**
a `vercel.json` that proxies the API to this service, so the browser only ever
talks to one origin.

```json
{
  "rewrites": [
    { "source": "/api/:path*", "destination": "https://<this-service>.vercel.app/api/:path*" }
  ]
}
```

The UI then calls `/api/bills/...` relative to itself and needs no
`VITE_API_BASE_URL` in production, and this service needs no `CORS_ORIGINS`.

Cross-origin also works — set `VITE_API_BASE_URL` on the frontend and
`CORS_ORIGINS` (or `CORS_ORIGIN_REGEX`, for preview URLs) here. It is two more
settings to keep in sync.

**Either way the frontend must send `X-App-Password` on every request.** Every
endpoint requires it; `/health` is the only exception.

## Deploying

Deployed as a **Vercel Function** (Python runtime) with **Neon Postgres**. The
service is stateless — uploads are read into memory and discarded, workbooks
are streamed from a buffer, nothing is written to disk — so it needs no
persistent volume.

Vercel resolves `app/main.py` automatically: the Python runtime looks for a
top-level `app` at that path, so the whole FastAPI app becomes one function.
`vercel.json` only sets `maxDuration` (OCR takes 10-20s per bill) and trims
the bundle. `.python-version` pins 3.12.

```bash
vercel deploy          # preview
vercel deploy --prod   # production
```

Environment variables to set in the project (Settings -> Environment Variables):

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Injected automatically by the Neon Marketplace integration. Use the **pooled** endpoint (`-pooler` in the hostname). |
| `APP_PASSWORD` | yes | Gates every endpoint. Without it the service returns 503 to everything. |
| `ADMIN_PASSWORD` | no | Additionally unlocks base price in `/search`. Must differ from `APP_PASSWORD`. |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | one of | A placeholder like `your_..._here` counts as unset. |
| `CORS_ORIGINS` | if cross-origin | Exact origins, comma-separated. |
| `CORS_ORIGIN_REGEX` | if cross-origin | For Vercel preview URLs, which change every deploy. Neither is needed if the frontend proxies `/api/*` here via a rewrite. |
| `DB_AUTO_INIT` | no | Defaults true. Set `false` after the first successful deploy. |

### Two limits worth knowing

**Request bodies cap at 4.5MB**, enforced by Vercel before any application
code runs. A 2.7MB phone photo fits; two in one request do not.
`MAX_REQUEST_BYTES` sits just under the platform limit so you get a readable
error instead of an opaque 413 — but the real fix is for billOCR-ui to
downscale images before upload. Server-side redaction cannot help here: it
runs after the body has already arrived.

**Function duration is 300s on Hobby** (default and maximum). Three bills at
10-20s each fits comfortably; local PII redaction (HANDOVER.md M3) will add
a few seconds per bill and needs a container image for its system
dependencies — see Vercel's Docker guide for Python.

### Running the tests against a database

The integration suite `TRUNCATE`s every table, so point it at a scratch Neon
**branch**, never the live one:

```bash
TEST_DATABASE_URL='<scratch branch pooled string>' ALLOW_REMOTE_TEST_DB=1 pytest
```

`ALLOW_REMOTE_TEST_DB=1` is required because `conftest.py` refuses any
`neon.tech` URL by default. The unit tests in `tests/unit/` need no database.

## Design notes

`ARCHITECTURE.md` documents the current system (DB schema, ingest/idempotency, OCR fallback, the price/margin fields). `implementation_plan.md` describes the original generic-invoice version of this tool and predates the textile-specific schema in `HANDOVER.md`/`output-format.md`/`workflow.md` — treat that one as historical, not current.

## Project structure

```
billOCR/
├── app/
│   ├── main.py               # FastAPI app, CORS, DB init (API-only, no UI)
│   ├── config.py             # Settings (env vars, .env)
│   ├── models.py             # Pydantic schemas (BillExtraction, Article)
│   ├── db.py                 # Postgres book of record (supplier/company/bill/line)
│   ├── routers/bills.py      # API endpoints
│   └── services/
│       ├── gemini_ocr.py     # Gemini Vision extraction
│       ├── groq_ocr.py       # Groq Qwen Vision extraction
│       └── excel_export.py   # openpyxl per-company book-ledger export
├── uploads/
├── requirements.txt
└── .env.example
```

The frontend (billOCR-ui) lives in its own repo, not here.
