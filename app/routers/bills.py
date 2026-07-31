"""API endpoints for uploading bill images and getting back extracted data / Excel files."""

import logging

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.models import ExtractionResult
from app.services import excel_export, gemini_ocr, tesseract_ocr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bills", tags=["bills"])


async def _extract_one(file: UploadFile, api_key: str | None) -> ExtractionResult:
    """Extract a single bill, preferring Gemini and falling back to Tesseract."""
    image_bytes = await file.read()

    try:
        bill = gemini_ocr.extract_bill_data(image_bytes, file.content_type or "image/jpeg", api_key)
        return ExtractionResult(source_filename=file.filename or "bill", engine="gemini", bill=bill)
    except Exception as exc:
        # Any Gemini failure (missing key, auth error, network issue, etc.)
        # falls through to the local Tesseract path instead of failing the request.
        logger.warning("Gemini extraction failed for %s, falling back to Tesseract: %s", file.filename, exc)
        bill = tesseract_ocr.extract_bill_data(image_bytes)
        return ExtractionResult(source_filename=file.filename or "bill", engine="tesseract", bill=bill)


@router.post("/preview")
async def preview_bills(files: list[UploadFile], api_key: str | None = Form(None)) -> list[ExtractionResult]:
    """Extract structured data from one or more bills and return it as JSON
    so the UI can show a preview before the user commits to downloading."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    return [await _extract_one(file, api_key) for file in files]


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
    preview-then-download flow costs exactly one Gemini call, not two.
    """
    if not results:
        raise HTTPException(status_code=400, detail="No extraction results provided")

    return _xlsx_response(results)


@router.post("/extract")
async def extract_bills(files: list[UploadFile], api_key: str | None = Form(None)) -> StreamingResponse:
    """Extract bills and stream back a workbook in a single call.

    Convenience path for API clients that don't need a preview step; the UI
    uses /preview + /workbook instead to avoid extracting the same files twice.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    results = [await _extract_one(file, api_key) for file in files]
    return _xlsx_response(results)
