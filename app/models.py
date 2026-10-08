"""Pydantic schemas shared by the OCR services, the API layer, and the Excel exporter.

Field descriptions double as extraction instructions: they are read by the
vision model as part of the prompt.

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
    amount: float | None = Field(
        None,
        description=(
            "Line total as printed on the bill (the AMOUNT column), or null if "
            "not legible. Read it even though it is not stored: it is the only "
            "redundancy the bill carries, and amount vs pcs*rate is what "
            "catches a misread price."
        ),
    )
    final_price: float | None = Field(
        None,
        description=(
            "Selling price the shop sets for this line. Never printed on the bill — "
            "always return null. The server then pre-fills it from the shop's "
            "pricing rule (app/services/pricing.py), and the review screen can "
            "still overwrite it."
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
    engine: str  # which provider read it, e.g. "groq"
    bill: BillExtraction
    flags: list[str] = Field(
        default_factory=list,
        description="Data-quality warnings the caller should surface, e.g. a missing bill_no/date",
    )
    # The same warnings with no prices in them, for staff. "5 x 1792 = 8960,
    # bill says 7295" tells whoever reads it what the shop paid.
    staff_flags: list[str] = Field(default_factory=list)
    # Indexes into bill.articles that the owner must check whatever staff do
    # on the review screen (an unreadable photo reads every line the same).
    check_lines: list[int] = Field(default_factory=list)


class StoredLine(BaseModel):
    """One line as it is written to the book, after review.

    Not Article: Article's field descriptions are the extraction prompt, and
    two of these differ from it on purpose. `rate` can be unknown — a row
    staff added by hand has no cost until the owner fills it in — and
    `needs_check` is a review outcome, not something read off a bill.
    """

    company: str
    product: str
    pcs: int
    rate: float | None
    final_price: float | None = None
    margin_pct: float | None = None
    tax_pct: float | None = None
    needs_check: bool = False

    @classmethod
    def from_article(cls, article: Article) -> "StoredLine":
        return cls.model_validate(article.model_dump())
