"""Shared fixtures for the integration tests.

These go through the HTTP API into a real Postgres — no mocking of the
database layer, since the point of the suite is to catch the
SQLite-to-Postgres port breaking something.

Start a database first (the pgvector image matches what the sibling
billOCR-visual service needs from the same database):

    docker run -d --name billocr-pg -p 5433:5432 \\
        -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17

    export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/postgres
"""

import os
import sys
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Environment has to be set before app.config is imported, since Settings
# reads os.getenv at class-definition time.
if TEST_DATABASE_URL:
    if "neon.tech" in TEST_DATABASE_URL and os.environ.get("ALLOW_REMOTE_TEST_DB") != "1":
        raise RuntimeError(
            "TEST_DATABASE_URL points at Neon. These tests TRUNCATE every table. "
            "Set ALLOW_REMOTE_TEST_DB=1 only if you are certain it's a scratch branch."
        )
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ.setdefault("APP_PASSWORD", "test-app-pw")
    os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pw")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

APP_PASSWORD = os.environ.get("APP_PASSWORD", "test-app-pw")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "test-admin-pw")


@pytest.fixture(scope="session")
def client():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set — see this file's docstring")

    from fastapi.testclient import TestClient

    from app.main import app

    # Every endpoint requires X-App-Password, so it goes on the client as a
    # default header rather than onto 30-odd individual calls. httpx merges
    # per-request headers with these, so a test passing X-Admin-Password still
    # carries the app password too.
    #
    # The `with` is what runs the lifespan, which is what creates the schema
    # and opens the pool.
    with TestClient(app, headers={"X-App-Password": APP_PASSWORD}) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def anonymous_client(client):
    """A client that sends no credentials, for asserting the gate is shut.

    Depends on `client` so the app's lifespan has already run.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_db(client):
    """Empty every table before each test so they can't leak into each other."""
    from app import db

    with db._connect() as conn:
        conn.execute(
            "TRUNCATE supplier, company, bill, line RESTART IDENTITY CASCADE"
        )
    yield


def make_result(
    *,
    supplier="Dindayal Jalan",
    bill_no="DJ-1",
    bill_date="2025-08-17",
    articles=None,
    source_filename="bill.jpg",
    flags=None,
):
    """Build one ExtractionResult-shaped dict for POSTing to /ingest."""
    if articles is None:
        articles = [
            {
                "company": "JAI MATA DI SRT",
                "product": "GREEN TEA",
                "pcs": 4,
                "rate": 567.0,
                "tax_pct": None,
                "margin_pct": None,
                "final_price": 670.0,
            }
        ]
    return {
        "source_filename": source_filename,
        "engine": "groq",
        "bill": {
            "supplier": supplier,
            "bill_no": bill_no,
            "bill_date": bill_date,
            "articles": articles,
        },
        "flags": flags or [],
    }
