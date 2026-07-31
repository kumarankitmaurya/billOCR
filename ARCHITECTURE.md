# Design Strategies

A catalogue of the deliberate techniques used in this codebase, why each was chosen, and what it costs. Written for someone modifying the code who needs to know which decisions are load-bearing.

---

## 1. Two-tier extraction with graceful degradation

**Where:** `app/routers/bills.py:16-28`

Gemini is tried first; *any* exception falls through to Tesseract. The engine that actually ran is recorded on the result (`ExtractionResult.engine`) and surfaced in both the UI badge and the Excel summary sheet.

```
Gemini (accurate, needs key + network)  ──fail──▶  Tesseract (local, always available)
```

**Why:** the service stays useful with no API key, no network, or an expired quota. A user who never opens the settings panel still gets output.

**Cost:** the `except Exception` is deliberately broad, so a genuine bug inside `gemini_ocr.py` degrades silently to worse output instead of surfacing. The `logger.warning` is the only signal. If you are debugging "why is it always using Tesseract", read the logs first — that warning line carries the real cause.

---

## 2. The Pydantic schema *is* the prompt

**Where:** `app/models.py`, consumed at `app/services/gemini_ocr.py:45`

`BillData` serves three roles simultaneously: Gemini's `response_schema`, the API response type, and the Excel exporter's input. The `Field(description=...)` strings are not documentation — they are extraction instructions the model reads.

**Why:** one definition, no drift. Editing a field description changes extraction behaviour, the OpenAPI docs, and the response contract in a single edit. It also removes text parsing from the primary path entirely: Gemini returns conforming JSON rather than prose to be regexed.

**Implication:** if you add a field, write the `description` as an instruction to the model ("HSN or SAC code, if present"), not as a note to a developer.

The response is re-validated with `model_validate_json` (`gemini_ocr.py:51`) even though the SDK already enforces the schema — cheap insurance that yields a real typed object rather than trusting the transport.

---

## 3. Nullable-by-default, never guess

**Where:** `app/models.py:29-45`, prompt rule at `app/services/gemini_ocr.py:20`

Every field except `line_items` is optional, and the prompt explicitly says *"If a field is not present on the bill, leave it null — do not guess."*

**Why:** bill layouts vary enormously — a market receipt has no HSN code, a service invoice has no unit. A schema demanding those fields forces the model to hallucinate. For financial data, a blank cell is honest and a fabricated number is a liability.

Only `item_name`, `quantity`, `rate`, `total` are required, and only on `LineItem` — the minimum for a row to mean anything.

---

## 4. Binarisation before OCR

**Where:** `app/services/tesseract_ocr.py:41-48`

Grayscale → autocontrast → hard threshold at 150.

**Why:** Tesseract is markedly more accurate on high-contrast bitonal input than on a photo with shadows and uneven lighting. Autocontrast first normalises exposure so the fixed 150 cutoff behaves consistently across differently-lit photos.

**Cost:** 150 is a magic constant tuned for typical printed receipts. Very faint thermal-printer output or dark photographs may wash out entirely. This is the first knob to turn if the fallback returns empty text.

---

## 5. Vocabulary-tolerant regex, positional line items

**Where:** `app/services/tesseract_ocr.py:19-33`

Summary fields use synonym alternation and tolerant punctuation:

```python
r"(?:grand\s*total|total\s*amount|net\s*amount)\s*[:\-]?\s*₹?\$?\s*([\d,]+\.?\d*)"
```

Optional currency symbols, optional separators, flexible whitespace — because OCR output spacing is unreliable and vendors phrase labels differently.

Line items instead match by *position*: `name → qty → rate → total`, anchored with `^`/`$` so partial matches don't produce garbage rows.

**Why positional:** a raw text dump has lost the table structure. Column order is the only surviving signal.

**Cost:** this is the weakest part of the fallback and it is expected to be. Multi-line item names, wrapped descriptions, and columns in a different order all fail to match — and a non-matching line is skipped silently, so items go missing rather than arriving wrong. Two mitigations are in place: `_to_float` strips thousands separators (`:35-38`), and a missing total is derived as `qty × rate` (`:70`).

---

## 6. Caller-supplied key beats server config

**Where:** `app/services/gemini_ocr.py:31` — `key = api_key or settings.gemini_api_key`

Precedence: per-request form field → environment/`.env`.

**Why:** supports both deployment shapes from one code path. A self-hoster sets `GEMINI_API_KEY` once and users never see a key prompt; a shared deployment ships no key and each user brings their own via the settings panel.

**Security posture:** the browser key lives in `localStorage` and is transmitted per request; it is never written to server-side storage or logs. On a shared deployment, note this still means users are sending their key to your server — it is trusted with the key for the duration of the request. Deploy over TLS.

---

## 7. In-memory buffers, no temp files

**Where:** `app/services/excel_export.py:105-125`, `app/routers/bills.py:18,51`

Uploads are read straight to `bytes`; the workbook is built into a `BytesIO` and handed to `StreamingResponse`. Nothing touches disk.

**Why:** no cleanup logic, no orphaned files, no leaking one user's bill into another's directory, and the service works on a read-only filesystem.

> **Dead config:** `settings.upload_dir` and the `os.makedirs` at `app/config.py:21,30` are vestigial — the directory is created at import but nothing ever writes to it. The original plan called for temp storage; the implementation went in-memory instead. Safe to delete both, along with the `uploads/` folder.

---

## 8. Excel structure and defensive sheet naming

**Where:** `app/services/excel_export.py`

One workbook, two sheets per bill (`Bill N Summary`, `Bill N Items`) so a multi-file upload yields a single download rather than a zip.

`_safe_sheet_title` (`:93-103`) enforces Excel's two hard constraints — titles ≤31 characters and unique within a workbook — by truncating, then suffixing `-2`, `-3` on collision. **These are format-level rules, not style choices:** violating either makes openpyxl raise or produces a file Excel refuses to open. Keep this function in the path if you change naming.

`_autofit` (`:37-41`) approximates column width from the longest cell value, with a floor of 10, since openpyxl has no true auto-fit.

---

## 9. Extract once, export from the result

**Where:** `app/routers/bills.py:41-53`, driven by `app/static/app.js:264-275`

The UI's two-step flow deliberately splits extraction from export:

```
/preview   images ──▶ JSON     (OCR runs here, once)
/workbook  JSON   ──▶ .xlsx    (no OCR — pure formatting)
```

The download button posts back the `ExtractionResult` JSON the client already received rather than re-uploading the images. `build_excel` consumes exactly the shape `/preview` emits, so no translation layer is needed.

**Why:** the obvious implementation — download re-posts the files to `/extract` — silently doubles the work. On the Gemini path that is a **second billed API call and a second multi-second wait for data the browser is already holding.** Verified via the fallback log line, which fires once per extraction: a preview-then-download cycle now logs one, not two.

`/extract` (`:56-66`) is retained as a one-shot `images → .xlsx` path for API clients and `curl` that don't want a preview round-trip. Both routes funnel through `_xlsx_response`, and their output is byte-identical for the same input.

**Consequence — staleness must be handled.** Because the export now ships precisely what's on screen, an edited file selection would otherwise let a user download a table that doesn't match their files. `invalidateResults()` (`app.js:76-84`) drops `lastResults` and the rendered table together on every mutation of the selection, so the two can never diverge.

---

## 10. Route order: API before static mount

**Where:** `app/main.py:29-33` — `include_router` precedes `app.mount("/static", ...)`

FastAPI matches routes in registration order. The API router is registered first so a static mount can never shadow an endpoint. `/` is a separate explicit `FileResponse` handler (`:36-39`) rather than a `StaticFiles(html=True)` mount at root, which would have swallowed unmatched API paths and returned HTML where JSON was expected.

---

## 11. Frontend: escape at the boundary

**Where:** `app/static/app.js:252-256`

```js
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}
```

Every OCR-derived string is passed through this before reaching `innerHTML` (`:195,208,230,232`). **This matters more than it looks:** the text originates from an uploaded image via an LLM, so it is fully attacker-controlled — a crafted bill image can carry markup. The DOM round-trip delegates escaping to the browser rather than a hand-rolled replace chain.

Object URLs are revoked after use (`:102,288`) so repeated uploads don't leak blobs.

---

## Known trade-offs and open issues

These are real, currently in the code, and worth fixing before production use.

### Blocking calls inside async handlers

`gemini_ocr.extract_bill_data` (network I/O) and `tesseract_ocr.extract_bill_data` (CPU-bound subprocess) are synchronous, called directly from `async def` handlers (`bills.py:21,27`). Each blocks the event loop for its full duration — seconds — during which the server serves no other request.

Fix: `await run_in_threadpool(...)` from `starlette.concurrency`, or declare the handlers `def` instead of `async def` and let FastAPI move them to its threadpool automatically.

### Multi-file uploads process sequentially

`[await _extract_one(file, api_key) for file in files]` (`bills.py:38,48`) awaits each file in turn, so ten bills take ten times one bill. These are independent and network-bound — `asyncio.gather` (once the blocking issue above is resolved) would make total latency roughly that of the slowest file.

### No upload limits

Neither endpoint caps file size, file count, or validates that content is really an image. A large or malformed upload is read fully into memory. Add a size guard and a content-type allowlist before exposing this publicly.
