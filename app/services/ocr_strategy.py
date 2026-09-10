"""OCR provider strategy — selects and runs the best available vision model.

Provider priority:
1. If the caller explicitly picks a provider (via the UI dropdown), use that.
2. Otherwise, auto-select based on which API keys are configured:
   Gemini → Groq.

Each provider is attempted in order; if one fails, the next is tried
automatically so the user always gets a result.
"""

import logging
from typing import Literal

from app.config import settings
from app.models import BillExtraction, ExtractionResult
from app.services import gemini_ocr, groq_ocr

logger = logging.getLogger(__name__)

# The set of providers the user can pick from.
Provider = Literal["auto", "gemini", "groq"]

# Maps provider names to (extract_fn, needs_api_key_field).
_PROVIDERS = {
    "gemini": {
        "extract": gemini_ocr.extract_bill_data,
        "key_field": "gemini_api_key",
        "label": "gemini",
    },
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
    order: list[str] = []

    if api_key:
        # User provided a key — try both cloud providers.
        order = ["gemini", "groq"]
    else:
        # Use whichever server-side keys are configured.
        if settings.gemini_api_key:
            order.append("gemini")
        if settings.groq_api_key:
            order.append("groq")

    return order


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
        raise ValueError("No OCR provider available: configure GEMINI_API_KEY or GROQ_API_KEY")

    last_error: Exception | None = None

    for name in try_order:
        info = _PROVIDERS[name]
        try:
            logger.info("Trying provider: %s", name)
            bill = info["extract"](image_bytes, mime_type, supplier, api_key)
            return ExtractionResult(
                source_filename=filename, engine=info["label"], bill=bill, flags=_quality_flags(bill)
            )
        except Exception as exc:
            logger.warning("%s extraction failed for %s: %s", name, filename, exc)
            last_error = exc

    raise last_error


def _quality_flags(bill: BillExtraction) -> list[str]:
    """Flag fields the model couldn't confidently read instead of letting a
    fabricated value pass through silently (see HANDOVER.md §4: never
    silently accept a mismatch/guess)."""
    flags: list[str] = []
    if not bill.bill_no:
        flags.append("bill_no not detected — enter it manually before saving")
    if not bill.bill_date:
        flags.append("bill_date not detected — enter it manually before saving")
    return flags


def available_providers() -> list[dict]:
    """Return a list of providers and whether they're currently usable.

    Used by the frontend to populate the provider dropdown.
    """
    providers = [{"id": "auto", "name": "Auto (best available)", "available": True}]

    providers.append({
        "id": "gemini",
        "name": "Google Gemini",
        "available": bool(settings.gemini_api_key),
    })
    providers.append({
        "id": "groq",
        "name": "Groq (Qwen Vision)",
        "available": bool(settings.groq_api_key),
    })

    return providers
