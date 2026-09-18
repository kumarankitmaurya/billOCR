"""API endpoints for finding a saree design by photo.

Indexing catalog photos is admin-only. Querying is public, but base price
(`rate`) is only in the response for a valid admin header — absent, not
null, for everyone else, same as /api/bills/search.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from app.auth import admin_access, require_admin
from app.config import settings
from app.services.visual_search.errors import ImageDecodeError, ImageTooLarge, NotReady

if TYPE_CHECKING:
    from app.services.visual_search.service import VisualSearch

router = APIRouter(prefix="/api/bills/visual-search", tags=["visual-search"])

_MAX_K = 20


def _service(request: Request) -> "VisualSearch":
    service = getattr(request.app.state, "visual", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Visual search is not enabled on this server")
    return service


async def _run(fn, *args):
    """Run blocking inference off the event loop, mapping failures to HTTP errors."""
    try:
        return await run_in_threadpool(fn, *args)
    except NotReady as exc:
        raise HTTPException(status_code=503, detail=f"Visual search is unavailable: {exc}")
    except ImageTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except ImageDecodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/status")
async def status(request: Request) -> dict:
    service = getattr(request.app.state, "visual", None)
    if service is None:
        return {"enabled": False, "ready": False, "model": None, "dim": None,
                "threshold": settings.visual_match_threshold, "reason": "disabled by config"}
    ready = await run_in_threadpool(service.state.check)
    return {
        "enabled": True,
        "ready": ready,
        "model": service.state.model_key,
        "dim": service.state.dim,
        "threshold": settings.visual_match_threshold,
        "reason": service.state.reason,
    }


@router.post("/index", dependencies=[Depends(require_admin)])
async def index_images(
    request: Request,
    files: list[UploadFile],
    company: str = Form(...),
    product: str = Form(...),
) -> dict:
    """Add one or more photos of a single design to the catalog.

    `has_price_history: false` in the response means no bill line matches
    this company/product yet — often a typo. The photo is indexed anyway.
    """
    service = _service(request)
    company, product = company.strip(), product.strip()
    if not company or not product:
        raise HTTPException(status_code=422, detail="company and product are required")
    if not files:
        raise HTTPException(status_code=422, detail="No files uploaded")

    payloads = [await file.read() for file in files]
    return await _run(service.index_images, company, product, payloads)


@router.post("/query")
async def query(
    request: Request,
    file: UploadFile,
    k: int = Form(settings.visual_top_k),
    color: str | None = Form(None),
    fabric: str | None = Form(None),
    border: str | None = Form(None),
    work: str | None = Form(None),
    is_admin: bool = Depends(admin_access),
) -> dict:
    """Find the catalog designs that look like this photo, with price history.

    Always 200 when the search ran: `status` is `match` or
    `no_confident_match`, and `best_score` is reported either way.
    """
    service = _service(request)
    if not 1 <= k <= _MAX_K:
        raise HTTPException(status_code=422, detail=f"k must be between 1 and {_MAX_K}")

    facets = {
        key: value.strip()
        for key, value in {"color": color, "fabric": fabric, "border": border, "work": work}.items()
        if value and value.strip()
    }
    data = await file.read()
    return await _run(service.query, data, k, facets, is_admin)


# Only used with IMAGE_STORE=local (tests, local dev). In production images
# are served straight from GCS and never pass through the app.
_LOCAL_NAME = re.compile(r"^[0-9a-f]{32}\.jpg$")


@router.get("/image/{name}")
async def local_image(name: str) -> FileResponse:
    if settings.image_store != "local" or not _LOCAL_NAME.match(name):
        raise HTTPException(status_code=404)
    path = Path(settings.upload_dir) / "product-images" / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")
