"""Bill extraction using Google's Gemini Vision API.

Gemini reads the bill image and returns JSON that already matches our
`BillData` schema via structured output, so no manual text parsing is
needed here (unlike the Tesseract fallback).
"""

from google import genai
from google.genai import types

from app.config import settings
from app.models import BillData

PROMPT = """You are an expert at reading bills, invoices, and receipts.
Look at the attached image and extract all information into the given
JSON schema. Rules:
- Fill in every field you can find on the bill.
- If a field is not present on the bill, leave it null - do not guess.
- Extract every line item exactly as printed, including quantity, rate,
  discount and tax if shown.
- Numbers must be plain numbers (no currency symbols or commas).
"""


def extract_bill_data(image_bytes: bytes, mime_type: str, api_key: str | None = None) -> BillData:
    """Send a bill image to Gemini and return the parsed BillData.

    Raises whatever exception the SDK raises (e.g. auth or network errors)
    so the caller can decide whether to fall back to another provider.
    """
    key = api_key or settings.gemini_api_key
    if not key:
        raise ValueError("No Gemini API key provided")

    client = genai.Client(api_key=key)

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            PROMPT,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=BillData,
        ),
    )

    # The SDK already validates against response_schema, but we re-parse
    # through Pydantic ourselves to get a concrete BillData instance.
    return BillData.model_validate_json(response.text)
