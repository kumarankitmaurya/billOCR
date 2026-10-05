"""Checks that turn a silent misread into a visible warning.

Reported symptom: Groq misreads prices and names when the photo is skewed or
the paper isn't flat. The real problem was that nothing could tell — a rate
borrowed from the row above is an unremarkable number, and it went into the
book unchallenged.
"""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.models import Article, BillExtraction
from app.services.image_prep import SKEW_WORTH_FLAGGING, find_skew, skew_flags
from app.services.ocr_strategy import _quality_flags


def bill(articles):
    return BillExtraction(supplier="S", bill_no="B-1", bill_date="2026-01-01", articles=articles)


def line(product="GREEN TEA", pcs=5, rate=100.0, amount=None):
    return Article(company="MILL", product=product, pcs=pcs, rate=rate, amount=amount)


def fake_bill_image(rotate: float = 0.0) -> bytes:
    """A page of evenly spaced dark rows — enough structure for a projection
    profile to lock onto, which is all find_skew looks at."""
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    for y in range(120, 1100, 44):
        draw.rectangle([90, y, 810, y + 13], fill=(20, 20, 20))
    if rotate:
        image = image.rotate(rotate, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


# --- Reconciliation against the bill's own totals -------------------------

def test_a_row_that_does_not_add_up_is_flagged():
    """The bill prints its line totals, the only redundancy the page carries.
    This caught a real error: ANIKA stored at 1792 where the printed total
    said 7295, i.e. 5 x 1459 — the rate had come from the row above."""
    flags = _quality_flags(bill([line("ANIKA", pcs=5, rate=1792.0, amount=7295.0)]))
    assert any("don't match the bill's own totals" in f for f in flags)
    assert any("ANIKA" in f for f in flags)


def test_a_row_that_adds_up_is_not_flagged():
    assert _quality_flags(bill([line(pcs=5, rate=100.0, amount=500.0)])) == []


@pytest.mark.parametrize("amount", [500.0, 495.0, 505.0])
def test_small_discrepancies_are_tolerated(amount):
    """Loose on purpose. These bills carry a discount column this service does
    not capture, and metre-billed rows price per metre — a tight bound would
    flag healthy rows and teach the shopkeeper to ignore the banner."""
    assert _quality_flags(bill([line(pcs=5, rate=100.0, amount=amount)])) == []


def test_a_bill_with_no_legible_amounts_says_prices_are_unverified():
    flags = _quality_flags(bill([line(amount=None), line("OTHER", rate=200.0, amount=None)]))
    assert any("could not be cross-checked" in f for f in flags)


# --- The failure reconciliation cannot see --------------------------------

def test_an_all_identical_read_is_flagged():
    """A badly degraded image returns the same number on every row, and that
    is internally consistent — 5 x 540754 really is 2703770 — so the amount
    check is blind to it. Found when an image-preprocessing attempt returned
    exactly this."""
    flags = _quality_flags(
        bill([line(p, rate=540754.0, amount=2703770.0) for p in ("A", "B", "C", "D")])
    )
    assert any("same price" in f for f in flags)


def test_two_lines_sharing_a_price_is_not_suspicious():
    """Two designs at one price is ordinary; it is only a smell in bulk."""
    assert _quality_flags(bill([line("A", amount=500.0), line("B", amount=500.0)])) == []


def test_no_lines_read_is_flagged():
    assert any("no article lines" in f for f in _quality_flags(bill([])))


# --- Skew measurement ----------------------------------------------------

@pytest.mark.parametrize("tilt", [-7.0, -4.0, 4.0, 7.0])
def test_skew_is_recovered_within_a_degree(tilt):
    """Validated against known rotations, because the whole flag rests on it."""
    measured = find_skew(Image.open(io.BytesIO(fake_bill_image(tilt))))
    assert abs(measured - (-tilt)) <= 1.0, f"measured {measured} for a {tilt} tilt"


def test_a_level_page_is_not_flagged():
    assert skew_flags(fake_bill_image(0.0)) == []


def test_a_tilted_page_is_flagged_with_the_angle():
    flags = skew_flags(fake_bill_image(6.0))
    assert flags and "tilted" in flags[0]


def test_a_barely_tilted_page_is_not_worth_mentioning():
    assert skew_flags(fake_bill_image(SKEW_WORTH_FLAGGING / 2)) == []


def test_an_unreadable_file_produces_no_warning_rather_than_an_error():
    """A photo that cannot be measured must not fail the scan."""
    assert skew_flags(b"not an image at all") == []
