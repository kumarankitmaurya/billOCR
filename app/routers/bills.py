"""API endpoints for uploading bill images, ingesting them into the book of
record, and downloading a supplier's workbook."""

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import db
from app.config import settings
from app.auth import admin_access, require_app_access
from app.models import ExtractionResult
from app.services import excel_export
from app.services.ocr_strategy import Provider, extract, available_providers

logger = logging.getLogger(__name__)

# The app-access gate is declared on the router, not per endpoint, so a new
# endpoint is private by default — forgetting to add it can't quietly publish
# the book of record. Base price stays separately gated (see /search).
router = APIRouter(
    prefix="/api/bills", tags=["bills"], dependencies=[Depends(require_app_access)]
)


def _attachment_disposition(filename: str) -> str:
    """Content-Disposition for a download, quoted and RFC 5987-encoded.

    Supplier names contain spaces ("Dindayal Jalan.xlsx"), and an unquoted
    filename token ends at the first one — the browser would save it as
    "Dindayal". The ASCII fallback is for clients that ignore filename*.
    """
    ascii_fallback = filename.encode("ascii", "replace").decode().replace('"', "'")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


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


# The first bytes of the formats a phone camera produces. Checked instead of
# trusting file.content_type, which is whatever the client chose to send.
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",      # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"RIFF",              # WebP (RIFF....WEBP)
    b"II*\x00",           # TIFF little-endian
    b"MM\x00*",           # TIFF big-endian
)


def _validate_batch(files: list[UploadFile]) -> None:
    """Reject an upload that is empty or larger than this service will carry.

    Each image costs an OCR call and is held in memory through a base64
    expansion, and they are extracted serially at 10-20s each — so an
    unbounded batch is both a cost and a request-timeout problem.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    if len(files) > settings.max_upload_files:
        logger.warning(
            "refused upload: %d files, limit %d", len(files), settings.max_upload_files
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many files ({len(files)}); this service takes at most "
                f"{settings.max_upload_files} per upload. Send them in smaller batches."
            ),
        )

    # Multipart gives us each part's size up front, so an oversized batch is
    # rejected before a single byte is read or a single OCR call is spent.
    declared = sum(file.size or 0 for file in files)
    if declared > settings.max_request_bytes:
        # Worth watching: a steady stream of these means billOCR-ui's
        # client-side downscale is not running in the shop's browser.
        logger.warning(
            "refused upload: %.1fMB across %d files, limit %.1fMB",
            declared / (1024 * 1024), len(files),
            settings.max_request_bytes / (1024 * 1024),
        )
        raise HTTPException(status_code=413, detail=_too_large_message(declared))


def _too_large_message(total: int) -> str:
    return (
        f"Upload is {total / (1024 * 1024):.1f}MB; the limit is "
        f"{settings.max_request_bytes / (1024 * 1024):.1f}MB per request. "
        "Send fewer bills at a time, or photograph them at a lower resolution."
    )


def _validate_image(filename: str, image_bytes: bytes) -> None:
    """Reject a file that is too big, or that isn't actually an image."""
    if len(image_bytes) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{filename} is {len(image_bytes) // (1024 * 1024)}MB; the limit is "
                f"{settings.max_upload_bytes // (1024 * 1024)}MB. Photograph the bill "
                "at a lower resolution."
            ),
        )
    if not image_bytes.startswith(_IMAGE_MAGIC):
        logger.warning("refused %s: first bytes are not a known image format", filename)
        raise HTTPException(
            status_code=400,
            detail=f"{filename} doesn't look like an image (JPEG, PNG, WebP or TIFF).",
        )


async def _extract_batch(
    files: list[UploadFile],
    api_key: str | None,
    provider: Provider,
    supplier: str | None,
) -> list[ExtractionResult]:
    """Extract every file in a validated batch, serially.

    Serial on purpose: the OCR call is the whole latency budget, and running
    them concurrently would multiply peak memory (each image is held in
    memory and base64-inflated by a third) for no wall-clock gain once the
    provider rate-limits. The batch size cap is what keeps the total inside
    the platform's request timeout.
    """
    results: list[ExtractionResult] = []
    budget = settings.max_request_bytes
    for file in files:
        image_bytes = await file.read()
        budget -= len(image_bytes)
        if budget < 0:
            # Reached only when multipart didn't declare part sizes, so
            # _validate_batch couldn't check the total up front.
            raise HTTPException(
                status_code=413, detail=_too_large_message(settings.max_request_bytes - budget)
            )
        results.append(await _extract_one(file, image_bytes, api_key, provider, supplier))
    return results


async def _extract_one(
    file: UploadFile,
    image_bytes: bytes,
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
    _validate_image(file.filename or "the file", image_bytes)
    try:
        # In a threadpool: a 10-20s OCR call would otherwise block every
        # other request on this instance.
        return await run_in_threadpool(
            extract,
            image_bytes=image_bytes,
            mime_type=file.content_type or "image/jpeg",
            filename=file.filename or "bill",
            provider=provider,
            api_key=api_key,
            supplier=supplier,
        )
    except HTTPException:
        raise
    except Exception:
        # Logged, not returned: provider SDK exceptions carry request URLs,
        # model ids and occasionally fragments of credentials, and this detail
        # goes straight to the client.
        logger.exception("Extraction failed for %s", file.filename)
        raise HTTPException(
            status_code=502,
            detail=(
                f"Couldn't read {file.filename or 'the bill'}. Retake the photo with "
                "more light and the whole bill in frame, or try another provider."
            ),
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
    _validate_batch(files)
    return await _extract_batch(files, api_key, provider, supplier)


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
        # The most likely thing a shopkeeper reports as "it won't save".
        logger.warning("refused ingest: bill_no/bill_date missing for %s", ", ".join(missing))
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot save — bill_no or bill_date wasn't detected for: "
                f"{', '.join(missing)}. Re-extract with a clearer photo of the header, "
                "or edit the field before saving."
            ),
        )

    suppliers = _resolve_suppliers(results, supplier_override)
    for result in results:
        db.ingest_bill(supplier_override or result.bill.supplier, result.bill)
    return suppliers


def _resolve_suppliers(results: list[ExtractionResult], supplier_override: str | None) -> list[str]:
    """The distinct suppliers these results would land under, in first-seen order.

    Separate from _ingest_results so /extract can check the batch resolves to
    one supplier *before* anything is written.
    """
    suppliers: list[str] = []
    for result in results:
        resolved = supplier_override or result.bill.supplier
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
        logger.warning("workbook requested for unknown supplier: %s", supplier)
        raise HTTPException(status_code=404, detail=f"No ingested bills for supplier: {supplier}")
    except ValueError as exc:
        # openpyxl rejects some sheet titles outright. _safe_sheet_title should
        # have made that impossible, so this is a 500 rather than a 4xx — but a
        # named one, because the alternative is an opaque unhandled traceback on
        # a supplier whose book is then silently un-downloadable.
        logger.exception("Workbook build failed for supplier %s", supplier)
        raise HTTPException(
            status_code=500, detail=f"Couldn't build the workbook for {supplier}: {exc}"
        )

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _attachment_disposition(f"{supplier}.xlsx")},
    )


@router.get("/search")
async def search_bills(
    name: str | None = None,
    min_final_price: float | None = None,
    max_final_price: float | None = None,
    min_base_price: float | None = None,
    max_base_price: float | None = None,
    limit: int = Query(db.DEFAULT_SEARCH_LIMIT, ge=1, le=db.MAX_SEARCH_LIMIT),
    offset: int = Query(0, ge=0),
    is_admin: bool = Depends(admin_access),
) -> list[dict]:
    """Search every ingested line across every supplier, by product name
    substring and/or price range.

    Base price (`rate`) is confidential: a plain search never sees it, and
    can't filter by it. A valid `X-Admin-Password` header unlocks both (see
    app/auth.py).
    """
    if (min_base_price is not None or max_base_price is not None) and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Searching by base price requires admin authentication (X-Admin-Password header)",
        )

    rows = db.search_lines(
        name=name,
        min_final_price=min_final_price,
        max_final_price=max_final_price,
        min_base_price=min_base_price,
        max_base_price=max_base_price,
        limit=limit,
        offset=offset,
    )

    if not is_admin:
        for row in rows:
            del row["rate"]

    return rows


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
    _validate_batch(files)
    results = await _extract_batch(files, api_key, provider, supplier)

    # Checked BEFORE _ingest_results: this used to persist the whole batch and
    # only then reject it, leaving the caller with a 400 and a book of record
    # that had already been written to.
    suppliers = _resolve_suppliers(results, supplier)
    if len(suppliers) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch resolved to {len(suppliers)} suppliers ({', '.join(suppliers)}); "
                "pass an explicit `supplier` or use /preview + /ingest + /workbook instead."
            ),
        )

    _ingest_results(results, supplier)
    return await get_workbook(suppliers[0])
