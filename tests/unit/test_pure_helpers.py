"""Unit tests for the helpers that need no database.

Deliberately importable and runnable on a bare checkout: the integration
suite skips entirely without TEST_DATABASE_URL, so without these `pytest`
exits green having executed nothing.
"""

from collections import deque

import pytest
from openpyxl import Workbook

from app.config import _api_key
from app.db import _merge_pricing
from app.models import Article, BillExtraction, ExtractionResult
from app.routers.bills import _attachment_disposition, _resolve_suppliers
from app.services.excel_export import _safe_sheet_title


def article(company="JAI MATA DI SRT", product="GREEN TEA", **kwargs):
    return Article(company=company, product=product, pcs=4, rate=567.0, **kwargs)


def result(supplier, filename="bill.jpg"):
    return ExtractionResult(
        source_filename=filename,
        engine="groq",
        bill=BillExtraction(supplier=supplier, bill_no="B1", bill_date="2025-01-01"),
    )


# --- Excel sheet titles ---------------------------------------------------

@pytest.mark.parametrize("name", ["M/S ROHAN FAB", "A*B SRT", "A[C]", "A?B", "A:B", "A\\B"])
def test_sheet_title_survives_every_character_openpyxl_rejects(name):
    """Mill names on these bills carry slashes ("M/S ...", "A/C"). Before this
    was sanitised, one such line made that supplier's whole workbook
    permanently un-downloadable, with no endpoint to edit the line back out."""
    Workbook().create_sheet(_safe_sheet_title(name, set()))


def test_sheet_titles_stay_unique_and_within_the_length_limit():
    used: set[str] = set()
    first = _safe_sheet_title("M/S ROHAN FAB", used)
    second = _safe_sheet_title("M/S ROHAN FAB", used)
    assert first != second
    assert len(_safe_sheet_title("A" * 40, used)) <= 31


def test_a_company_name_of_only_bad_characters_still_gets_a_title():
    assert _safe_sheet_title("///", set())


# --- Re-ingest must not destroy hand-entered pricing ----------------------

def test_reingest_without_pricing_keeps_what_was_stored():
    """tax/margin/final_price are typed by hand and never printed on the bill,
    so OCR can never reproduce them. Re-scanning a bill to fix a misread rate
    must not wipe them."""
    preserved = {("JAI MATA DI SRT", "GREEN TEA"): deque([(670.0, 12.0, 5.0)])}
    assert _merge_pricing(article(), preserved) == (670.0, 12.0, 5.0)


def test_supplied_pricing_wins_field_by_field():
    preserved = {("JAI MATA DI SRT", "GREEN TEA"): deque([(670.0, 12.0, 5.0)])}
    assert _merge_pricing(article(final_price=699.0), preserved) == (699.0, 12.0, 5.0)


def test_the_same_product_twice_on_one_bill_keeps_each_rows_pricing():
    preserved = {("X", "P"): deque([(1.0, None, None), (2.0, None, None)])}
    first = _merge_pricing(article("X", "P"), preserved)
    second = _merge_pricing(article("X", "P"), preserved)
    assert (first[0], second[0]) == (1.0, 2.0)


def test_a_new_line_does_not_inherit_a_neighbours_pricing():
    assert _merge_pricing(article("NEW MILL", "NEW DESIGN"), {}) == (None, None, None)


# --- /extract resolves suppliers before it writes anything ----------------

def test_distinct_suppliers_in_first_seen_order():
    batch = [result("A"), result("B"), result("A")]
    assert _resolve_suppliers(batch, None) == ["A", "B"]


def test_an_explicit_supplier_collapses_the_batch():
    assert _resolve_suppliers([result("A"), result("B")], "Override") == ["Override"]


# --- Download filename ----------------------------------------------------

def test_filename_with_spaces_is_quoted_and_encoded():
    """An unquoted Content-Disposition filename ends at the first space, so
    "Dindayal Jalan.xlsx" used to download as "Dindayal"."""
    disposition = _attachment_disposition("Dindayal Jalan.xlsx")
    assert '"Dindayal Jalan.xlsx"' in disposition
    assert "filename*=UTF-8''Dindayal%20Jalan.xlsx" in disposition


def test_non_ascii_filename_keeps_an_ascii_fallback():
    disposition = _attachment_disposition("Râm & Co.xlsx")
    assert "filename*=UTF-8''R%C3%A2m%20%26%20Co.xlsx" in disposition
    assert disposition.split(";")[1].strip().isascii()


# --- Placeholder API keys count as unset ----------------------------------

@pytest.mark.parametrize("value", ["your_gemini_api_key_here", "YOUR-KEY", "", "   ", "<paste here>"])
def test_placeholder_keys_read_as_unset(monkeypatch, value):
    """An unedited .env.example placeholder is non-empty, so every truthiness
    check used to treat it as configured: the provider was advertised as
    available, tried first, and failed auth on every single request."""
    monkeypatch.setenv("SOME_API_KEY", value)
    assert _api_key("SOME_API_KEY") is None


def test_a_real_key_is_kept(monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "gsk_realkey123")
    assert _api_key("SOME_API_KEY") == "gsk_realkey123"
