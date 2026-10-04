"""Failure logging.

These exist because every one of these refusals used to leave no trace: a
shopkeeper reporting "it won't save" produced nothing in the logs to look at.
"""

import logging

import pytest
from starlette.testclient import TestClient

from app import logs


# --- Correlation id -------------------------------------------------------

def test_vercel_request_id_is_reused_so_logs_line_up():
    """Using the platform's own id means an app traceback can be matched to
    the request in Vercel's log viewer and to x-vercel-id in the response."""
    assert logs.new_request_id("bom1::iad1::7bb9r-1791039269310-8831c5d0fdb6") == "8831c5d0fdb6"


def test_a_missing_vercel_header_still_gets_an_id():
    """Local runs and non-Vercel hosts must still be correlatable."""
    generated = logs.new_request_id(None)
    assert generated and generated != "-"
    assert generated != logs.new_request_id(None)


def test_outside_a_request_the_id_is_a_placeholder():
    assert logs.current_request_id.get() == "-"


# --- The request log line -------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from app import config, main

    monkeypatch.setattr(config.settings, "app_password", "pw")
    monkeypatch.setattr(config.settings, "db_auto_init", False)
    monkeypatch.setattr(config.settings, "database_url", "postgresql://u:p@127.0.0.1:1/none")
    monkeypatch.setattr(main, "_startup_error", None)
    return TestClient(main.app)


def messages(caplog):
    """Rendered log lines. LogRecord.message does not exist until a formatter
    has run; getMessage() is what applies the % args."""
    return [record.getMessage() for record in caplog.records]


def test_a_rejected_request_is_logged_with_its_reason(client, caplog):
    """A wall of these is how you discover the frontend lost its password."""
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        with client as cl:
            cl.get("/api/bills/providers")
    assert any("missing X-App-Password" in line for line in messages(caplog))


def test_the_attempted_password_is_never_logged(client, caplog):
    with caplog.at_level(logging.WARNING):
        with client as cl:
            cl.get("/api/bills/providers", headers={"X-App-Password": "hunter2"})
    assert "hunter2" not in caplog.text


def test_every_request_logs_method_path_and_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.main"):
        with client as cl:
            cl.get("/health")
    assert any("GET /health -> 200" in line for line in messages(caplog))


def test_the_filter_stamps_every_record_with_the_current_id():
    """The filter lives on the production handler, so caplog never sees it —
    exercised directly instead."""
    record = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    token = logs.current_request_id.set("CAFEBABE")
    try:
        logs._RequestIdFilter().filter(record)
    finally:
        logs.current_request_id.reset(token)
    assert record.request_id == "CAFEBABE"


def test_the_summary_line_carries_the_request_id(client, caplog):
    """An earlier version reset the context var in a `finally` that ran before
    this log call, so the one line most worth correlating was the only line
    without an id on it."""
    caplog.handler.addFilter(logs._RequestIdFilter())
    try:
        with caplog.at_level(logging.INFO, logger="app.main"):
            with client as cl:
                cl.get("/health", headers={"x-vercel-id": "a::b::zz-1-CAFEBABE"})
    finally:
        caplog.handler.filters.clear()

    summaries = [r for r in caplog.records if "GET /health" in r.getMessage()]
    assert summaries, "no summary line was logged"
    assert [r.request_id for r in summaries] == ["CAFEBABE"]
