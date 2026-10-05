"""The shop's pricing rule.

Selling price is not on the bill — the shop sets it, and used to type tax,
margin and price on every line of every bill. These lock down the rule that
now pre-fills them.
"""

import pytest

from app.models import Article, BillExtraction
from app.services import pricing
from app.services.ocr_strategy import _apply_pricing_defaults


# --- The margin tier -----------------------------------------------------

@pytest.mark.parametrize("rate,expected", [
    (1.0, 15.0), (1015.0, 15.0), (1499.0, 15.0),
    (1500.0, 17.0), (1570.0, 17.0), (3901.0, 17.0),
])
def test_margin_tier_switches_at_the_threshold(rate, expected):
    assert pricing.margin_for(rate) == expected


def test_the_threshold_tests_the_bill_rate_not_the_taxed_price():
    """1459 plus 5% tax is 1531.95, over the line. The rule deliberately uses
    the rate printed on the bill, so it can be checked at a glance — and this
    book has 1459 on two lines, so the distinction is real money."""
    assert pricing.margin_for(1459.0) == 15.0
    assert pricing.computed_price(1459.0, 5, 15) > 1500


# --- The computed price --------------------------------------------------

def test_margin_compounds_on_the_tax_inclusive_price():
    """Must match what excel_export writes into columns D and E, or the
    book's own arithmetic stops adding up."""
    assert pricing.computed_price(1000.0, 5, 15) == pytest.approx(1000 * 1.05 * 1.15)


def test_missing_percentages_count_as_zero():
    assert pricing.computed_price(1000.0, None, None) == 1000.0


# --- Landing on a tidy number --------------------------------------------

@pytest.mark.parametrize("rate,expected", [
    (1015.0, 1235), (1459.0, 1770), (1570.0, 1935),
    (1626.0, 2020), (1792.0, 2210), (3901.0, 4800),
])
def test_dear_lines_match_the_agreed_examples(rate, expected):
    """Signed off against the shop's own rates."""
    assert pricing.sell_price(rate) == expected


@pytest.mark.parametrize("rate,expected", [
    (105.0, 130), (457.0, 555), (529.0, 640), (663.0, 805),
])
def test_cheap_lines_round_up_with_no_nudge(rate, expected):
    """Below the nudge threshold the price is rounded up instead. Nearest-5
    with no nudge priced 7 of this book's 12 cheap rates below their computed
    figure — 457 computes to 551.83 and sold at 550 — quietly shaving the
    margin the rule had just added."""
    assert pricing.sell_price(rate) == expected


def test_no_rate_in_the_book_ever_prices_below_its_computed_figure():
    """The property both landing rules exist to guarantee."""
    for rate in [75, 105, 387, 439, 456, 457, 485, 529, 555, 567, 595, 615,
                 663, 1015, 1459, 1570, 1626, 1792, 3901]:
        computed = pricing.computed_price(float(rate), 5, pricing.margin_for(float(rate)))
        assert pricing.sell_price(float(rate)) >= computed, rate


def test_a_dear_price_landing_on_x05_is_pushed_clear():
    """1626 computes to 1997.54; +8 to the nearest 5 is 2005, which reads
    like a mistake rather than a price."""
    assert pricing.sell_price(1626.0) == 2020
    assert pricing.sell_price(1626.0) % 100 != 5


def test_a_halfway_price_rounds_up_not_down():
    """floor(x + 0.5), not round(): Python's round() is banker's rounding, so
    round(246.5) is 246 and a price landing exactly halfway would quietly go
    down, shaving the margin."""
    assert pricing.round_to_nearest(1232.5, 5) == 1235
    assert pricing.round_to_nearest(1237.5, 5) == 1240


def test_every_sell_price_lands_on_the_configured_step():
    for rate in range(100, 5000, 37):
        assert pricing.sell_price(float(rate)) % 5 == 0


def test_an_explicit_tax_or_margin_overrides_the_default():
    """Zero tax and zero margin on a 1000 rate: computed is exactly 1000, so
    it sits on the nudge threshold and is rounded up rather than nudged."""
    assert pricing.sell_price(1000.0, tax_pct=0, margin_pct=0) == 1000


def test_the_nudge_threshold_is_the_computed_price_not_the_rate():
    """A rate under 1000 whose computed price clears it still gets nudged —
    the threshold is about the number the customer sees."""
    assert pricing.computed_price(900.0, 5, 15) > 1000
    assert pricing.sell_price(900.0) == pricing.round_to_nearest(
        pricing.computed_price(900.0, 5, 15) + 8, 5
    )


# --- Pre-filling a scanned bill ------------------------------------------

def line(rate, **kw):
    return Article(company="MILL", product="X", pcs=5, rate=rate, **kw)


def bill(articles):
    return BillExtraction(supplier="S", bill_no="B", bill_date="2026-01-01", articles=articles)


def test_a_scanned_line_arrives_with_tax_margin_and_price_filled():
    b = bill([line(1015.0)])
    _apply_pricing_defaults(b)
    article = b.articles[0]
    assert (article.tax_pct, article.margin_pct, article.final_price) == (5.0, 15.0, 1235.0)


def test_values_already_present_are_never_overwritten():
    """A value that is already there came from somewhere deliberate."""
    b = bill([line(1015.0, tax_pct=0.0, margin_pct=50.0, final_price=999.0)])
    _apply_pricing_defaults(b)
    article = b.articles[0]
    assert (article.tax_pct, article.margin_pct, article.final_price) == (0.0, 50.0, 999.0)


def test_a_partly_filled_line_prices_from_what_it_already_has():
    """Margin was overridden but no price given: the price must follow the
    override, not the default margin."""
    b = bill([line(1000.0, margin_pct=0.0)])
    _apply_pricing_defaults(b)
    article = b.articles[0]
    assert article.margin_pct == 0.0
    assert article.final_price == float(pricing.sell_price(1000.0, 5.0, 0.0))


# --- The policy the frontend applies -------------------------------------

def test_the_policy_exposes_every_number_the_ui_needs():
    """Served rather than duplicated in the UI: the two deploy separately,
    and a drifted copy of a pricing rule is worse than no copy."""
    policy = pricing.policy()
    assert set(policy) == {
        "default_tax_pct", "margin_threshold", "margin_pct_below",
        "margin_pct_above", "sell_price_nudge", "sell_price_round_to",
        "sell_price_nudge_above", "sell_price_ugly_step",
    }
    assert policy["margin_pct_below"] == 15.0
    assert policy["margin_pct_above"] == 17.0
