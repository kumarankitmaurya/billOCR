"""API endpoints for uploading bill images, ingesting them into the book of
record, and downloading a supplier's workbook."""

import logging

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import db
from app.models import ExtractionResult
from app.services import excel_export
from app.services.ocr_strategy import Provider, extract, available_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bills", tags=["bills"])


class IngestRequest(BaseModel):
    results: list[ExtractionResult]
    supplier: str | None = None


class IngestResponse(BaseModel):
    ingested: int
    suppliers: list[str]


@router.get("/providers")
async def list_providers() -> list[dict]:
    """Return the available OCR providers so the frontend can build a dropdown."""
    return available_providers()


async def _extract_one(
    file: UploadFile,
    api_key: str | None,
    provider: Provider = "auto",
    supplier: str | None = None,
) -> ExtractionResult:
    """Extract a single bill using the strategy module.

    ocr_strategy.extract() lets provider errors (bad image, auth, network,
    a malformed model response) propagate so it can try the next provider;
    once every provider is exhausted the last one bubbles up here. Without
    this try/except that reaches FastAPI as an unhandled 500 with a plain
    text body, so the frontend's JSON-only error parsing silently loses the
    real reason and just shows "Server returned 500".
    """
    image_bytes = await file.read()
    try:
        return extract(
            image_bytes=image_bytes,
            mime_type=file.content_type or "image/jpeg",
            filename=file.filename or "bill",
            provider=provider,
            api_key=api_key,
            supplier=supplier,
        )
    except Exception as exc:
        logger.warning("Extraction failed for %s: %s", file.filename, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Couldn't read {file.filename or 'the bill'}: {exc}",
        )


@router.post("/preview")
async def preview_bills(
    files: list[UploadFile],
    supplier: str | None = Form(None),
    api_key: str | None = Form(None),
    provider: Provider = Form("auto"),
) -> list[ExtractionResult]:
    """Extract structured data from one or more bills and return it as JSON
    so the UI can show a preview before the user commits to ingesting it.

    `supplier`, if given, is trusted context passed to the extractor instead
    of being read off the image (see HANDOVER.md: supplier is chosen in the
    UI, never OCR'd). If omitted, each bill's supplier is read off the image.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    return [await _extract_one(file, api_key, provider, supplier) for file in files]


def _ingest_results(results: list[ExtractionResult], supplier_override: str | None) -> list[str]:
    """Persist each result, returning the distinct suppliers it landed under.

    Refuses to persist any bill missing bill_no/bill_date — those are the DB's
    idempotency key and sort key, so a guessed value would silently corrupt
    the book of record. The model is instructed to return null instead of
    guessing (see ocr_strategy._quality_flags); surface that here as a hard
    error rather than let it through.
    """
    missing = [
        result.source_filename
        for result in results
        if not result.bill.bill_no or not result.bill.bill_date
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot save — bill_no or bill_date wasn't detected for: "
                f"{', '.join(missing)}. Re-extract with a clearer photo of the header, "
                "or edit the field before saving."
            ),
        )

    suppliers: list[str] = []
    for result in results:
        resolved = supplier_override or result.bill.supplier
        db.ingest_bill(resolved, result.bill)
        if resolved not in suppliers:
            suppliers.append(resolved)
    return suppliers


@router.post("/ingest")
async def ingest_bills(payload: IngestRequest) -> IngestResponse:
    """Persist previously-previewed extraction results into the book of record.

    Idempotent: re-ingesting a bill with the same (supplier, bill_no) replaces
    its lines rather than duplicating them.
    """
    if not payload.results:
        raise HTTPException(status_code=400, detail="No extraction results provided")

    suppliers = _ingest_results(payload.results, payload.supplier)
    return IngestResponse(ingested=len(payload.results), suppliers=suppliers)


@router.get("/workbook")
async def get_workbook(supplier: str) -> StreamingResponse:
    """Stream the given supplier's full book, rebuilt from the DB."""
    try:
        buffer = excel_export.build_supplier_workbook(supplier)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No ingested bills for supplier: {supplier}")

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={supplier}.xlsx"},
    )


@router.post("/extract")
async def extract_bills(
    files: list[UploadFile],
    supplier: str | None = Form(None),
    api_key: str | None = Form(None),
    provider: Provider = Form("auto"),
) -> StreamingResponse:
    """Extract bills, ingest them, and stream back the resulting workbook in
    one call. Convenience path for API clients that don't need a preview step.

    Single-supplier-per-call only: if `supplier` isn't given and the batch
    resolves to more than one distinct supplier via OCR, use
    `/preview` -> `/ingest` -> `/workbook` per supplier instead.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    results = [await _extract_one(file, api_key, provider, supplier) for file in files]
    suppliers = _ingest_results(results, supplier)

    if len(suppliers) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch resolved to {len(suppliers)} suppliers ({', '.join(suppliers)}); "
                "pass an explicit `supplier` or use /preview + /ingest + /workbook instead."
            ),
        )

    return await get_workbook(suppliers[0])
