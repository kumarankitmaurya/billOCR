"""Fallback bill extraction using local Tesseract OCR + regex heuristics.

Used when no Gemini API key is available or the Gemini call fails. This
path is best-effort: Tesseract only gives us raw text, so we have to guess
at structure with regular expressions. Accuracy is noticeably lower than
Gemini, especially for the line-item table.
"""

import io
import re

import pytesseract
from PIL import Image, ImageOps

from app.models import BillData, LineItem

# Common bill vocabulary used to locate summary fields in the raw OCR text.
_TOTAL_RE = re.compile(r"(?:grand\s*total|total\s*amount|net\s*amount)\s*[:\-]?\s*₹?\$?\s*([\d,]+\.?\d*)", re.I)
_SUBTOTAL_RE = re.compile(r"sub\s*-?\s*total\s*[:\-]?\s*₹?\$?\s*([\d,]+\.?\d*)", re.I)
_TAX_RE = re.compile(r"(?:tax|gst|vat)\s*(?:total)?\s*[:\-]?\s*₹?\$?\s*([\d,]+\.?\d*)", re.I)
_BILL_NO_RE = re.compile(r"(?:invoice|bill|receipt)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Za-z0-9\-/]+)", re.I)
_DATE_RE = re.compile(r"(?:date)\s*[:\-]?\s*([\d]{1,4}[/\-.][\d]{1,2}[/\-.][\d]{1,4})", re.I)
_PAYMENT_RE = re.compile(r"(?:payment\s*(?:mode|method)?)\s*[:\-]?\s*(cash|card|upi|credit|debit|cheque|online)", re.I)

# A line-item row: description followed by qty, rate and a trailing total,
# e.g. "Rice 25kg   2   150.00   300.00"
_LINE_ITEM_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9 /\-.]{1,40}?)\s+"
    r"(?P<qty>\d+(?:\.\d+)?)\s+"
    r"(?P<rate>\d+(?:,\d{3})*(?:\.\d+)?)\s+"
    r"(?P<total>\d+(?:,\d{3})*(?:\.\d+)?)\s*$"
)


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    return float(value.replace(",", ""))


def _preprocess(image_bytes: bytes) -> Image.Image:
    """Grayscale + threshold the image so Tesseract has an easier time."""
    image = Image.open(io.BytesIO(image_bytes))
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    # Simple binary threshold: pixels darker than 150 become black, rest white.
    image = image.point(lambda pixel: 0 if pixel < 150 else 255)
    return image


def _parse_line_items(lines: list[str]) -> list[LineItem]:
    items = []
    for serial, line in enumerate(lines, start=1):
        match = _LINE_ITEM_RE.match(line.strip())
        if not match:
            continue
        qty = _to_float(match.group("qty"))
        rate = _to_float(match.group("rate"))
        total = _to_float(match.group("total"))
        items.append(
            LineItem(
                serial_no=serial,
                item_name=match.group("name").strip(),
                description=None,
                hsn_sac_code=None,
                quantity=qty or 0.0,
                unit=None,
                rate=rate or 0.0,
                discount=None,
                tax_rate=None,
                tax_amount=None,
                total=total if total is not None else (qty or 0.0) * (rate or 0.0),
            )
        )
    return items


def extract_bill_data(image_bytes: bytes) -> BillData:
    """Run Tesseract on the image and heuristically parse the result."""
    image = _preprocess(image_bytes)
    text = pytesseract.image_to_string(image)
    lines = [line for line in text.splitlines() if line.strip()]

    bill_no_match = _BILL_NO_RE.search(text)
    date_match = _DATE_RE.search(text)
    payment_match = _PAYMENT_RE.search(text)
    subtotal_match = _SUBTOTAL_RE.search(text)
    tax_match = _TAX_RE.search(text)
    total_match = _TOTAL_RE.search(text)

    # Best guess at vendor name: the first non-empty line of the bill.
    vendor_name = lines[0].strip() if lines else None

    return BillData(
        vendor_name=vendor_name,
        vendor_address=None,
        bill_number=bill_no_match.group(1) if bill_no_match else None,
        bill_date=date_match.group(1) if date_match else None,
        customer_name=None,
        line_items=_parse_line_items(lines),
        subtotal=_to_float(subtotal_match.group(1)) if subtotal_match else None,
        tax_total=_to_float(tax_match.group(1)) if tax_match else None,
        grand_total=_to_float(total_match.group(1)) if total_match else None,
        payment_method=payment_match.group(1) if payment_match else None,
    )
