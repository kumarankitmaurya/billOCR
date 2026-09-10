# Architecture

A catalogue of the deliberate techniques used in this codebase, why each was
chosen, and what it costs. Written for someone modifying the code who needs
to know which decisions are load-bearing. Companion docs: `HANDOVER.md`
(domain model, decisions log), `output-format.md` (exact field/column
contract), `FRONTEND.md` (the API contract billOCR-ui depends on).

---

## 1. Two repos, one contract

```
┌── billOCR-ui (sibling repo) ─────┐        ┌── billOCR (this repo) ───────────────┐
│ React + TS + Vite + Tailwind     │  HTTP  │  FastAPI, API-only (no served UI)    │
│ Capture → Review → Done          │ ─────► │  /api/bills/{preview,ingest,workbook}│
│ Zustand store, MSW mocks+tests   │ ◄───── │  SQLite book of record               │
└───────────────────────────────────┘  CORS *│  OCR (Gemini/Groq) → Excel export    │
                                              └────────────────────────────────────┘
```

**Where:** `app/main.py` (no static mount — this backend has no UI of its
own); `billOCR-ui/src/api/{types.ts,client.ts}`.

**Why:** the two repos are versioned and deployed independently, but share
one contract by convention, not by code generation — `app/models.py`
(Pydantic) and `billOCR-ui/src/api/types.ts` (TypeScript) are hand-kept in
lockstep. `FRONTEND.md` §3 is the narrative version of that contract; when
either side's shape changes, both files and that doc need updating together.

**Cost:** no compiler catches drift between the two type definitions. A
field renamed in `Article` silently breaks billOCR-ui only at runtime (a
`KeyError`-shaped bug, or a field that's always `undefined`), not at build
time. Grep both repos for the field name before renaming anything in
`app/models.py`.

---

## 2. The book of record: SQLite, not the Excel file

**Where:** `app/db.py`

```
supplier ──< company ──< line >── bill
(id, name)   (id, supplier_id,   (id, bill_id, company_id, product,
              name)                pcs, rate, line_order,
                                    final_price, margin_pct)
                          bill (id, supplier_id, bill_no, bill_date, entered_at)
```

- `supplier` and `company` are `get_or_create`d by exact-string name — there
  is no canonical ID a client can pass, no fuzzy matching, no merge tool.
  Two spellings of the same real-world supplier ("Dindayal Jalan" vs.
  "Dindayal Jalan Textiles Pvt.Ltd") are two different rows, fragmenting the
  book, with no automatic reconciliation. This has already happened in
  practice — the fix today is manual (delete/rename one supplier's rows).
- `bill` is `UNIQUE(supplier_id, bill_no)` — this is the idempotency key.
  `line` rows aren't unique on anything; a re-ingest deletes every `line`
  under the bill and reinserts from the incoming payload (`_upsert_bill` in
  `app/db.py`), so editing-then-resaving a bill is a full replace, not a
  diff.
- `final_price`/`margin_pct` (added for the review screen's price/margin
  fields — see §5) are nullable `REAL` columns with **no `CREATE TABLE IF
  NOT EXISTS` safety net** for existing databases: SQLite's `IF NOT EXISTS`
  only guards table creation, not new columns on a table that already
  exists. `init_db()` runs an explicit `PRAGMA table_info` check and `ALTER
  TABLE ... ADD COLUMN` for anything missing, every startup. **This is the
  pattern to follow for any future column added to an existing table** —
  don't assume `_SCHEMA`'s `CREATE TABLE IF NOT EXISTS` retrofits a live DB.

**Why SQLite as the source of truth, not the `.xlsx`:** the shop's shape
(per-company sheets, dated blocks) and a query-friendly relational shape
don't coincide — deriving the book view from a relational store is far
simpler than parsing it back out of a spreadsheet. `excel_export.py` is a
pure read: `build_supplier_workbook()` never writes to the DB.

**Cost:** the on-disk `.xlsx` files under `OUTPUT_DIR` are a cache, not a
record — if the shop hand-edits a downloaded workbook, that edit is invisible
to the system and gets silently clobbered the next time anyone re-downloads
that supplier's book. There is no import path from Excel back into SQLite.

---

## 3. OCR provider strategy: try, fall through, never guess

**Where:** `app/services/ocr_strategy.py`

```
provider="auto" ──► try_order from configured API keys ──► gemini ──fail──► groq ──fail──► raise last_error
```

Each provider is attempted in order (`gemini_ocr.py`, `groq_ocr.py`); any
exception moves to the next. The engine that actually ran is recorded on the
result (`ExtractionResult.engine`) and surfaced to the caller. If every
provider fails, the last exception propagates — `app/routers/bills.py`'s
`_extract_one` catches it there and turns it into a clean `502` with the
real reason in `detail`, rather than letting it reach FastAPI's default
handler as an unhandled 500 with a plain-text body (a real bug fixed this
way — see git history on `bills.py`).

**Why:** the service stays useful with whichever key is configured, and a
transient failure on one provider doesn't fail the whole request.

**Nullable-by-default, never guess** (`app/models.py`): `bill_no`/`bill_date`
are `str | None`, and both prompts (`gemini_ocr.py`, `groq_ocr.py`)
explicitly instruct "if not legible, return null — do NOT guess." The same
pattern now applies to `final_price`/`margin_pct` (§5): the prompts
explicitly say these are *never* on the bill and must always come back
`null`, because a model given an `Optional[float]` field with no other
instruction may still try to be helpful and compute something.

**Cost:** the response schema (`BillExtraction`/`Article`) doubles as
Gemini's `response_schema` *and* Groq's hand-written JSON-schema prompt text
*and* the `/ingest` request body shape. Add a field to `Article` and there
are three places that need the addition to stay in sync: the Pydantic model,
Groq's literal schema block in its prompt string, and (if the field must
never be OCR'd) an explicit "always null" instruction in both prompts —
Pydantic's `response_schema` alone does not stop Gemini from populating an
optional field with a plausible-looking guess.

---

## 4. Excel export: a generated view, one write path

**Where:** `app/services/excel_export.py`

```
GET /api/bills/workbook?supplier=X
  ├─► db.get_workbook_data(X)         # read-only: {company: [(bill_no, bill_date, [lines])]}
  ├─► build one sheet per company     # _write_company_sheet, dated blocks
  ├─► save to OUTPUT_DIR/X.xlsx       # side effect: a cache, not the record (§2)
  └─► stream the same bytes back      # the actual HTTP response
```

`build_supplier_workbook()` does the on-disk save and returns the in-memory
buffer in the same call — both come from one `Workbook` object, so the
downloaded file and the on-disk cache can never diverge from each other
(they can still diverge from the DB, per §2, if hand-edited afterward).

**Why one function does both:** simplicity — a supplier's workbook is cheap
enough to regenerate in full on every request rather than diffed/patched, so
there's no incremental-write path to keep correct.

**Column layout** (`_write_company_sheet`): A/B/C = product/pcs/rate always;
D = loading, still reserved and always blank (not captured anywhere yet); E
= margin *amount* = `rate * margin_pct / 100` when `margin_pct` was set on
that line, else blank; F = `final_price` exactly as entered, else blank. The
label row (`pc | price | . | margin | SP`) is written once per company
sheet, not once per bill block.

---

## 5. Price/margin fields: shop-only, never OCR'd, computed at the edge

**Where:** `app/models.py` (`Article.final_price`, `Article.margin_pct`),
`app/db.py` (`line.final_price`, `line.margin_pct`), `excel_export.py` (§4).

These two fields exist for exactly one reason: the shop's selling price and
margin are business decisions made by a person, never printed on a
supplier's bill (same category as the pre-existing SP concept in
`HANDOVER.md` §3 — "SP stays manual... the scan must not clobber it"). The
design consequence is that they:

- are **not** derived from `rate` anywhere upstream of the Excel export —
  the backend stores exactly what the review screen sends, no clamping, no
  sanity-checking against `rate`;
- are **not** part of what the OCR prompt asks for — they're declared on
  the shared `Article` schema (so `/ingest` can accept them) but the prompts
  explicitly instruct both providers to always return `null` for them (§3);
- are stored raw (`margin_pct` as a percent, not pre-multiplied) so that a
  later `rate` correction on the review screen doesn't leave a stale
  computed value sitting in the DB — the multiplication happens once, at
  export time, from whatever `rate`/`margin_pct` currently are.

**Cost / open question:** because `margin_pct` is normalized (stored as a
percent, multiplied at export) but `final_price` is stored raw (whatever the
user typed, no relationship to `rate`/`margin_pct` enforced), the two fields
can disagree — someone can set `margin_pct=20` on a ₹100 line (→ ₹20 margin)
and also type `final_price=999` with no connection between them. Nothing in
this system reconciles that; if that's ever undesirable, decide in
`FRONTEND.md`/`HANDOVER.md` whether `final_price` should become computed
(`rate + margin_amount`) or stay an independent override, and update both
the UI and this doc together.

---

## 6. Idempotent ingest, not append-only

**Where:** `app/db.py` `_upsert_bill` / `ingest_bill`

Re-POSTing the same `(supplier, bill_no)` to `/ingest` — whether because the
same photo was scanned twice, or because the review screen's edits are being
resaved — deletes every existing `line` row under that bill and reinserts
from the current payload. There is no `line`-level diffing and no history:
the previous version of a re-ingested bill's lines is gone, not archived.

**Why:** the "scan the same bill twice by accident" case has to be free —
that's the whole point of keying on `(supplier, bill_no)`. Diffing/archiving
would need a design for what "history" even means when the *same physical
bill* gets edited during review before the first save (is that a
correction, or a new version?) — deliberately punted for now, same as the
"price history" note in `output-format.md` (a re-priced product just shows
up as a new dated block, not a version chain).

**Cost:** there is no audit trail. If a review-screen edit is wrong and
already saved, the only recovery is re-scanning and re-saving correctly —
the previous (wrong) `line` rows are unrecoverable once overwritten.
