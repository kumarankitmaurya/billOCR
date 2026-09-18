"""scripts/migrate_sqlite_to_neon.py, run end to end against a real Postgres.

Builds a small SQLite book in the old schema, runs the script as a
subprocess the way it's run for real, then checks the rows arrived with
their ids intact and that the app can keep writing afterwards — which only
works if the script moved each sequence past the copied ids.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

from .conftest import TEST_DATABASE_URL, make_result

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_sqlite_to_neon.py"

# The SQLite schema as it stood before the move, including the columns
# that were added to `line` by ALTER TABLE over time.
SQLITE_SCHEMA = """
CREATE TABLE supplier (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE company (
    id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    name TEXT NOT NULL, UNIQUE(supplier_id, name));
CREATE TABLE bill (
    id INTEGER PRIMARY KEY, supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    bill_no TEXT NOT NULL, bill_date TEXT NOT NULL, entered_at TEXT NOT NULL,
    UNIQUE(supplier_id, bill_no));
CREATE TABLE line (
    id INTEGER PRIMARY KEY, bill_id INTEGER NOT NULL REFERENCES bill(id),
    company_id INTEGER NOT NULL REFERENCES company(id), product TEXT NOT NULL,
    pcs INTEGER NOT NULL, rate REAL NOT NULL, line_order INTEGER NOT NULL,
    final_price REAL, margin_pct REAL, tax_pct REAL, photo_path TEXT, photo_caption TEXT);
"""


def _build_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SQLITE_SCHEMA)
    # Non-contiguous ids on purpose, so "ids preserved" is actually tested.
    conn.execute("INSERT INTO supplier VALUES (3, 'Dindayal Jalan')")
    conn.execute("INSERT INTO company VALUES (7, 3, 'JAI MATA DI SRT')")
    conn.execute("INSERT INTO bill VALUES (5, 3, 'DJ-OLD', '2025-07-01', '2025-07-01T10:00:00+00:00')")
    conn.executemany(
        "INSERT INTO line VALUES (?, 5, 7, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)",
        [(11, "GREEN TEA", 4, 567.0, 0, 670.0), (12, "MILK CAKE", 2, 410.0, 1, None)],
    )
    conn.commit()
    conn.close()


def _run(sqlite_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite", str(sqlite_path),
         "--database-url", TEST_DATABASE_URL, *extra],
        capture_output=True,
        text=True,
    )


def test_migration_copies_rows_with_ids_and_resets_sequences(client, tmp_path):
    sqlite_path = tmp_path / "bills.db"
    _build_sqlite(sqlite_path)

    result = _run(sqlite_path)
    assert result.returncode == 0, result.stderr

    from app import db

    with db._connect() as conn:
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("supplier", "company", "bill", "line")}
        assert counts == {"supplier": 1, "company": 1, "bill": 1, "line": 2}
        assert conn.execute("SELECT id FROM supplier").fetchone()[0] == 3
        assert sorted(r[0] for r in conn.execute("SELECT id FROM line")) == [11, 12]

    # The migrated rows are visible through the API...
    rows = client.get("/api/bills/search", params={"name": "milk"}).json()
    assert [r["bill_no"] for r in rows] == ["DJ-OLD"]

    # ...and new writes don't collide with the copied ids. Without setval
    # this would fail on the primary key of supplier/company/bill/line.
    new = make_result(supplier="New Supplier", bill_no="NS-1")
    response = client.post("/api/bills/ingest", json={"results": [new]})
    assert response.status_code == 200, response.text

    workbook = client.get("/api/bills/workbook", params={"supplier": "Dindayal Jalan"})
    assert workbook.status_code == 200


def test_migration_refuses_a_non_empty_target(client, tmp_path):
    client.post("/api/bills/ingest", json={"results": [make_result()]})

    sqlite_path = tmp_path / "bills.db"
    _build_sqlite(sqlite_path)

    result = _run(sqlite_path)
    assert result.returncode == 1
    assert "not empty" in result.stderr


def test_migration_refuses_the_pooled_endpoint(tmp_path):
    sqlite_path = tmp_path / "bills.db"
    _build_sqlite(sqlite_path)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite", str(sqlite_path),
         "--database-url", "postgresql://u:p@ep-x-pooler.ap-southeast-1.aws.neon.tech/db"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "pooled" in result.stderr
