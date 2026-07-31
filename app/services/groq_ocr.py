"""Bill extraction using Groq's Llama Vision API (free tier).

Groq provides fast inference on open-source models. We use Llama 3.2 Vision
which can read bill images and return structured JSON.
"""

import base64
import logging

from groq import Groq

from app.config import settings
from app.models import BillData

logger = logging.getLogger(__name__)

PROMPT = """You are an expert at reading bills, invoices, and receipts.
Look at the attached image and extract all information into a JSON object
with this exact schema:

{
  "vendor_name": "string or null",
  "vendor_address": "string or null",
  "bill_number": "string or null",
  "bill_date": "string or null",
  "customer_name": "string or null",
  "line_items": [
    {
      "serial_no": "int or null",
      "item_name": "string",
      "description": "string or null",
      "hsn_sac_code": "string or null",
      "quantity": "number",
      "unit": "string or null",
      "rate": "number",
      "discount": "number or null",
      "tax_rate": "number or null",
      "tax_amount": "number or null",
      "total": "number"
    }
  ],
  "subtotal": "number or null",
  "tax_total": "number or null",
  "grand_total": "number or null",
  "payment_method": "string or null"
}

Rules:
- Fill in every field you can find on the bill.
- If a field is not present on the bill, set it to null - do not guess.
- Extract every line item exactly as printed, including quantity, rate,
  discount and tax if shown.
- Numbers must be plain numbers (no currency symbols or commas).
- Return ONLY valid JSON, no markdown fences, no explanation."""


def extract_bill_data(image_bytes: bytes, mime_type: str, api_key: str | None = None) -> BillData:
    """Send a bill image to Groq Llama Vision and return the parsed BillData.

    Raises whatever exception the SDK raises (e.g. auth or network errors)
    so the caller can decide whether to fall back to Tesseract.
    """
    key = api_key or settings.groq_api_key
    if not key:
        raise ValueError("No Groq API key provided")

    client = Groq(api_key=key)

    # Encode image as base64 data URI for the vision model.
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64_image}"

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
                        "text": PROMPT,
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

    return BillData.model_validate_json(raw_text)
