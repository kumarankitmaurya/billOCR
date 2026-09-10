"""Bill extraction using Google's Gemini Vision API.

Gemini reads the bill image and returns JSON that already matches our
`BillExtraction` schema via structured output, so no manual text parsing is
needed here.
"""

# pyrefly: ignore [missing-import]
from google import genai
# pyrefly: ignore [missing-import]
from google.genai import types

from app.config import settings
from app.models import BillExtraction

_INSTRUCTIONS = """You extract the article table from a textile trade bill into
the given JSON schema.

{supplier_context}

The bill's DESCRIPTION column stacks TWO things per row — split them:
  company  the mill / brand printed with the line (e.g. ROHAN FAB SRT, JAI MATA DI SRT, SIYARAM)
  product  the design name (e.g. MILK CAKE, ANARKALI, GREEN TEA)

Per row, also read pcs (piece count) and rate (price per piece). Ignore and do
not include amount, discount, gst, hsn/code, or mtr — they are out of scope.

final_price and margin_pct are shop-internal pricing fields filled in by hand
later during review — they are never printed on the bill. Always return null
for both; do not guess, compute, or infer them from rate.

Numbers: strip commas/symbols so they are plain numbers (e.g. "4,000.00" -> 4000).
Skip summary rows (Total C/F, CGST, SGST, Grand Total) and amount-in-words.
If a value isn't clearly legible, do your best rather than guessing wildly, but
never fabricate rows that aren't on the bill.

bill_no and bill_date are often on a letterhead/header block that may not be in
frame (e.g. a continuation page of a multi-page bill). If you cannot clearly
read one of them, return null for that field — do NOT guess, invent digits, or
reuse a nearby number/date. A missing field is far better than a fabricated one."""


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
    """Send a bill image to Gemini and return the parsed BillExtraction.

    Raises whatever exception the SDK raises (e.g. auth or network errors)
    so the caller can decide whether to fall back to another provider.
    """
    key = api_key or settings.gemini_api_key
    if not key:
        raise ValueError("No Gemini API key provided")

    client = genai.Client(api_key=key)

    prompt = _INSTRUCTIONS.format(supplier_context=_supplier_context(supplier))

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BillExtraction,
        ),
    )

    # The SDK already validates against response_schema, but we re-parse
    # through Pydantic ourselves to get a concrete BillExtraction instance.
    return BillExtraction.model_validate_json(response.text)
