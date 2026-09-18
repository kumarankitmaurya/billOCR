"""One-off copy of the old SQLite book of record into Postgres.

Run once, when moving off the local `bills.db`:

    python scripts/migrate_sqlite_to_neon.py \\
        --sqlite bills.db \\
        --database-url "postgresql://...ap-southeast-1.aws.neon.tech/neondb?sslmode=require"

Use the DIRECT Neon connection string (the hostname *without* `-pooler`).
This does DDL-adjacent work — `setval` on sequences — in one long
transaction, which is exactly what transaction-mode pooling is bad at.

Row ids are preserved, so foreign keys still line up, and each sequence is
then advanced past the highest id so later inserts don't collide.
"""

import argparse
import sqlite3
import sys

import psycopg

TABLES = ["supplier", "company", "bill", "line"]

COPY_SQL = {
    "supplier": (
        "SELECT id, name FROM supplier ORDER BY id",
        "INSERT INTO supplier (id, name) VALUES (%s, %s)",
    ),
    "company": (
        "SELECT id, supplier_id, name FROM company ORDER BY id",
        "INSERT INTO company (id, supplier_id, name) VALUES (%s, %s, %s)",
    ),
    "bill": (
        "SELECT id, supplier_id, bill_no, bill_date, entered_at FROM bill ORDER BY id",
        "INSERT INTO bill (id, supplier_id, bill_no, bill_date, entered_at) "
        "VALUES (%s, %s, %s, %s, %s)",
    ),
    "line": (
        "SELECT id, bill_id, company_id, product, pcs, rate, line_order, "
        "final_price, margin_pct, tax_pct, photo_path, photo_caption FROM line ORDER BY id",
        "INSERT INTO line (id, bill_id, company_id, product, pcs, rate, line_order, "
        "final_price, margin_pct, tax_pct, photo_path, photo_caption) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
    ),
}


def sqlite_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def pg_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="bills.db", help="path to the old SQLite file")
    parser.add_argument("--database-url", required=True, help="DIRECT Neon connection string")
    parser.add_argument(
        "--force",
        action="store_true",
        help="copy even if the target already has rows (may violate unique constraints)",
    )
    args = parser.parse_args()

    if "-pooler." in args.database_url:
        print(
            "Refusing to run against the pooled endpoint — use the direct connection "
            "string (the same URL without '-pooler').",
            file=sys.stderr,
        )
        return 2

    src = sqlite3.connect(args.sqlite)
    source_counts = sqlite_counts(src)
    print("source (sqlite):", source_counts)
    if sum(source_counts.values()) == 0:
        print("Nothing to copy.")
        return 0

    with psycopg.connect(args.database_url, prepare_threshold=None) as dst:
        existing = pg_counts(dst)
        if any(existing.values()) and not args.force:
            print(f"Target is not empty ({existing}); pass --force to copy anyway.", file=sys.stderr)
            return 1

        # One transaction: either the whole book arrives or none of it does.
        with dst.transaction():
            for table in TABLES:
                select_sql, insert_sql = COPY_SQL[table]
                rows = src.execute(select_sql).fetchall()
                with dst.cursor() as cur:
                    cur.executemany(insert_sql, rows)
                print(f"copied {len(rows):>5} rows into {table}")

            # Ids were copied verbatim, so each sequence still points at 1
            # and the next insert would collide. Move it past the max id.
            for table in TABLES:
                dst.execute(
                    "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                    "COALESCE((SELECT max(id) FROM " + table + "), 1))",
                    (table,),
                )

        print("target (postgres):", pg_counts(dst))

    src.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
