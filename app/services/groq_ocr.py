"""Bill extraction using Groq's Llama Vision API (free tier).

Groq provides fast inference on open-source models. We use Llama Vision
which can read bill images and return structured JSON.
"""

import base64
import logging

# pyrefly: ignore [missing-import]
from groq import Groq

from app.config import settings
from app.models import BillExtraction

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """You extract the article table from a textile trade bill
into strict JSON.

{supplier_context}

The bill's DESCRIPTION column stacks TWO things per row — split them:
  company  the mill / brand printed with the line (e.g. ROHAN FAB SRT, JAI MATA DI SRT, SIYARAM)
  product  the design name (e.g. MILK CAKE, ANARKALI, GREEN TEA)

Per row, also read pcs (piece count) and rate (price per piece). Ignore and do
not include amount, discount, gst, hsn/code, or mtr — they are out of scope.

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
       "final_price": null, "margin_pct": null }}
  ]
}}

Rules:
- Extract every article row exactly as printed.
- Return ONLY valid JSON, no markdown fences, no explanation."""


def _supplier_context(supplier: str | None) -> str:
    if supplier:
        return (
            f'The supplier is exactly "{supplier}" — use this value for the '
            '"supplier" field. Do not read a different seller name off the image.'
        )
    return 'Read the seller/supplier name from the bill header and put it in the "supplier" field.'


def extract_bill_data(
    image_bytes: bytes,
    mime_type: str,
    supplier: str | None = None,
    api_key: str | None = None,
) -> BillExtraction:
    """Send a bill image to Groq Llama Vision and return the parsed BillExtraction.

    Raises whatever exception the SDK raises (e.g. auth or network errors)
    so the caller can decide whether to fall back to another provider.
    """
    key = api_key or settings.groq_api_key
    if not key:
        raise ValueError("No Groq API key provided")

    client = Groq(api_key=key)

    # Encode image as base64 data URI for the vision model.
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64_image}"

    prompt = _PROMPT_TEMPLATE.format(supplier_context=_supplier_context(supplier))

    response = client.chat.completions.create(
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
        temperature=0.1,
        max_tokens=4096,
    )

    raw_text = response.choices[0].message.content.strip()

    # Strip markdown fences if the model wraps the JSON in ```json ... ```
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[1]  # Remove opening fence line
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3].strip()

    logger.debug("Groq raw response: %s", raw_text[:500])

    return BillExtraction.model_validate_json(raw_text)
