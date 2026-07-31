"""Pydantic schemas shared by the OCR services, the API layer, and the Excel exporter.

The same `BillData` model doubles as the Gemini `response_schema`, so its field
descriptions double as extraction instructions for the model.
"""

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    """A single row of a bill/invoice (one product or service)."""

    serial_no: int | None = Field(None, description="Row/serial number as printed on the bill")
    item_name: str = Field(..., description="Name of the product or service")
    description: str | None = Field(None, description="Extra details about the item, if present")
    hsn_sac_code: str | None = Field(None, description="HSN or SAC code, if present")
    quantity: float = Field(..., description="Quantity purchased")
    unit: str | None = Field(None, description="Unit of measure, e.g. kg, pcs, ltr")
    rate: float = Field(..., description="Price per unit")
    discount: float | None = Field(None, description="Discount, as percentage or absolute amount")
    tax_rate: float | None = Field(None, description="Tax/GST rate as a percentage")
    tax_amount: float | None = Field(None, description="Tax amount for this line")
    total: float = Field(..., description="Line total after discount and tax")


class BillData(BaseModel):
    """Structured representation of an entire bill/invoice.

    Every field besides `line_items` is optional because bill layouts vary
    widely; missing fields are simply left as None instead of guessed.
    """

    vendor_name: str | None = Field(None, description="Name of the seller/vendor/shop")
    vendor_address: str | None = Field(None, description="Address of the seller/vendor")
    bill_number: str | None = Field(None, description="Invoice/bill/receipt number")
    bill_date: str | None = Field(None, description="Date the bill was issued")
    customer_name: str | None = Field(None, description="Name of the buyer/customer, if present")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = Field(None, description="Sum of line items before tax")
    tax_total: float | None = Field(None, description="Total tax amount across all items")
    grand_total: float | None = Field(None, description="Final amount payable")
    payment_method: str | None = Field(None, description="Cash, card, UPI, etc., if mentioned")


class ExtractionResult(BaseModel):
    """Wraps a BillData with metadata about which engine produced it."""

    source_filename: str
    engine: str  # "gemini" or "tesseract"
    bill: BillData
