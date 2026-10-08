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
from app.auth import admin_access, require_admin, require_app_access
from app.models import BillExtraction, ExtractionResult, StoredLine
from app.services import excel_export, pricing
from app.services.ocr_strategy import Provider, available_providers, extract, reconciles

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


class DraftLine(BaseModel):
    """One reviewed line, as the review screen sends it back.

    `ref` points at the line of the stored draft it came from, which is where
    its rate is; None for a row added by hand. rate/tax_pct/margin_pct are
    the owner's corrections and are ignored from staff, who never see them.
    """

    ref: int | None = None
    company: str
    product: str
    pcs: int
    final_price: float | None = None
    rate: float | None = None
    tax_pct: float | None = None
    margin_pct: float | None = None


class DraftEdit(BaseModel):
    draft_id: str
    bill_no: str | None = None
    bill_date: str | None = None
    articles: list[DraftLine]


class IngestRequest(BaseModel):
    # Reviewed scans, by draft id — what billOCR-ui sends.
    drafts: list[DraftEdit] = []
    # Whole extractions with their rates, for owner-side scripts. Owner only:
    # it lets the caller write any cost into the book.
    results: list[ExtractionResult] = []
    supplier: str | None = None


class IngestResponse(BaseModel):
    ingested: int
    suppliers: list[str]
    # Lines saved for the owner to check, so the UI can say so.
    needs_check: int = 0


class CheckResolution(BaseModel):
    rate: float | None = None
    tax_pct: float | None = None
    margin_pct: float | None = None
    final_price: float | None = None


# A bill on its way into the book: where it came from, the bill, and its
# reviewed lines (None = write bill.articles as they are).
PendingBill = tuple[str, BillExtraction, list[StoredLine] | None]


class SupplierRequest(BaseModel):
    name: str


@router.get("/suppliers")
async def list_suppliers() -> list[str]:
    """The suppliers this book accepts — the set the UI must choose from.

    Served from the database rather than hardcoded so the frontend, which is
    deployed separately, cannot drift from what ingest will actually accept.
    """
    return await run_in_threadpool(db.list_suppliers)


@router.post("/suppliers", status_code=201)
async def add_supplier(payload: SupplierRequest, _: None = Depends(require_admin)) -> dict:
    """Add a supplier. Admin only — this changes the shape of the book.

    Deliberately not something a scan can do as a side effect: implicit
    creation is what let one supplier become two ledgers.
    """
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Supplier name cannot be blank")

    created = await run_in_threadpool(db.add_supplier, name)
    logger.info("supplier %s: %s", "created" if created else "already present", name)
    return {"name": name, "created": created}


@router.get("/pricing")
async def pricing_policy(is_admin: bool = Depends(admin_access)) -> dict:
    """The shop's pricing rule, for the UI to apply as the owner edits.

    Served rather than duplicated in the frontend for the same reason the
    supplier list is: the two deploy separately, and a pricing rule that
    drifts from the backend's is worse than no copy at all.

    Staff get only the price step: tax and margin turn a selling price back
    into the cost.
    """
    return pricing.policy() if is_admin else pricing.staff_policy()


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


async def _require_known_supplier(supplier: str | None) -> None:
    """Reject an unknown supplier before a single OCR call is paid for.

    Checked here as well as at ingest because an OCR call costs money and
    20 seconds: finding out at save time that the supplier was wrong wastes
    both. HANDOVER.md §3 locks this decision — "supplier is chosen in the UI,
    never OCR'd" — and a closed set is what finally enforces it.
    """
    known = await run_in_threadpool(db.list_suppliers)
    if not supplier or supplier.strip() not in known:
        logger.warning("refused: unknown supplier %r", supplier)
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown supplier {supplier!r}. Choose one of: {', '.join(known)}."
                if known
                else "No suppliers have been set up yet. Add one first (admin)."
            ),
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
    rules = await run_in_threadpool(db.margin_rules)
    for file in files:
        image_bytes = await file.read()
        budget -= len(image_bytes)
        if budget < 0:
            # Reached only when multipart didn't declare part sizes, so
            # _validate_batch couldn't check the total up front.
            raise HTTPException(
                status_code=413, detail=_too_large_message(settings.max_request_bytes - budget)
            )
        results.append(
            await _extract_one(file, image_bytes, api_key, provider, supplier, rules)
        )
    return results


async def _extract_one(
    file: UploadFile,
    image_bytes: bytes,
    api_key: str | None,
    provider: Provider = "auto",
    supplier: str | None = None,
    margin_rules: pricing.MarginRules | None = None,
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
            margin_rules=margin_rules,
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
    is_admin: bool = Depends(admin_access),
) -> list[dict]:
    """Extract structured data from one or more bills and return it as JSON
    so the UI can show a preview before the user commits to ingesting it.

    Each scan is held server-side as a draft and the response carries its
    `draft_id`; /ingest takes the reviewed lines back against it. Staff get
    a view with no rate, amount, tax or margin in it at all — hiding them in
    the UI is not enough when they would still be in the JSON.

    `supplier`, if given, is trusted context passed to the extractor instead
    of being read off the image (see HANDOVER.md: supplier is chosen in the
    UI, never OCR'd). If omitted, each bill's supplier is read off the image.
    """
    _validate_batch(files)
    await _require_known_supplier(supplier)
    results = await _extract_batch(files, api_key, provider, supplier)

    views = []
    for result in results:
        draft_id = await run_in_threadpool(db.save_draft, result)
        views.append(_admin_view(result, draft_id) if is_admin else _staff_view(result, draft_id))
    return views


def _admin_view(result: ExtractionResult, draft_id: str) -> dict:
    """Everything, plus the draft id and each line's ref."""
    view = result.model_dump(exclude={"staff_flags", "check_lines"})
    view["draft_id"] = draft_id
    for ref, article in enumerate(view["bill"]["articles"]):
        article["ref"] = ref
    return view


def _staff_view(result: ExtractionResult, draft_id: str) -> dict:
    """What staff review: product, pcs and selling price — nothing that gives cost back.

    Built up from an allowlist rather than by deleting fields from the full
    dump, so a field added to Article later stays hidden until someone
    decides staff should see it.
    """
    bill = result.bill
    return {
        "source_filename": result.source_filename,
        "engine": result.engine,
        "draft_id": draft_id,
        "flags": result.staff_flags,
        "bill": {
            "supplier": bill.supplier,
            "bill_no": bill.bill_no,
            "bill_date": bill.bill_date,
            "articles": [
                {
                    "ref": ref,
                    "company": article.company,
                    "product": article.product,
                    "pcs": article.pcs,
                    "final_price": article.final_price,
                }
                for ref, article in enumerate(bill.articles)
            ],
        },
    }


def _apply_edit(draft: ExtractionResult, edit: DraftEdit, is_admin: bool) -> PendingBill:
    """The reviewed bill: staff's edits laid over the stored scan.

    Rate, tax and margin always come from the draft for staff — they never
    had them, so anything they send for those is ignored. The owner's values
    win where given.
    """
    originals = draft.bill.articles
    lines: list[StoredLine] = []
    for row in edit.articles:
        if row.ref is not None and not 0 <= row.ref < len(originals):
            raise HTTPException(
                status_code=422,
                detail="A line doesn't belong to this scan. Start over and scan the bill again.",
            )
        original = originals[row.ref] if row.ref is not None else None
        rate = original.rate if original else None
        tax_pct = original.tax_pct if original else None
        margin_pct = original.margin_pct if original else None
        if is_admin:
            rate = row.rate if row.rate is not None else rate
            tax_pct = row.tax_pct if row.tax_pct is not None else tax_pct
            margin_pct = row.margin_pct if row.margin_pct is not None else margin_pct

        line = StoredLine(
            company=row.company,
            product=row.product,
            pcs=row.pcs,
            rate=rate,
            final_price=row.final_price,
            margin_pct=margin_pct,
            tax_pct=tax_pct,
        )
        line.needs_check = _needs_check(line, row.ref, original, draft, is_admin)
        lines.append(line)

    bill = draft.bill.model_copy(
        update={"bill_no": edit.bill_no, "bill_date": edit.bill_date, "articles": []}
    )
    return draft.source_filename, bill, lines


def _needs_check(
    line: StoredLine, ref: int | None, original, draft: ExtractionResult, is_admin: bool
) -> bool:
    """Whether the owner has to look at this line's price before trusting it.

    Staff can't see the rate, so they can't fix a misread one; anything that
    makes the price doubtful goes to the owner instead of into the book
    looking settled. Staff are not told which reason applied — "below cost"
    in particular would tell them roughly what the cost is.
    """
    if line.rate is None:
        return True  # added by hand: nobody has said what it cost
    if is_admin:
        return False  # the owner saw the rate while reviewing
    if ref in draft.check_lines:
        return True  # a whole-bill misread
    if reconciles(line.pcs, line.rate, original.amount) is not True:
        # Mismatched, or no printed amount to check against. Asked again with
        # the reviewed pcs, so a mismatch staff fixed by correcting pcs clears.
        return True
    if line.final_price is None:
        return True
    floor = line.rate * (1 + (line.tax_pct or 0) / 100)
    return line.final_price < floor  # priced below cost


def _ingest_results(bills: list[PendingBill], supplier_override: str | None) -> list[str]:
    """Persist each result, returning the distinct suppliers it landed under.

    Refuses to persist any bill missing bill_no/bill_date — those are the DB's
    idempotency key and sort key, so a guessed value would silently corrupt
    the book of record. The model is instructed to return null instead of
    guessing (see ocr_strategy._quality_flags); surface that here as a hard
    error rather than let it through.
    """
    missing = [
        filename for filename, bill, _ in bills if not bill.bill_no or not bill.bill_date
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

    suppliers = _resolve_suppliers([bill for _, bill, _ in bills], supplier_override)

    # Checked for the whole batch first. db.ingest_bill would raise on the
    # first unknown name, but by then earlier bills in the batch are already
    # committed — the same half-written-then-rejected shape /extract had.
    known = set(db.list_suppliers())
    unknown = [s for s in suppliers if s not in known]
    if unknown:
        logger.warning("refused ingest: unknown supplier(s) %s", ", ".join(unknown))
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown supplier(s): {', '.join(unknown)}. "
                f"Choose from: {', '.join(sorted(known)) or '(none set up yet)'}."
            ),
        )

    for _, bill, lines in bills:
        db.ingest_bill(supplier_override or bill.supplier, bill, lines)
    return suppliers


def _resolve_suppliers(bills: list[BillExtraction], supplier_override: str | None) -> list[str]:
    """The distinct suppliers these results would land under, in first-seen order.

    Separate from _ingest_results so /extract can check the batch resolves to
    one supplier *before* anything is written.
    """
    suppliers: list[str] = []
    for bill in bills:
        resolved = supplier_override or bill.supplier
        if resolved not in suppliers:
            suppliers.append(resolved)
    return suppliers


@router.post("/ingest")
async def ingest_bills(
    payload: IngestRequest, is_admin: bool = Depends(admin_access)
) -> IngestResponse:
    """Persist reviewed scans into the book of record.

    Idempotent: re-ingesting a bill with the same (supplier, bill_no) replaces
    its lines rather than duplicating them.
    """
    if not payload.drafts and not payload.results:
        raise HTTPException(status_code=400, detail="No extraction results provided")
    if payload.results and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Saving a whole extraction with its rates requires admin authentication "
            "(X-Admin-Password header). Send the reviewed scan as a draft instead.",
        )

    bills: list[PendingBill] = [(r.source_filename, r.bill, None) for r in payload.results]
    for edit in payload.drafts:
        draft = await run_in_threadpool(db.load_draft, edit.draft_id)
        if draft is None:
            raise HTTPException(
                status_code=410,
                detail=f"This scan has expired (scans are kept {db.DRAFT_TTL_DAYS} days "
                "unsaved). Scan the bill again.",
            )
        bills.append(_apply_edit(draft, edit, is_admin))

    suppliers = await run_in_threadpool(_ingest_results, bills, payload.supplier)
    flagged = sum(line.needs_check for _, _, lines in bills for line in lines or [])
    if flagged:
        logger.info("ingest: %d line(s) saved for the owner to check", flagged)
    return IngestResponse(ingested=len(bills), suppliers=suppliers, needs_check=flagged)


@router.get("/checks")
async def list_checks(_: None = Depends(require_admin)) -> list[dict]:
    """Lines saved with a price the owner has to check. Admin only — they carry rate."""
    return await run_in_threadpool(db.list_checks)


@router.patch("/lines/{line_id}")
async def resolve_check(
    line_id: int, payload: CheckResolution, _: None = Depends(require_admin)
) -> dict:
    """Correct a checked line's pricing and clear its mark. Admin only.

    Only the fields sent are changed. A line still without a rate stays
    marked — a cost-less line in the book is what the mark is for.
    """
    resolved = await run_in_threadpool(
        db.resolve_check, line_id, payload.model_dump(exclude_unset=True)
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"No line {line_id}")
    return {"id": line_id, "needs_check": not resolved}


@router.get("/workbook")
async def get_workbook(
    supplier: str, is_admin: bool = Depends(admin_access)
) -> StreamingResponse:
    """Stream the given supplier's full book, rebuilt from the DB.

    Staff get product, pcs and selling price only; the price, tax and margin
    columns are the owner's.
    """
    try:
        buffer = excel_export.build_supplier_workbook(supplier, include_cost=is_admin)
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
    _: None = Depends(require_admin),
) -> StreamingResponse:
    """Extract bills, ingest them, and stream back the resulting workbook in
    one call. Convenience path for API clients that don't need a preview step.

    Admin only: it writes OCR'd rates straight into the book unreviewed and
    returns the full workbook, cost columns included.

    Single-supplier-per-call only: if `supplier` isn't given and the batch
    resolves to more than one distinct supplier via OCR, use
    `/preview` -> `/ingest` -> `/workbook` per supplier instead.
    """
    _validate_batch(files)
    await _require_known_supplier(supplier)
    results = await _extract_batch(files, api_key, provider, supplier)

    # Checked BEFORE _ingest_results: this used to persist the whole batch and
    # only then reject it, leaving the caller with a 400 and a book of record
    # that had already been written to.
    suppliers = _resolve_suppliers([r.bill for r in results], supplier)
    if len(suppliers) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch resolved to {len(suppliers)} suppliers ({', '.join(suppliers)}); "
                "pass an explicit `supplier` or use /preview + /ingest + /workbook instead."
            ),
        )

    _ingest_results([(r.source_filename, r.bill, None) for r in results], supplier)
    return await get_workbook(suppliers[0], is_admin=True)
