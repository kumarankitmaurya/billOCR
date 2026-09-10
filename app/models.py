"""Pydantic schemas shared by the OCR services, the API layer, and the Excel exporter.

The same `BillExtraction` model doubles as the Gemini `response_schema`, so its
field descriptions double as extraction instructions for the model.

Fields match the textile bill contract in output-format.md: a bill's
DESCRIPTION column stacks company (mill/brand) and product (design name), and
only pcs/rate are captured for now — amount, discount, gst, hsn/code and mtr
are intentionally dropped, not just left null.
"""

from pydantic import BaseModel, Field


class Article(BaseModel):
    """A single product line on a bill, billed under one company/mill."""

    company: str = Field(..., description="Mill/brand the line is billed under, e.g. 'ROHAN FAB SRT'")
    product: str = Field(..., description="Design name, e.g. 'MILK CAKE'")
    pcs: int = Field(..., description="Piece count for this line")
    rate: float = Field(..., description="Price per piece")
    final_price: float | None = Field(
        None,
        description=(
            "Selling price the shop sets for this line. Never printed on the bill — "
            "always return null; it's filled in by hand during review, same as SP."
        ),
    )
    margin_pct: float | None = Field(
        None,
        description=(
            "Margin percent the shop applies to this line. Never printed on the "
            "bill — always return null; it's filled in by hand during review."
        ),
    )
    tax_pct: float | None = Field(
        None,
        description=(
            "Tax percent applied to this line before margin. Never printed on "
            "the bill — always return null; it's filled in by hand during "
            "review. Chains with margin_pct: final_price = rate * "
            "(1 + tax_pct/100) * (1 + margin_pct/100), computed client-side."
        ),
    )


class BillExtraction(BaseModel):
    """Structured representation of one textile bill."""

    supplier: str = Field(..., description="Who issued the bill")
    bill_no: str | None = Field(
        None, description="Invoice number as printed on the bill, or null if not clearly legible"
    )
    bill_date: str | None = Field(
        None, description="Date the bill was issued, as YYYY-MM-DD, or null if not clearly legible"
    )
    articles: list[Article] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Wraps a BillExtraction with metadata about which engine produced it."""

    source_filename: str
    engine: str  # "gemini" or "groq"
    bill: BillExtraction
    flags: list[str] = Field(
        default_factory=list,
        description="Data-quality warnings the caller should surface, e.g. a missing bill_no/date",
    )
