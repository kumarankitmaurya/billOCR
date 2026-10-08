"""The shop's pricing rule, in one place.

Selling price is not on the bill — the shop sets it. Until now the shopkeeper
typed a tax percentage, a margin percentage and a final price on every single
line, which is slow and drifts. This encodes the rule the shop actually uses
so a bill arrives with prices already filled in, still editable per line.

    tax      5% of the bill rate                      (default, editable)
    margin   15% if the bill rate is under 1500
             17% at 1500 and above
    computed rate x (1 + tax) x (1 + margin)          margin on the
                                                      tax-inclusive price,
                                                      matching the exporter

    over 1000   sell = nearest 5 of (computed + 8)
                ...unless that ends in 05, then the next 20
    up to 1000  sell = rounded UP to the next 5, with no nudge

Four decisions worth recording, because none of them is arithmetic:

  * The 1500 margin threshold is tested against the BILL RATE, before tax —
    the number printed on the bill, so it can be checked at a glance. Testing
    the tax-inclusive price would push rates between ~1429 and 1500 into the
    higher tier, which affects real lines in this book (1459 appears twice).
  * The 1000 nudge threshold is tested against the COMPUTED PRICE, not the
    rate: it is about the number the customer sees, so a 850 rate that
    computes past 1000 is nudged.
  * Cheap lines round UP rather than to the nearest 5. Nearest-5 with no
    nudge priced 7 of this book's 12 cheap rates BELOW their computed figure
    — 457 computes to 551.83 and would have sold at 550 — quietly shaving the
    margin the rule had just added. Rounding up removes the need for a nudge
    at all down there.
  * A dear price landing on x05 goes up to the next 20 instead. 1626 computes
    to 1997.54, and +8 rounded to 5 gives 2005, which reads like a mistake
    rather than a price; 2020 reads deliberate.

Nothing here is forced on a line. Every value is a default the review screen
pre-fills and the owner can overwrite.

Per-line margin overrides: a margin rule (db table margin_rule, managed with
scripts/margin_rules.py) can set the margin for one company's design, or for
everything a company sells. The most specific match wins:

    (company, product)  ->  (company, any product)  ->  the 15/17% tier

Matching ignores case and repeated spaces, because the names come from OCR
and "Rohan Fab  SRT" is the same mill as "ROHAN FAB SRT".
"""

import math

from app.config import settings


# (company, product) -> margin %. product "" means every product of that company.
MarginRules = dict[tuple[str, str], float]


def normalise(name: str | None) -> str:
    """The form margin rules are matched on: upper case, single-spaced."""
    return " ".join((name or "").upper().split())


def margin_for(
    rate: float,
    company: str | None = None,
    product: str | None = None,
    rules: MarginRules | None = None,
) -> float:
    """The margin percentage for a line.

    A margin rule for this company's product, then for the company as a
    whole, then the tier by bill rate.
    """
    if rules and company:
        mill = normalise(company)
        for key in ((mill, normalise(product)), (mill, "")):
            if key in rules:
                return rules[key]
    return (
        settings.margin_pct_below
        if rate < settings.margin_threshold
        else settings.margin_pct_above
    )


def computed_price(rate: float, tax_pct: float | None, margin_pct: float | None) -> float:
    """Rate plus tax, plus margin on the tax-inclusive price.

    Margin compounds on the taxed figure rather than the bare rate, which is
    what excel_export already does when it writes columns D and E — the two
    must agree or the book's own arithmetic stops adding up.
    """
    taxed = rate * (1 + (tax_pct or 0) / 100)
    return taxed * (1 + (margin_pct or 0) / 100)


def round_to_nearest(value: float, step: int) -> int:
    """Nearest `step`, rounding a halfway value up.

    floor(x + 0.5) rather than round(): Python's round() is banker's rounding,
    so round(246.5) is 246, and a price landing exactly halfway would quietly
    go down instead of up.
    """
    return int(math.floor(value / step + 0.5) * step)


def round_up_to(value: float, step: int) -> int:
    """The next multiple of `step` at or above `value`."""
    return int(math.ceil(value / step) * step)


def sell_price(rate: float, tax_pct: float | None = None, margin_pct: float | None = None) -> int:
    """The shelf price for a line: computed, then landed on a tidy number.

    Cheap and dear lines are landed differently, and both rules exist to stop
    the price falling below what was computed — see the module docstring.
    """
    tax = settings.default_tax_pct if tax_pct is None else tax_pct
    margin = margin_for(rate) if margin_pct is None else margin_pct
    computed = computed_price(rate, tax, margin)
    step = settings.sell_price_round_to

    if computed <= settings.sell_price_nudge_above:
        # No nudge down here: rounding up already guarantees the price never
        # lands under the computed figure, which is all the nudge was for.
        return round_up_to(computed, step)

    nudged = computed + settings.sell_price_nudge
    landed = round_to_nearest(nudged, step)
    if landed % 100 == settings.sell_price_round_to:
        # x05 — just past a round hundred. Reads like a mistake, so clear it.
        return round_up_to(nudged, settings.sell_price_ugly_step)
    return landed


def defaults_for(
    rate: float,
    company: str | None = None,
    product: str | None = None,
    rules: MarginRules | None = None,
) -> dict:
    """Everything the review screen needs to pre-fill one line."""
    tax = settings.default_tax_pct
    margin = margin_for(rate, company, product, rules)
    return {
        "tax_pct": tax,
        "margin_pct": margin,
        "final_price": float(sell_price(rate, tax, margin)),
    }


def policy() -> dict:
    """The rule itself, for the frontend to apply as the shopkeeper edits.

    Served rather than duplicated in the UI for the same reason the supplier
    list is: the two deploy separately, and a copy of a pricing rule that
    drifts from the backend's is worse than no copy at all.
    """
    return {
        "default_tax_pct": settings.default_tax_pct,
        "margin_threshold": settings.margin_threshold,
        "margin_pct_below": settings.margin_pct_below,
        "margin_pct_above": settings.margin_pct_above,
        "sell_price_nudge": settings.sell_price_nudge,
        "sell_price_round_to": settings.sell_price_round_to,
        "sell_price_nudge_above": settings.sell_price_nudge_above,
        "sell_price_ugly_step": settings.sell_price_ugly_step,
    }


def staff_policy() -> dict:
    """What staff need to price a line: only the step the -/+ buttons move by.

    Not the tiers or the tax. Margin and tax applied to a known selling
    price give the cost straight back, and cost is what staff must not see.
    """
    return {"price_step": settings.sell_price_round_to}
