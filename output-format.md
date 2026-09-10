# Output format (aligned to your book, tax/calc skipped)

Matches the `sudesh` / `satvachan` sheet layout in book1-p.xlsx.

## What the extraction LLM returns (only these fields for now)

```json
{
  "supplier": "Dindayal Jalan",
  "bill_no": "DJ-STI-36600",
  "bill_date": "2025-08-17",
  "articles": [
    { "company": "JAI MATA DI SRT", "product": "SAGAR", "pcs": 4, "rate": 595,
      "final_price": null, "margin_pct": null },
    { "company": "ROHAN FAB SRT",   "product": "MILK CAKE", "pcs": 20, "rate": 615,
      "final_price": null, "margin_pct": null }
  ]
}
```

Dropped for now (add back when you want them): `amount`, `discount`, `gst`,
`hsn/code`, `mtr`. Only company, product, pcs (-> pc), rate (-> price) are
read off the bill.

`final_price` and `margin_pct` are **always null straight out of OCR** —
neither is printed on the bill. They're filled in by hand on the review
screen (billOCR-ui) before saving, same as the bill_no/bill_date correction
flow. See "The columns" below for what they compute into.

## How it lands in the workbook

- workbook = supplier  (one file per supplier)
- sheet    = company
- each bill = a stacked block inside the company sheet:

```
DJ-STI-36600   2025-08-17
JAI MATA DI SRT   pc    price   .    margin   SP  <- label row, once per sheet
SAGAR             4     595     .    119      714
SWIGGY            4     595
GREEN TEA         4     567
(blank line)
DJ-STI-36742   2025-10-02                        <- next bill appends below
DIWALI SPL        6     720
GREEN TEA         4     580
```

## The columns (D reserved, E/F filled from the review screen)

| col | meaning                       | where it comes from                          |
|-----|-------------------------------|-----------------------------------------------|
| D   | loading / freight             | still reserved, left blank — not captured yet |
| E   | margin **amount**              | `rate * margin_pct / 100`, computed at export  |
| F   | SP (selling price)            | `final_price`, exactly as entered              |

`margin_pct` and `final_price` are two separate fields on the review screen,
per line — nothing is inferred from the other. If a line's `margin_pct` is
null, column E is left blank for that row; same for `final_price` and F.
Nothing stops hand-editing the exported `.xlsx` afterward, same as before —
these columns just now start pre-filled when the shop enters them during
review instead of always starting blank.

## Price history

Because each bill is a dated block, the same design at a new price simply shows
up again in the next block (GREEN TEA 567 then 580). No separate versioning
column needed in this layout - the date on each block is the record.
