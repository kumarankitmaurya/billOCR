"""Merge one supplier into another, for when the same supplier was entered twice.

    python scripts/merge_suppliers.py --from "Dindayal Jalan Textiles Pvt.Ltd" \
                                      --into "Dindayal Jalan"          # dry run
    python scripts/merge_suppliers.py --from ... --into ... --apply

Supplier is the workbook key and the UI collects it as free text, so one
supplier typed two ways becomes two ledgers, and a price search cannot connect
a design bought under one spelling to the same design under the other.

Three collisions have to be handled, which is why this is a script and not an
UPDATE:

  * company is UNIQUE (supplier_id, name) — a company present under both
    names cannot simply be repointed, its lines move to the surviving row.
  * bill is UNIQUE (supplier_id, bill_no) — the same bill scanned under both
    names cannot be repointed either.
  * tax_pct / margin_pct / final_price are typed by hand and never appear on
    the bill, so OCR cannot reproduce them. Whichever copy of a duplicated
    bill holds that work must keep it — the same rule db.ingest_bill applies
    on re-ingest.

Dry run by default. Nothing is written without --apply.
"""

import argparse
import sys
from pathlib import Path

# Run as `python scripts/merge_suppliers.py`, so Python puts scripts/ on the
# path rather than the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

PRICING = ("final_price", "margin_pct", "tax_pct")


def supplier_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM supplier WHERE name = %s", (name,)).fetchone()
    if not row:
        sys.exit(f"No such supplier: {name!r}")
    return row[0]


def plan(conn, src: int, dst: int) -> dict:
    """What the merge would do, without doing any of it."""
    src_bills = dict(
        conn.execute("SELECT bill_no, id FROM bill WHERE supplier_id = %s", (src,)).fetchall()
    )
    dst_bills = dict(
        conn.execute("SELECT bill_no, id FROM bill WHERE supplier_id = %s", (dst,)).fetchall()
    )
    src_companies = dict(
        conn.execute("SELECT name, id FROM company WHERE supplier_id = %s", (src,)).fetchall()
    )
    dst_companies = dict(
        conn.execute("SELECT name, id FROM company WHERE supplier_id = %s", (dst,)).fetchall()
    )
    return {
        "bills_to_move": {n: i for n, i in src_bills.items() if n not in dst_bills},
        "bills_duplicated": {n: (i, dst_bills[n]) for n, i in src_bills.items() if n in dst_bills},
        "companies_to_move": {n: i for n, i in src_companies.items() if n not in dst_companies},
        "companies_merged": {
            n: (i, dst_companies[n]) for n, i in src_companies.items() if n in dst_companies
        },
    }


def rescue_pricing(conn, src_bill: int, dst_bill: int) -> list[str]:
    """Copy hand-entered pricing from a duplicate bill onto the surviving one.

    Field by field and only where the survivor has none, so the copy that was
    actually worked on wins without overwriting anything already there.
    """
    rescued = []
    src_lines = conn.execute(
        "SELECT co.name, l.product, l.final_price, l.margin_pct, l.tax_pct "
        "FROM line l JOIN company co ON l.company_id = co.id WHERE l.bill_id = %s",
        (src_bill,),
    ).fetchall()
    for company, product, *values in src_lines:
        if all(v is None for v in values):
            continue
        for field, value in zip(PRICING, values):
            if value is None:
                continue
            updated = conn.execute(
                f"UPDATE line l SET {field} = %s "
                "FROM company co WHERE l.company_id = co.id AND l.bill_id = %s "
                f"AND co.name = %s AND l.product = %s AND l.{field} IS NULL",
                (value, dst_bill, company, product),
            ).rowcount
            if updated:
                rescued.append(f"{company}/{product}.{field}={value}")
    return rescued


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", required=True, help="supplier to merge away")
    parser.add_argument("--into", dest="target", required=True, help="supplier to keep")
    parser.add_argument("--apply", action="store_true", help="actually write the changes")
    args = parser.parse_args()

    if args.source == args.target:
        sys.exit("--from and --into are the same supplier")

    db.open_pool()
    try:
        with db._connect() as conn:
            src, dst = supplier_id(conn, args.source), supplier_id(conn, args.target)
            steps = plan(conn, src, dst)

            print(f"merging {args.source!r} (id {src}) into {args.target!r} (id {dst})")
            for label, items in steps.items():
                print(f"  {label.replace('_', ' ')}: {len(items)}")
                for name in items:
                    print(f"      {name}")

            if not args.apply:
                print("\nDry run — nothing written. Re-run with --apply.")
                return 0

            # Duplicated bills: keep the target's, rescue its pricing, drop the source's.
            for bill_no, (src_bill, dst_bill) in steps["bills_duplicated"].items():
                rescued = rescue_pricing(conn, src_bill, dst_bill)
                for item in rescued:
                    print(f"  rescued pricing on {bill_no}: {item}")
                conn.execute("DELETE FROM line WHERE bill_id = %s", (src_bill,))
                conn.execute("DELETE FROM bill WHERE id = %s", (src_bill,))
                print(f"  dropped duplicate bill {bill_no}")

            # Companies present under both names: move the lines, drop the spare row.
            for name, (src_co, dst_co) in steps["companies_merged"].items():
                conn.execute(
                    "UPDATE line SET company_id = %s WHERE company_id = %s", (dst_co, src_co)
                )
                conn.execute("DELETE FROM company WHERE id = %s", (src_co,))
                print(f"  merged company {name}")

            # Everything left is unique to the source and can simply be repointed.
            conn.execute("UPDATE company SET supplier_id = %s WHERE supplier_id = %s", (dst, src))
            conn.execute("UPDATE bill SET supplier_id = %s WHERE supplier_id = %s", (dst, src))
            conn.execute("DELETE FROM supplier WHERE id = %s", (src,))
            print(f"  removed supplier {args.source!r}")
            print("Done.")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
