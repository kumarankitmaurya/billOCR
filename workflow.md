# Bill → inventory: workflow spec + prompts

Grounded in a real bill (Dindayal Jalan Textiles). The key structural fact:
the **DESCRIPTION** column is three fields stacked, and they map to three
levels of storage.

```
DESCRIPTION cell         ->  role         ->  where it lands
────────────────────────     ──────────       ────────────────────────────
(bill header) "Dindayal…"     supplier         the WORKBOOK  (one .xlsx)
"ROHAN FAB SRT"               company/mill     the SHEET/tab inside it
"SAREE"                       type             a column
"MILK CAKE"                   product/design   the ROW  (keyed with rate+date)
```

So one supplier = one workbook; each mill/brand on the bill = one tab; each
design = rows, versioned by price.

---

## The steps (level-0 flow)

```
FRONTEND (scan surface — phone / web)
  1. user picks SUPPLIER from a list        (known up front, never OCR'd)
  2. user snaps / uploads the bill

BACKEND (local first, then cloud)
  3. LOCAL redaction pass  -> blacks out seller PII, keeps bill_no + date on device
  4. redacted image -> extraction LLM using PROMPT B
       returns line items: company, type, product, code, pcs, mtr, rate, amount
  5. routing:
       workbook  = supplier
       tab       = company     (create if new)
       rows      = products, upsert by (product, rate); new price -> new row,
                   tagged with bill date
  6. write workbook, return a diff (+new / seen-again / price-changed)
```

Only the redacted image crosses the network. Supplier, bill_no and date are
supplied to the extraction LLM as context, not read from the image.

---

## PROMPT A — local redaction (vision model on-device)

Use a local vision model (or the tesseract+regex fallback in `redact.py`).
The point is that nothing identifying leaves the machine.

```
You are a privacy filter running locally. Look at this bill image and return
the pixel bounding boxes (x1,y1,x2,y2) of every region containing seller
identity or financial-account data: business name, address, phone, email,
GSTIN, PAN, bank account, IFSC. Also return, as plain text, the invoice number
and invoice date if visible.

Return ONLY:
{ "redact_boxes": [[x1,y1,x2,y2], ...],
  "bill_no": "string|null",
  "bill_date": "YYYY-MM-DD|null" }

Do NOT return the article/product table or any of its contents. Do not describe
the image. Boxes only.
```

The caller blacks out those boxes, keeps `bill_no`/`bill_date` on device, and
sends the blacked-out image on to PROMPT B.

---

## PROMPT B — extraction (cloud vision LLM, on the redacted image)

```
You extract the article table from a textile trade bill into strict JSON.

CONTEXT (trust these, do not read them off the image):
- supplier: {{SUPPLIER}}
- bill_no:  {{BILL_NO}}
- bill_date: {{BILL_DATE}}

The image is redacted: black boxes cover seller name/address/GSTIN/bank. Never
reconstruct anything under a box. If a value isn't clearly legible, use null.

The DESCRIPTION column stacks THREE things per row — split them:
  type     the goods class  (SAREE, SHIRTING, ...)
  company  the mill / brand (ROHAN FAB SRT, JAI MATA DI SRT, SIYARAM ...)
  product  the design name  (MILK CAKE, ANARKALI, GREEN TEA ...)

Per row also read: code (HSN), pcs, mtr, rate, amount, discount. Textiles are
billed by piece (Pcs) OR by metre (Mtr) — capture whichever is non-zero, leave
the other 0/null. Any other column a supplier prints goes into "extra" keyed by
its printed header (e.g. packing "1x18", gst_pct).

Numbers: strip commas/symbols -> plain numbers (12,300.000 -> 12300).
Skip summary rows (Total C/F, CGST, SGST, Grand Total) and amount-in-words.
Output ONLY this JSON, no prose, no fences:

{
  "supplier": "{{SUPPLIER}}",
  "bill_no": "{{BILL_NO}}",
  "bill_date": "{{BILL_DATE}}",
  "columns_seen": ["verbatim headers"],
  "articles": [
    { "type":"", "company":"", "product":"",
      "code":"string|null", "pcs":number|null, "mtr":number|null,
      "rate":number|null, "amount":number|null, "discount":number|null,
      "extra": { } }
  ],
  "notes": "ambiguities, or empty string"
}
```

---

## Storage contract (what `store.py` guarantees)

- workbook path  = one per supplier
- tab            = per company; created on first sight
- row identity   = (product, rate) within a tab
    · same product, same rate  -> refresh last_seen, qty, amount
    · same product, NEW rate    -> append a row (full price history kept)
    · new product               -> append
- every row carries bill_no, bill_date, first_seen, last_seen
- supplier-specific columns (packing, gst_pct…) attach as their own columns

Verified on the real bill: 6 company tabs, JAI MATA DI SRT holds SAGAR/SWIGGY/
GREEN TEA; a later bill repricing GREEN TEA 567 -> 580 produced a second row
while SAGAR (unchanged) stayed one.

---

## Level-0 architecture

```
┌── Frontend (scan) ──────────────┐        ┌── Backend ───────────────────────────┐
│ supplier picker                 │  POST  │ /ingest                              │
│ camera / file upload            │ ─────► │  ├─ redact  (local model)  PROMPT A  │
│ shows the diff after ingest     │ ◄───── │  ├─ extract (cloud vlm)    PROMPT B  │
└─────────────────────────────────┘  diff  │  ├─ route + upsert  (store.py)       │
                                            │  └─ workbooks/ per supplier          │
                                            └──────────────────────────────────────┘
```

Frontend owns only: supplier selection, capture, and rendering the result diff.
Everything privacy-sensitive (redaction) and stateful (routing, versioning,
the workbooks) lives in the backend. The raw photo never leaves the device
un-redacted; the workbook of record is the backend's.
