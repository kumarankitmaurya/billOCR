"""API endpoints for uploading bill images and getting back extracted data / Excel files."""

import logging

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import ExtractionResult
from app.services import excel_export
from app.services.ocr_strategy import Provider, extract, available_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bills", tags=["bills"])


@router.get("/providers")
async def list_providers() -> list[dict]:
    """Return the available OCR providers so the frontend can build a dropdown."""
    return available_providers()


async def _extract_one(
    file: UploadFile,
    api_key: str | None,
    provider: Provider = "auto",
) -> ExtractionResult:
    """Extract a single bill using the strategy module."""
    image_bytes = await file.read()
    return extract(
        image_bytes=image_bytes,
        mime_type=file.content_type or "image/jpeg",
        filename=file.filename or "bill",
        provider=provider,
        api_key=api_key,
    )


@router.post("/preview")
async def preview_bills(
    files: list[UploadFile],
    api_key: str | None = Form(None),
    provider: Provider = Form("auto"),
) -> list[ExtractionResult]:
    """Extract structured data from one or more bills and return it as JSON
    so the UI can show a preview before the user commits to downloading."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    return [await _extract_one(file, api_key, provider) for file in files]


def _xlsx_response(results: list[ExtractionResult]) -> StreamingResponse:
    """Build the workbook and wrap it in a download response."""
    buffer = excel_export.build_excel(results)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=extracted_bills.xlsx"},
    )


@router.post("/workbook")
async def build_workbook(results: list[ExtractionResult]) -> StreamingResponse:
    """Turn already-extracted data into a workbook, running no OCR.

    This is what the UI uses for its download button: it posts back the
    results it got from /preview instead of re-uploading the images, so a
    preview-then-download flow costs exactly one OCR call, not two.
    """
    if not results:
        raise HTTPException(status_code=400, detail="No extraction results provided")

    return _xlsx_response(results)


@router.post("/extract")
async def extract_bills(
    files: list[UploadFile],
    api_key: str | None = Form(None),
    provider: Provider = Form("auto"),
) -> StreamingResponse:
    """Extract bills and stream back a workbook in a single call.

    Convenience path for API clients that don't need a preview step; the UI
    uses /preview + /workbook instead to avoid extracting the same files twice.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    results = [await _extract_one(file, api_key, provider) for file in files]
    return _xlsx_response(results)
