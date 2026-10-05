"""Bill extraction using Groq's vision model.

The model is Qwen (see GROQ_MODEL in app/config.py) — the Llama Vision models
this originally used were decommissioned. Groq is the only provider, so a
failure here is a failed scan, not a fallback.
"""

import base64
import logging

# pyrefly: ignore [missing-import]
from groq import Groq, RateLimitError

from app.config import settings
from app.models import BillExtraction

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """You extract the article table from a textile trade bill
into strict JSON.

{supplier_context}

The bill's DESCRIPTION column stacks TWO things per row — split them:
  company  the mill / brand printed with the line (e.g. ROHAN FAB SRT, JAI MATA DI SRT, SIYARAM)
  product  the design name (e.g. MILK CAKE, ANARKALI, GREEN TEA)

Per row, read pcs (piece count), rate (price per piece), and amount (the
printed line total). Ignore discount, gst, hsn/code and mtr — out of scope.

The amount matters even though it is not stored: amount should equal
pcs * rate, and that is how a misread price gets caught. Read it as printed —
never compute it, and never adjust rate or pcs to make the arithmetic work. A
row that does not add up must come back not adding up.

The photo may be skewed, rotated, or of a page that was not lying flat. Follow
each row along its own baseline rather than along a straight horizontal line,
because a tilted page makes a value from the row above or below look aligned
with this one. If a cell is genuinely unreadable return null for that field
rather than borrowing the neighbouring row's value or inventing a plausible
number.

Numbers: strip commas/symbols so they are plain numbers (e.g. "4,000.00" -> 4000).
Skip summary rows (Total C/F, CGST, SGST, Grand Total) and amount-in-words.

bill_no and bill_date are often on a letterhead/header block that may not be in
frame (e.g. a continuation page of a multi-page bill). If you cannot clearly
read one of them, return JSON null for that field — do NOT guess, invent
digits, or reuse a nearby number/date. A missing field is far better than a
fabricated one.

final_price and margin_pct are shop-internal pricing fields filled in by hand
later during review — they are never printed on the bill. Always return null
for both; do not guess, compute, or infer them from rate.

Return a JSON object with this exact schema:

{{
  "supplier": "string",
  "bill_no": "string or null",
  "bill_date": "YYYY-MM-DD or null",
  "articles": [
    {{ "company": "string", "product": "string", "pcs": "int", "rate": "number",
       "amount": "number or null", "final_price": null, "margin_pct": null }}
  ]
}}

Rules:
- Extract every article row exactly as printed.
- Return ONLY valid JSON, no markdown fences, no explanation."""


def _supplier_context(supplier: str | None) -> str:
    """Supplier is always supplied now, and always trusted.

    The branch that asked the model to read the seller name off the letterhead
    is gone. It is what produced two ledgers for one supplier — a human types
    the short name the shop uses, while OCR reads the full legal name off the
    header. HANDOVER.md §3 always said supplier is chosen in the UI and never
    OCR'd; a closed supplier set is what finally makes that true.
    """
    return (
        f'The supplier is exactly "{supplier}" — use this value for the '
        '"supplier" field. Do not read a different seller name off the image.'
    )


def _complete(client: "Groq", image_url: str, prompt: str):
    """The provider call itself, kept separate so the error handling above
    reads as a list of failure modes rather than wrapping a 20-line call."""
    return client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }
        ],
        # 0, not 0.1: this is transcription, and the same photo should give
        # the same answer twice.
        temperature=0,
        # Back to 4096 after 8192 proved counter-productive: Groq's on-demand
        # tier caps output tokens per minute, and a request whose expected
        # output exceeds the remaining budget is rejected outright rather than
        # queued. A long bill needs ~1100 output tokens, so this is already
        # generous — and the truncation check below is what makes an
        # over-long bill fail loudly instead of silently losing its last rows.
        max_tokens=4096,
    )


def extract_bill_data(
    image_bytes: bytes,
    mime_type: str,
    supplier: str | None = None,
    api_key: str | None = None,
) -> BillExtraction:
    """Send a bill image to Groq and return the parsed BillExtraction.

    Raises whatever exception the SDK raises (e.g. auth or network errors);
    ocr_strategy logs it with a traceback and reports the failure.
    """
    key = api_key or settings.groq_api_key
    if not key:
        raise ValueError("No Groq API key provided")

    client = Groq(api_key=key)

    # Encode image as base64 data URI for the vision model.
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64_image}"

    prompt = _PROMPT_TEMPLATE.format(supplier_context=_supplier_context(supplier))

    try:
        response = _complete(client, image_url, prompt)
    except RateLimitError as exc:
        # Groq's free tier allows ~1000 output tokens a minute and one bill
        # costs roughly 1100, so a second bill inside the same minute is
        # refused. Worth saying plainly: the generic provider error reads as
        # "the scan failed" when the scan was fine and merely too soon.
        raise RuntimeError(
            "The OCR provider's per-minute limit was reached — this plan allows "
            "about one bill a minute. Wait a moment and scan this bill again, or "
            "upgrade the Groq plan to scan several at once."
        ) from exc

    if not response.choices:
        raise ValueError("Groq returned no choices")

    choice = response.choices[0]
    # A truncated response is the dangerous failure here: the JSON is cut off
    # mid-array, so without this check a long bill silently loses its last
    # rows rather than failing.
    if choice.finish_reason == "length":
        raise ValueError(
            "The model's reply was cut off before the bill ended — the bill has "
            "more rows than one response can hold. Photograph it in two halves."
        )
    if choice.message.content is None:
        raise ValueError("Groq returned an empty reply (refusal or tool call)")

    raw_text = choice.message.content.strip()

    # Strip markdown fences if the model wraps the JSON in ```json ... ```
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]  # Remove opening fence line
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

    logger.debug("Groq raw response: %s", raw_text[:500])

    return BillExtraction.model_validate_json(raw_text)
