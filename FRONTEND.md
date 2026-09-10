# Frontend : the scan surface

Companion to HANDOVER.md. The frontend owns a narrow slice — pick supplier,
capture the bill, review what was read, confirm. Everything privacy-sensitive
(redaction) and stateful (extraction, routing, the book) lives in the backend.
This doc is self-contained: screens, the API it depends on, and where the
implementation lives.

**The frontend is `billOCR-ui`** — a React + TypeScript PWA in a sibling
repo, not part of this one. This backend (`billOCR`) is API-only and has no
UI of its own (see `app/main.py`). §3 below is the *real, implemented* API
contract billOCR-ui depends on — nothing here is aspirational.

---

## 1. Scope

**Owns:** supplier entry · photo capture/upload · a review-and-correct grid ·
confirm · triggering the download.

**Does NOT own:** PII redaction (not yet built — see HANDOVER.md M3, still
stubbed), OCR/extraction, price/SP calc, the workbook layout. All of that is
backend. The frontend sends a supplier + image(s) and renders what comes back.

Why it matters: choosing the supplier up front is what lets the backend scrub
the whole PII header once redaction ships. The picker/field is not a
convenience — it's load-bearing.

---

## 2. Flow (today: synchronous request/response, not async job polling)

```
[Enter supplier] ─► [Capture/select files] ─► POST /api/bills/preview ─► [Review & correct]
                                                                              │
                                                                              ▼
                                                        POST /api/bills/ingest
                                                                              │
                                                                              ▼
                                          GET /api/bills/workbook (per supplier) ─► [Downloaded]
```

There is no ingest job / job id / polling today — each step is one HTTP call
the client awaits directly. If a redaction/reconciliation pipeline is added
later (HANDOVER.md M2/M3), this will likely become async and this section
should be updated alongside it; until then, don't build a polling UI against
an endpoint that doesn't exist.

### Screens
1. **Supplier** — a free-text field (not a picker from a backend list — there
   is no `/suppliers` endpoint). Persisted in `localStorage` so it carries
   across bills in a session. If left blank, each bill's supplier is read off
   the image by the OCR model instead.
2. **Capture** — drag-and-drop, file browse, or camera capture; multi-file.
3. **Reading** — one `POST /api/bills/preview` call per batch; no
   stage-by-stage progress (there's no redact/extract/reconcile pipeline to
   report on yet — just the one call).
4. **Review & correct** — editable cards, one per bill: supplier/bill_no/
   bill_date header, an articles grid (`company | product | pcs | rate`)
   grouped by company, and a flag banner if `flags` came back non-empty. See
   §4 for the editing behavior in detail.
5. **Confirm & Download** — `POST /api/bills/ingest` persists every reviewed
   bill, then `GET /api/bills/workbook` is fetched once per distinct supplier
   in the response and offered as a download. There's no diff summary
   (`+new/seen/repriced`) returned or shown today — `ingest` only reports
   `{ingested, suppliers}`.

Non-goals for v1: offline queue, bulk multi-bill import, analytics.

---

## 3. API contract (what the frontend depends on)

Base path: `/api/bills`. Source of truth: `app/routers/bills.py`.

```
GET  /api/bills/providers
     -> [ { id: "auto"|"gemini"|"groq", name: string, available: boolean } ]
     Populates the provider dropdown; "available" reflects whether the
     server has that provider's API key configured.

POST /api/bills/preview          (multipart/form-data)
     fields: files[] (image files), supplier? (string), api_key? (string),
             provider? ("auto"|"gemini"|"groq", default "auto")
     -> ExtractionResult[]
        {
          source_filename: string,
          engine: "gemini" | "groq",
          bill: {
            supplier: string,
            bill_no: string | null,
            bill_date: string | null,   // "YYYY-MM-DD"
            articles: [ { company: string, product: string, pcs: int, rate: float,
                          tax_pct: float | null, margin_pct: float | null,
                          final_price: float | null } ]
          },
          flags: string[]               // bill-level, e.g. "missing bill_no"
        }
     Runs OCR only — nothing is persisted. `supplier`, if given, is trusted
     context (not read off the image); if omitted, each bill's supplier
     comes from the OCR result instead.

POST /api/bills/ingest           (application/json)
     body: { results: ExtractionResult[], supplier?: string }
     -> { ingested: number, suppliers: string[] }
     422 if any result is missing bill_no or bill_date — those are the DB's
     idempotency/sort key, never guessed. Re-ingesting the same
     (supplier, bill_no) replaces its lines rather than duplicating them.

GET  /api/bills/workbook?supplier=<name>
     -> streams <name>.xlsx (application/vnd.openxmlformats-...sheet)
     Rebuilds the workbook from the DB and also saves a copy server-side to
     OUTPUT_DIR/<name>.xlsx (see app/config.py). 404 if the supplier has no
     ingested bills.

POST /api/bills/extract          (multipart/form-data) — convenience only,
                                  not used by billOCR-ui
     fields: files[], supplier?, api_key?, provider?
     -> streams the workbook directly: OCR + ingest + workbook in one call.
     400 if the batch resolves to more than one supplier without an
     explicit `supplier` — use preview -> ingest -> workbook per supplier
     instead in that case.
```

Notes:
- `tax_pct`/`margin_pct`/`final_price` are always `null` straight out of OCR
  — none is printed on the bill (see output-format.md). All three are filled
  in on the review screen and flow through to `/ingest` in the same
  `ExtractionResult[]` shape as every other edit — no separate endpoint.
  `final_price` is mandatory before the review screen will let you save —
  enforced client-side only (billOCR-ui disables its save button; there is
  no corresponding backend 422 for it, unlike bill_no/bill_date below). At
  export, `/workbook` independently computes column D (tax *amount*) as
  `rate * tax_pct / 100` and column E (margin *amount*) as
  `(rate + tax_amount) * margin_pct / 100` — i.e. margin on the tax-inclusive
  price, matching the review screen's own chain — then writes `final_price`
  straight into column F. Any of the three is left blank if its underlying
  field wasn't set; column F is never recomputed from D/E, since
  `final_price` can be hand-overridden independently on the review screen.
- Everything is synchronous: no job id, no polling endpoint, no SSE.
- No redaction step exists yet — the frontend uploads the raw photo directly
  to `/preview`/`/extract`. Once HANDOVER.md M3 (local redaction) ships, this
  contract will need a pre-upload redaction step or a backend redaction pass;
  don't design around a `redact_boxes`/job-status shape that doesn't exist.
- 502 with a JSON `{"detail": "..."}` body if every configured OCR provider
  fails on a given image (bad photo, auth error, network error) — surface
  `detail` to the user rather than a generic failure message.
- CORS defaults to `*` (`app/config.py`), so billOCR-ui can call this backend
  from its own dev server/origin with no extra configuration.

---

## 4. Review-and-correct UX

OCR on a shadowed phone photo is noisy, so the review grid is where the
shopkeeper catches it — implemented in billOCR-ui as follows:

- Rows are grouped by company (matches how the book is organised); company
  is editable too, as a bulk rename — editing the group header renames every
  row in that group at once, so a misread mill name doesn't fork into a
  wrong sheet.
- Inline-edit `product`, `pcs`, `rate`, plus three shop-only pricing fields
  per line, in order: `tax_pct`, `margin_pct`, `final_price` (percents, never
  the raw amounts — the backend separately computes those at export time,
  see §3 notes). All three start blank; OCR never fills them. Entering
  `tax_pct` and/or `margin_pct` computes a default `final_price` client-side
  as `rate * (1 + tax_pct/100) * (1 + margin_pct/100)` (a missing percent is
  treated as 0) — but `final_price` stays a plain, independently-editable
  field after that: hand-adjusting it doesn't change `tax_pct`/`margin_pct`,
  and it isn't recomputed unless one of them is edited again. Same
  compute-a-default-then-hand-adjust pattern as the book's own tax/margin/SP
  columns (output-format.md), not a live formula. `final_price` is
  **mandatory** — the Save & Download button stays disabled, with a banner
  giving the count still blank, until every line on every bill in the batch
  has one (client-side only; see §3 notes). Numeric keypad for all five on
  mobile (`inputmode="numeric"`/`"decimal"`).
- `flags` is a flat `string[]` per bill (e.g. "missing bill_no"), not
  per-line with a `confidence` score or an `amount_mismatch` flag — the
  backend doesn't compute amount reconciliation yet (HANDOVER.md §4, open
  decision). billOCR-ui renders `flags` verbatim as a per-bill banner and
  deliberately does **not** build per-line confidence/amber-row/tooltip UI
  against a shape the backend doesn't return — if that ships later, this
  section and the UI both need updating together.
- A visible nudge (red outline) on `bill_no`/`bill_date` when either is
  null, since that's exactly what makes `/ingest` 422 — client-side, on top
  of the real validation, not a replacement for it.
- Row actions: delete a spurious row, add a missed one, add a whole new
  company group.
- Edited results still `POST /api/bills/ingest` in the same
  `ExtractionResult[]` shape from §3 — there is no separate `/confirm`
  endpoint, and none should be invented.
- The source image is **not** shown beside the grid — there is no
  redacted-image endpoint (see §3 notes), and until redaction ships
  billOCR-ui clears the raw upload from client state the instant OCR
  results come back, rather than keep it around to display.

---

## 5. Implementation (`billOCR-ui`)

Lives in the sibling repo, not in this one — a separate app, same API, no
backend changes required.

**Stack:** React + TypeScript, Vite, Tailwind, installable as a PWA
(`vite-plugin-pwa`). Camera capture via `<input type="file"
accept="image/*" capture="environment">`.

**Screens:** `Capture` (supplier field, provider/API-key settings, drag-drop
+ camera capture, thumbnails) → `Review` (the §4 grid) → `Done` (ingest
outcome + an explicit per-supplier download button per supplier, rather than
looping through auto-downloads, since browsers can block multiple
simultaneous auto-triggered downloads).

**State:** a single Zustand store — a `screen` enum drives which of the
three screens renders (no router: the flow is linear, matching §2's
diagram). Supplier/provider/API-key persist to `localStorage`; the uploaded
`File[]` and the editable OCR results live only in memory and are never
persisted.

**API layer:** `src/api/types.ts`/`client.ts` mirror `app/models.py` and
`app/routers/bills.py` field-for-field — `Article`, `BillExtraction`,
`ExtractionResult`, `IngestResponse`. If those Pydantic models change, these
are the two files to update.

**Mocking + tests:** MSW handlers (`src/mocks/`) implement the same 4
endpoints in-memory, seeded from this doc's own `output-format.md` example
(Dindayal Jalan / DJ-STI-36600 / JAI MATA DI SRT → SAGAR, SWIGGY, GREEN TEA),
so the UI is developable without the backend running (`VITE_USE_MOCK=true`).
Vitest + React Testing Library screen tests cover all three §6 acceptance
lines against those mocks. Also manually verified against this live backend
end-to-end (real OCR call, real ingest, real `.xlsx` written to
`OUTPUT_DIR`).

**Known deviations from a naive 1:1 spec port**, all deliberate:
- No diff summary on `Done` — `/ingest` only returns `{ingested, suppliers}`,
  so none is shown or invented.
- No per-line flag styling (amber rows, confidence underlines) — only the
  bill-level `flags[]` banner §4 describes, since that's all the backend
  returns today.
- No source-image panel in Review — no redacted-image endpoint exists yet.

If `/preview`/`/ingest` response shapes change (redaction, per-line
confidence, an `/confirm` split, async job status), both §3/§4 here and
`billOCR-ui/src/api/types.ts` need updating together — they're meant to stay
in lockstep with `app/models.py`.

---

## 6. Acceptance

- Enter (or leave blank and let OCR read) "Dindayal Jalan", select
  `IMG_20250817_155721.jpg`, click Extract, and reach a review grid with 6
  companies and their products (JAI MATA DI SRT → SAGAR/SWIGGY/GREEN TEA),
  editable in place.
- A row whose company is renamed moves into (or creates) the matching group.
- Clicking Download ingests the reviewed result(s) and downloads a
  `<supplier>.xlsx` that also now exists server-side under `OUTPUT_DIR`.
- A bill missing `bill_no`/`bill_date` is flagged in the grid and rejected at
  ingest (422) with a message naming the file, not silently guessed.
