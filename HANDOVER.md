# Bill → inventory : engineering handover

Digitize textile supplier bills by phone photo into the shop's existing book,
PII scrubbed before anything hits a cloud model. This doc is self-contained:
domain, decisions, architecture, and a build prompt for a coding agent.

---

## 1. Domain model

The one non-obvious thing: the bill's `DESCRIPTION` column is **three fields
stacked**, and they map to three storage levels.

```
supplier   who issued the bill        e.g. "Dindayal Jalan Textiles"   (DJ-STI-… bill nos)
  company    mill / brand on a line    e.g. "ROHAN FAB SRT", "sudesh"
    product    the design name         e.g. "MILK CAKE", "GREEN TEA"
bill       one invoice                 has bill_no + date, many lines
line       one product on a bill       type, code(HSN), pcs, mtr, rate, amount, discount
```

Selling price (SP) is **not** on the bill — it's set by the shop, per design,
by hand.

---

## 2. Ground truth (sample artifacts in this bundle)

- `IMG_20250817_155721.jpg` — a real bill (Dindayal Jalan). Phone photo, shadow,
  skew, partly-obscured HSN codes. This is the realistic input quality.
- `book1-p.xlsx` — the shop's actual book. Two layouts of the same data:
  per-company sheets (`sudesh`, `satvachan`) and per-bill sheets (`partty-…`).
- `Dindayal_Jalan_book-format.xlsx` — generated output matching the per-company
  layout. This is the target shape.

Per-company sheet layout (what to write):
```
DJ-STI-36600   2025-08-17                    <- bill block header (bill_no, date)
JAI MATA DI SRT   pc   price   .   .   SP     <- label row, once per sheet
SAGAR             4    595                    <- product | pc | price
SWIGGY            4    595
GREEN TEA         4    567
(blank separator)
DJ-STI-36742   2025-10-02                     <- next bill appends below
GREEN TEA         4    580
```

Derived columns (skipped for now, positions reserved): D = price×1.05,
E = D×1.15 (=price×1.2075), F = SP (hand-adjusted, e.g. 479→565 not 578).

---

## 3. Decisions locked

- **Supplier is chosen in the UI, never OCR'd.** This is what lets the whole PII
  header be scrubbed — the model is told who the supplier is.
- **Redact locally, extract in the cloud.** Raw photo never leaves the device
  un-redacted. Only a blacked-out image crosses the network.
- **Fields captured for now:** company, product, pcs, rate. Skipped: amount,
  discount, gst, hsn/code, mtr, and all SP/price-calc columns.
- **Storage shape:** workbook = supplier, sheet = company, each bill a dated
  block. Price history is implicit in the dated blocks (same design reappears at
  a new price in a later block) — no versioning column needed in this layout.
- **SP stays manual.** Never auto-fill it; the scan must not clobber it.

## 4. Open decisions (flag to product owner, don't guess)

- **Pcs vs Mtr.** Shirting is billed by metre, saree by piece. A per-metre rate
  and a per-piece rate aren't comparable — decide whether to store `unit` and
  keep them apart in any price comparison.
- **Product-name collisions.** Design names (SWIGGY, GREEN TEA) repeat across
  mills/seasons. They're separated today because they live in different company
  sheets — confirm that's intended vs. a global product catalog.
- **Two book views.** Per-company and per-bill are the same records grouped two
  ways. Generate both from one dataset rather than maintaining by hand.
- **Reconciliation.** HSN/amount cells are the noisiest under shadow. On ingest,
  check `amount ≈ pcs·rate` (or `mtr·rate`) and flag rows that don't reconcile
  instead of trusting extracted numbers.

---

## 5. Architecture

Recommended: a normalized store as system-of-record, with the shop's Excel
generated as a *view*. This resolves the two-views problem and price history
cleanly, and keeps the Excel the shop already trusts. Excel-only is acceptable
for an MVP (prototypes already do that), but you'll re-solve both problems by
hand later.

```
┌── Frontend (phone / web) ─────────┐        ┌── Backend ──────────────────────────────┐
│ • supplier picker                 │  POST  │  API  (Spring Boot / Kotlin)            │
│ • capture / upload                │ ─────► │   └─ enqueue ingest job                 │
│ • show ingest diff + flags        │ ◄───── │                                         │
└───────────────────────────────────┘  diff  │  Ingest worker  (Python — CV/LLM land)  │
                                              │   1. redact   (local model)  PROMPT A   │
                                              │   2. extract  (cloud VLM)    PROMPT B   │
                                              │   3. reconcile (amount≈pcs·rate)        │
                                              │   4. route + persist                    │
                                              │        DB of record ──► Excel export    │
                                              └─────────────────────────────────────────┘
```

**Why the split.** The CV/redaction/vision-LLM stack is Python-native
(tesseract, opencv, vendor SDKs). The API + orchestration is comfortable in
your Kotlin/Spring world. Keep them as two services with a queue between —
matches an event pipeline (scan → redacted → extracted → persisted) you can
retry per stage. All-Python is fine too if you'd rather not run two runtimes.

### Data flow (per bill)
```
photo ─► redact ─► {redacted.png, bill_no, date kept local} ─► extract JSON
      ─► reconcile ─► upsert(supplier→company→lines) ─► render Excel view ─► diff to UI
```

### Suggested schema (DB-of-record option)
```
supplier(id, name)
company(id, supplier_id, name)                     -- mill/brand within a supplier
bill(id, supplier_id, bill_no, bill_date, image_ref, entered_at)
line(id, bill_id, company_id, type, product, code,
     pcs, mtr, rate, amount, discount)             -- price history = lines over time
pricing(company_id, product, sp, updated_at)       -- SP: manual, per design, separate
```
Excel export = query grouped by company (per-company book) or by bill
(per-bill book). Price history = `line` rows ordered by bill_date.

### Privacy boundary
Redaction runs before the network hop. bill_no + date are captured on-device and
passed to the extractor as context, so no PII needs to be read off the image.
Retain the raw photo only on-device (or encrypted, short-TTL) if at all.

---

## 6. The two prompts

Full text in `workflow.md`. Summary:
- **PROMPT A (local redaction):** returns pixel boxes for seller name / address /
  phone / email / GSTIN / PAN / bank+IFSC, plus bill_no + date as text. Caller
  blacks the boxes.
- **PROMPT B (cloud extraction):** on the redacted image, splits the 3-part
  DESCRIPTION into type/company/product and returns `{company, product, pcs,
  rate}` per line (other fields deferred — see §3). Strict JSON, no prose.

Current output contract (`output-format.md`):
```json
{ "supplier":"…", "bill_no":"…", "bill_date":"YYYY-MM-DD",
  "articles":[ {"company":"…","product":"…","pcs":0,"rate":0} ] }
```

---

## 7. Prototype inventory (what already exists, and its status)

| file | does | status |
|------|------|--------|
| `redact.py` | local PII scrub (tesseract+regex), keeps bill_no/date on device | works; regex path. LLM-box path = PROMPT A |
| `pic2xlsx.py` | naive local OCR→xlsx (img2table) | works; superseded by VLM extraction for dynamic formats |
| `store_book.py` | writes the per-company block-ledger, calc/SP blank | works; matches book1-p layout |
| `store.py` | earlier clean-table store, (product,rate) versioning | works; alt layout, keep for reference |
| `watch.py` | folder watcher (click-pic → open) | works; optional desktop path |
| `workflow.md` | level-0 flow + both prompts | reference |
| `output-format.md` | current field contract + book mapping | reference |

Treat the Python files as **executable spec**, not production code — they prove
each stage and pin the exact behaviour to reimplement.

---

## 8. Build plan (milestones)

- **M0 – contract.** Freeze the PROMPT B JSON (§6) and the DB schema (§5). Wire
  a stub extractor returning fixed JSON so storage can be built independently.
- **M1 – storage + Excel export.** Persist supplier/company/bill/line; regenerate
  the per-company book identical to `Dindayal_Jalan_book-format.xlsx`. Idempotent
  re-ingest of the same bill.
- **M2 – extraction.** Real VLM call on a redacted image → JSON → reconcile
  (`amount≈pcs·rate`) → persist. Flag non-reconciling rows.
- **M3 – redaction.** PROMPT A (or the regex fallback) in the ingest worker;
  assert no PII patterns survive on the image sent to the cloud.
- **M4 – frontend.** Supplier picker, capture, ingest-diff view with flags.

Acceptance for M1+M2: feed `IMG_20250817_155721.jpg` (supplier = Dindayal
Jalan), get 6 company sheets, JAI MATA DI SRT = SAGAR/SWIGGY/GREEN TEA, and a
second dated bill appends a block with GREEN TEA at its new price.

---

## 9. The ask (paste to a coding agent)

```
Build the bill-ingestion system described in HANDOVER.md.

Scope this iteration: M0–M2 (contract, storage+Excel export, extraction with
reconciliation). Redaction (M3) and frontend (M4) are stubbed.

Constraints:
- Fields captured now: company, product, pcs, rate. Nothing else. Do NOT write
  the SP or price-calc columns — leave columns D/E/F blank but present.
- Storage: <chosen — DB-of-record with Excel export, or Excel-only MVP>.
- Excel output must byte-for-layout match the per-company block ledger in
  book1-p.xlsx (sheet=company, [bill_no,date] block header, one label row,
  product|pc|price rows, blank separator between bills).
- Extraction returns the strict JSON in output-format.md. Supplier, bill_no and
  date are inputs (from the UI + local capture), not read off the image.
- On ingest, verify amount≈pcs·rate and surface a flag list; never silently
  accept mismatches.
- Re-ingesting the same bill must be idempotent.

Deliver: services per the architecture, tests that ingest the sample bill and
reproduce Dindayal_Jalan_book-format.xlsx, and a short run/README.

Treat the Python files in this bundle as executable spec for each stage's
behaviour. Ask before guessing on any §4 open decision.
```
