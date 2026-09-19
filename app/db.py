"""Postgres persistence: the supplier/company/bill/line book of record.

The Excel workbook (see app/services/excel_export.py) is a generated view
over this data, not the source of truth — re-ingesting the same
(supplier, bill_no) updates its lines in place instead of duplicating them,
so scanning the same bill twice is idempotent.

Runs against Neon's pooled endpoint, which is PgBouncer in transaction mode.
Two consequences are load-bearing below:
  * `prepare_threshold=None` — psycopg would otherwise start using
    server-side prepared statements, which transaction pooling breaks.
  * Session-scoped SQL (SET without LOCAL, LISTEN, session temp tables)
    can't be relied on, since a connection isn't pinned to one session.
"""

import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg_pool import ConnectionPool

from app.config import settings
from app.models import BillExtraction

logger = logging.getLogger(__name__)

# Schema statements, applied one at a time — psycopg has no executescript().
# All are idempotent, so this is safe to run on every startup.
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS supplier (
        id bigserial PRIMARY KEY,
        name text UNIQUE NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS company (
        id bigserial PRIMARY KEY,
        supplier_id bigint NOT NULL REFERENCES supplier(id),
        name text NOT NULL,
        UNIQUE (supplier_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bill (
        id bigserial PRIMARY KEY,
        supplier_id bigint NOT NULL REFERENCES supplier(id),
        bill_no text NOT NULL,
        bill_date text NOT NULL,
        entered_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (supplier_id, bill_no)
    )
    """,
    # rate/final_price are `double precision`, not `numeric`: numeric comes
    # back as Decimal, which isn't JSON-serialisable and which openpyxl
    # writes as text rather than as a number.
    """
    CREATE TABLE IF NOT EXISTS line (
        id bigserial PRIMARY KEY,
        bill_id bigint NOT NULL REFERENCES bill(id),
        company_id bigint NOT NULL REFERENCES company(id),
        product text NOT NULL,
        pcs integer NOT NULL,
        rate double precision NOT NULL,
        line_order integer NOT NULL,
        final_price double precision,
        margin_pct double precision,
        tax_pct double precision
    )
    """,
    # Postgres doesn't index foreign keys automatically, and both of these
    # are joined on for every workbook build and every search.
    "CREATE INDEX IF NOT EXISTS line_bill_id_idx ON line (bill_id)",
    "CREATE INDEX IF NOT EXISTS line_company_id_idx ON line (company_id)",
]

# Columns added after the initial release. `ADD COLUMN IF NOT EXISTS` makes
# each a no-op once applied, so this runs on every startup.
_LINE_MIGRATIONS = [
    "final_price double precision",
    "margin_pct double precision",
    "tax_pct double precision",
    # From an unfinished product-photo feature on SQLite. Kept so the
    # migration loses nothing. Product photos now live in the separate
    # billOCR-visual service, which stores them in object storage.
    "photo_path text",
    "photo_caption text",
]

# Arbitrary but fixed: serialises schema creation so two instances starting
# together can't race on CREATE TABLE/INDEX.
_SCHEMA_LOCK_KEY = 727001

_pool: ConnectionPool | None = None


def open_pool() -> None:
    """Open the shared connection pool. Called once, from the app lifespan."""
    global _pool
    if _pool is not None:
        return
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set — the app has no database to talk to")

    started = time.monotonic()
    _pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=settings.db_pool_max_size,
        max_idle=120,
        open=False,
        check=ConnectionPool.check_connection,
        kwargs={"prepare_threshold": None},
    )
    _pool.open(wait=True, timeout=30)
    logger.info("db pool opened in %.2fs", time.monotonic() - started)


def close_pool() -> None:
    """Close the shared pool on shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def _connect():
    """Borrow a pooled connection.

    Commits on success and rolls back on error, which is what the previous
    sqlite3 version did.
    """
    if _pool is None:
        raise RuntimeError("Connection pool is not open — call open_pool() first")
    with _pool.connection() as conn:
        yield conn


def init_db() -> None:
    """Create the schema if it doesn't exist and apply column migrations.

    Safe to call on every startup, and uses its own short-lived connection
    so the schema exists before the pool is opened.
    """
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set — the app has no database to talk to")

    started = time.monotonic()
    with psycopg.connect(settings.database_url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
            for statement in _SCHEMA:
                conn.execute(statement)
            for column_def in _LINE_MIGRATIONS:
                conn.execute(f"ALTER TABLE line ADD COLUMN IF NOT EXISTS {column_def}")
    logger.info("init_db finished in %.2fs", time.monotonic() - started)


def _get_or_create_supplier(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM supplier WHERE name = %s", (name,)).fetchone()
    if row:
        return row[0]
    # Unlike SQLite, Postgres allows genuinely concurrent writers, so another
    # caller may insert between the SELECT above and this INSERT.
    row = conn.execute(
        "INSERT INTO supplier (name) VALUES (%s) ON CONFLICT (name) DO NOTHING RETURNING id",
        (name,),
    ).fetchone()
    if row:
        return row[0]
    return conn.execute("SELECT id FROM supplier WHERE name = %s", (name,)).fetchone()[0]


def _get_or_create_company(conn: psycopg.Connection, supplier_id: int, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM company WHERE supplier_id = %s AND name = %s", (supplier_id, name)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "INSERT INTO company (supplier_id, name) VALUES (%s, %s) "
        "ON CONFLICT (supplier_id, name) DO NOTHING RETURNING id",
        (supplier_id, name),
    ).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "SELECT id FROM company WHERE supplier_id = %s AND name = %s", (supplier_id, name)
    ).fetchone()[0]


def _upsert_bill(conn: psycopg.Connection, supplier_id: int, bill_no: str, bill_date: str) -> int:
    now = datetime.now(timezone.utc)
    bill_id = conn.execute(
        "INSERT INTO bill (supplier_id, bill_no, bill_date, entered_at) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (supplier_id, bill_no) "
        "DO UPDATE SET bill_date = EXCLUDED.bill_date, entered_at = EXCLUDED.entered_at "
        "RETURNING id",
        (supplier_id, bill_no, bill_date, now),
    ).fetchone()[0]

    # Re-ingesting the same bill replaces its lines rather than appending to them.
    conn.execute("DELETE FROM line WHERE bill_id = %s", (bill_id,))
    return bill_id


def ingest_bill(supplier_name: str, bill: BillExtraction) -> None:
    """Persist one extracted bill, idempotently keyed by (supplier, bill_no)."""
    with _connect() as conn:
        supplier_id = _get_or_create_supplier(conn, supplier_name)
        bill_id = _upsert_bill(conn, supplier_id, bill.bill_no, bill.bill_date)

        for order, article in enumerate(bill.articles):
            company_id = _get_or_create_company(conn, supplier_id, article.company)
            conn.execute(
                "INSERT INTO line (bill_id, company_id, product, pcs, rate, line_order, "
                "final_price, margin_pct, tax_pct) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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

    One ordered query, grouped in Python, rather than a query per company
    and per bill — over a network that difference is most of the latency.

    Raises KeyError if the supplier has no ingested bills.
    """
    with _connect() as conn:
        supplier_row = conn.execute(
            "SELECT id FROM supplier WHERE name = %s", (supplier_name,)
        ).fetchone()
        if not supplier_row:
            raise KeyError(f"Unknown supplier: {supplier_name}")
        supplier_id = supplier_row[0]

        # COLLATE "C" sorts by byte value, which is what SQLite did, so
        # sheet order doesn't shift for anyone who already has a book.
        rows = conn.execute(
            """
            SELECT company.name, bill.bill_no, bill.bill_date, bill.id,
                   line.product, line.pcs, line.rate,
                   line.final_price, line.margin_pct, line.tax_pct
            FROM line
            JOIN bill ON line.bill_id = bill.id
            JOIN company ON line.company_id = company.id
            WHERE bill.supplier_id = %s
            ORDER BY company.name COLLATE "C", bill.bill_date, bill.id, line.line_order
            """,
            (supplier_id,),
        ).fetchall()

    result: dict[
        str, list[tuple[str, str, list[tuple[str, int, float, float | None, float | None, float | None]]]]
    ] = {}
    current_bill_id: dict[str, int] = {}
    for company_name, bill_no, bill_date, bill_id, *line_values in rows:
        blocks = result.setdefault(company_name, [])
        if not blocks or current_bill_id.get(company_name) != bill_id:
            blocks.append((bill_no, bill_date, []))
            current_bill_id[company_name] = bill_id
        blocks[-1][2].append(tuple(line_values))

    return result


def search_lines(
    name: str | None = None,
    min_final_price: float | None = None,
    max_final_price: float | None = None,
    min_base_price: float | None = None,
    max_base_price: float | None = None,
) -> list[dict]:
    """Search every ingested line across every supplier, by product name
    substring and/or price range.

    Always includes `rate` (the confidential base price) in each row — this
    layer doesn't know about auth; stripping `rate` for non-admin callers is
    the router's job (see routers/bills.py).

    Returns rows most-recently-billed first: [{supplier, company, product,
    bill_no, bill_date, pcs, rate, final_price}, ...]
    """
    clauses = []
    params: list[str | float] = []

    if name:
        # ILIKE, not LIKE: SQLite's LIKE ignored case, and callers rely on that.
        clauses.append("line.product ILIKE %s")
        params.append(f"%{name}%")
    if min_final_price is not None:
        clauses.append("line.final_price >= %s")
        params.append(min_final_price)
    if max_final_price is not None:
        clauses.append("line.final_price <= %s")
        params.append(max_final_price)
    if min_base_price is not None:
        clauses.append("line.rate >= %s")
        params.append(min_base_price)
    if max_base_price is not None:
        clauses.append("line.rate <= %s")
        params.append(max_base_price)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT supplier.name, company.name, line.product, bill.bill_no,
                   bill.bill_date, line.pcs, line.rate, line.final_price
            FROM line
            JOIN bill ON line.bill_id = bill.id
            JOIN company ON line.company_id = company.id
            JOIN supplier ON bill.supplier_id = supplier.id
            {where}
            ORDER BY bill.bill_date DESC, bill.id DESC, line.line_order
            """,
            params,
        ).fetchall()

    return [
        {
            "supplier": supplier,
            "company": company,
            "product": product,
            "bill_no": bill_no,
            "bill_date": bill_date,
            "pcs": pcs,
            "rate": rate,
            "final_price": final_price,
        }
        for supplier, company, product, bill_no, bill_date, pcs, rate, final_price in rows
    ]
