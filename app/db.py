"""SQLite persistence: the supplier/company/bill/line book of record.

The Excel workbook (see app/services/excel_export.py) is a generated view
over this data, not the source of truth — re-ingesting the same
(supplier, bill_no) updates its lines in place instead of duplicating them,
so scanning the same bill twice is idempotent.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import settings
from app.models import BillExtraction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS company (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    name TEXT NOT NULL,
    UNIQUE(supplier_id, name)
);
CREATE TABLE IF NOT EXISTS bill (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    bill_no TEXT NOT NULL,
    bill_date TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    UNIQUE(supplier_id, bill_no)
);
CREATE TABLE IF NOT EXISTS line (
    id INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bill(id),
    company_id INTEGER NOT NULL REFERENCES company(id),
    product TEXT NOT NULL,
    pcs INTEGER NOT NULL,
    rate REAL NOT NULL,
    line_order INTEGER NOT NULL,
    final_price REAL,
    margin_pct REAL,
    tax_pct REAL
);
"""

# Columns added after the initial release. CREATE TABLE IF NOT EXISTS won't
# retrofit these onto a `line` table that already exists from before they
# were added, so init_db() adds them by hand, once each, if missing.
_LINE_MIGRATIONS = ["final_price REAL", "margin_pct REAL", "tax_pct REAL"]


@contextmanager
def _connect():
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_line_table(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(line)")}
    for column_def in _LINE_MIGRATIONS:
        column_name = column_def.split()[0]
        if column_name not in existing:
            conn.execute(f"ALTER TABLE line ADD COLUMN {column_def}")


def init_db() -> None:
    """Create the schema if it doesn't exist yet, and migrate `line` to pick
    up any columns added since. Safe to call on every startup."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate_line_table(conn)


def _get_or_create_supplier(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM supplier WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cursor = conn.execute("INSERT INTO supplier (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _get_or_create_company(conn: sqlite3.Connection, supplier_id: int, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM company WHERE supplier_id = ? AND name = ?", (supplier_id, name)
    ).fetchone()
    if row:
        return row[0]
    cursor = conn.execute(
        "INSERT INTO company (supplier_id, name) VALUES (?, ?)", (supplier_id, name)
    )
    return cursor.lastrowid


def _upsert_bill(conn: sqlite3.Connection, supplier_id: int, bill_no: str, bill_date: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT id FROM bill WHERE supplier_id = ? AND bill_no = ?", (supplier_id, bill_no)
    ).fetchone()
    if row:
        bill_id = row[0]
        conn.execute(
            "UPDATE bill SET bill_date = ?, entered_at = ? WHERE id = ?",
            (bill_date, now, bill_id),
        )
        # Re-ingesting the same bill replaces its lines rather than appending to them.
        conn.execute("DELETE FROM line WHERE bill_id = ?", (bill_id,))
        return bill_id

    cursor = conn.execute(
        "INSERT INTO bill (supplier_id, bill_no, bill_date, entered_at) VALUES (?, ?, ?, ?)",
        (supplier_id, bill_no, bill_date, now),
    )
    return cursor.lastrowid


def ingest_bill(supplier_name: str, bill: BillExtraction) -> None:
    """Persist one extracted bill, idempotently keyed by (supplier, bill_no)."""
    with _connect() as conn:
        supplier_id = _get_or_create_supplier(conn, supplier_name)
        bill_id = _upsert_bill(conn, supplier_id, bill.bill_no, bill.bill_date)

        for order, article in enumerate(bill.articles):
            company_id = _get_or_create_company(conn, supplier_id, article.company)
            conn.execute(
                "INSERT INTO line (bill_id, company_id, product, pcs, rate, line_order, "
                "final_price, margin_pct, tax_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bill_id,
                    company_id,
                    article.product,
                    article.pcs,
                    article.rate,
                    order,
                    article.final_price,
                    article.margin_pct,
                    article.tax_pct,
                ),
            )


def get_workbook_data(
    supplier_name: str,
) -> dict[str, list[tuple[str, str, list[tuple[str, int, float, float | None, float | None, float | None]]]]]:
    """Return every ingested bill for a supplier, grouped by company.

    Shape: {company_name: [(bill_no, bill_date, [(product, pcs, rate,
    final_price, margin_pct, tax_pct), ...]), ...]}
    Bills are ordered by date (then insertion order); lines preserve the
    order they appeared in on the original bill.

    Raises KeyError if the supplier has no ingested bills.
    """
    with _connect() as conn:
        supplier_row = conn.execute(
            "SELECT id FROM supplier WHERE name = ?", (supplier_name,)
        ).fetchone()
        if not supplier_row:
            raise KeyError(f"Unknown supplier: {supplier_name}")
        supplier_id = supplier_row[0]

        companies = conn.execute(
            "SELECT id, name FROM company WHERE supplier_id = ? ORDER BY name", (supplier_id,)
        ).fetchall()

        result: dict[
            str, list[tuple[str, str, list[tuple[str, int, float, float | None, float | None, float | None]]]]
        ] = {}
        for company_id, company_name in companies:
            bills = conn.execute(
                "SELECT id, bill_no, bill_date FROM bill "
                "WHERE supplier_id = ? AND id IN (SELECT DISTINCT bill_id FROM line WHERE company_id = ?) "
                "ORDER BY bill_date, id",
                (supplier_id, company_id),
            ).fetchall()

            bill_blocks = []
            for bill_id, bill_no, bill_date in bills:
                lines = conn.execute(
                    "SELECT product, pcs, rate, final_price, margin_pct, tax_pct FROM line "
                    "WHERE bill_id = ? AND company_id = ? ORDER BY line_order",
                    (bill_id, company_id),
                ).fetchall()
                bill_blocks.append((bill_no, bill_date, lines))

            # A re-ingest can leave a company with no current lines (all its
            # articles were edited out); skip it rather than emit an empty sheet.
            if bill_blocks:
                result[company_name] = bill_blocks

        return result
