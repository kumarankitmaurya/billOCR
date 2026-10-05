"""OCR provider strategy — selects and runs a vision model.

Groq is the only provider. The try-in-order machinery below is kept rather
than collapsed into a single call, because it is what makes adding a second
provider a one-entry change — and with one provider there is no fallback at
all, so a bad day at Groq is an outage.
"""

import logging
import time
from typing import Literal

from app.config import settings
from app.models import BillExtraction, ExtractionResult
from app.services import groq_ocr, image_prep

logger = logging.getLogger(__name__)

# The set of providers the user can pick from.
Provider = Literal["auto", "groq"]

# Maps provider names to (extract_fn, needs_api_key_field).
_PROVIDERS = {
    "groq": {
        "extract": groq_ocr.extract_bill_data,
        "key_field": "groq_api_key",
        "label": "groq",
    },
}


def _auto_order(api_key: str | None) -> list[str]:
    """Return the provider try-order based on what's configured.

    If the user supplied an API key via the UI we can't know which provider
    it belongs to, so we try all of them.  Otherwise we only try providers
    whose server-side key is set.
    """
    if api_key:
        # A key supplied from the UI; we cannot tell which provider it belongs
        # to, so try everything we have.
        return list(_PROVIDERS)
    return [name for name in _PROVIDERS if getattr(settings, _PROVIDERS[name]["key_field"])]


def extract(
    image_bytes: bytes,
    mime_type: str,
    filename: str,
    provider: Provider = "auto",
    api_key: str | None = None,
    supplier: str | None = None,
) -> ExtractionResult:
    """Run bill extraction using the selected (or auto-detected) provider.

    Tries each candidate provider in order and raises the last error if all
    of them fail.
    """
    if provider == "auto":
        try_order = _auto_order(api_key)
    else:
        try_order = [provider]

    if not try_order:
        raise ValueError("No OCR provider available: configure GROQ_API_KEY")

    last_error: Exception | None = None

    for name in try_order:
        info = _PROVIDERS[name]
        started = time.monotonic()
        try:
            bill = info["extract"](image_bytes, mime_type, supplier, api_key)
            # Timing is the whole latency budget of a scan, and which provider
            # actually answered is not otherwise visible once a fallback has
            # kicked in. Line counts, never line contents.
            logger.info(
                "%s read %s in %.1fs: %d articles, bill_no=%s",
                name, filename, time.monotonic() - started,
                len(bill.articles), "yes" if bill.bill_no else "MISSING",
            )
            return ExtractionResult(
                source_filename=filename,
                engine=info["label"],
                bill=bill,
                # Skew is measured from the photo rather than inferred from the
                # result: a tilted page is the cause, and saying so lets the
                # shopkeeper fix it instead of hunting for the wrong row.
                flags=image_prep.skew_flags(image_bytes) + _quality_flags(bill),
            )
        except Exception as exc:
            # exception(), not warning(): a provider failure is the single most
            # common real fault here, and the traceback is what distinguishes
            # an auth problem from a timeout from an unparseable response.
            logger.exception(
                "%s failed on %s after %.1fs", name, filename, time.monotonic() - started
            )
            last_error = exc

    logger.error("every provider failed for %s (tried: %s)", filename, ", ".join(try_order))
    raise last_error


# How far amount may differ from pcs * rate before it is worth a second look.
# Loose on purpose: the bill has a discount column this service does not
# capture, and metre-billed rows price per metre rather than per piece, so a
# tight tolerance would flag rows that are perfectly fine and train the
# shopkeeper to ignore the banner. Only a mismatch too large to be either.
_RECONCILE_TOLERANCE = 0.05
_RECONCILE_FLOOR = 2.0


def _reconciliation_flags(bill: BillExtraction) -> list[str]:
    """Flag rows where the printed amount doesn't match pcs * rate.

    The bill prints its own line totals, which is the only redundancy it
    carries — and the thing that catches a misread price. A skewed or
    unflattened photo makes the model pick up a digit from the row above, and
    nothing downstream would ever notice: the number looks perfectly ordinary,
    it just isn't what the bill says.

    This flags rather than rejects. The shop knows which of its suppliers
    discount a line, and a wrong flag costs a glance where a missed misread
    costs a wrong price in the book for good.
    """
    mismatched = []
    for article in bill.articles:
        if article.amount is None or article.amount == 0:
            continue
        expected = article.pcs * article.rate
        slack = max(_RECONCILE_FLOOR, abs(article.amount) * _RECONCILE_TOLERANCE)
        if abs(expected - article.amount) > slack:
            mismatched.append(
                f"{article.product} ({article.pcs} x {article.rate:g} = {expected:g}, "
                f"bill says {article.amount:g})"
            )

    if not mismatched:
        return []
    return [
        f"{len(mismatched)} of {len(bill.articles)} lines don't match the bill's own "
        f"totals — check pcs and price on: {'; '.join(mismatched)}"
    ]


def _uniform_read_flags(bill: BillExtraction) -> list[str]:
    """Catch a read where every line came back the same.

    This is what a badly degraded image produces — not a plausible-looking
    wrong number, but the same number on every row. It was found by accident:
    an image-preprocessing attempt returned all eight rows as
    "5 x 540754, amount 2703770". Reconciliation is blind to it, because a
    uniform read is internally consistent (5 x 540754 really is 2703770), so
    it needs its own check.
    """
    if len(bill.articles) < 3:
        return []
    rates = {a.rate for a in bill.articles}
    if len(rates) == 1:
        return [
            f"every one of the {len(bill.articles)} lines came back at the same price "
            f"({rates.pop():g}) — that is what an unreadable photo looks like, not a bill. "
            "Retake it with more light and the page flat."
        ]
    return []


def _quality_flags(bill: BillExtraction) -> list[str]:
    """Flag what the model couldn't confidently read instead of letting a
    fabricated value pass through silently (see HANDOVER.md §4: never
    silently accept a mismatch/guess)."""
    flags: list[str] = []
    if not bill.bill_no:
        flags.append("bill_no not detected — enter it manually before saving")
    if not bill.bill_date:
        flags.append("bill_date not detected — enter it manually before saving")
    if not bill.articles:
        flags.append("no article lines were read — retake the photo straight on")

    flags.extend(_uniform_read_flags(bill))
    flags.extend(_reconciliation_flags(bill))

    unreadable = sum(1 for a in bill.articles if a.amount is None)
    if bill.articles and unreadable == len(bill.articles):
        # Nothing to reconcile against, so every price on this bill is
        # unverified. Worth saying so rather than looking clean.
        flags.append(
            "the AMOUNT column wasn't legible, so prices could not be "
            "cross-checked — verify them against the bill"
        )
    return flags


def available_providers() -> list[dict]:
    """Return a list of providers and whether they're currently usable.

    Used by the frontend to populate the provider dropdown.
    """
    providers = [{"id": "auto", "name": "Auto (best available)", "available": True}]

    providers.append({
        "id": "groq",
        "name": "Groq (Qwen Vision)",
        "available": bool(settings.groq_api_key),
    })

    return providers
