"""FastAPI application entry point.

API-only backend — the frontend is the billOCR-ui React app (a sibling
repo), which talks to this over the CORS-open /api/bills/* endpoints. See
FRONTEND.md for the API contract.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings
from app.routers import bills, visual_search

# uvicorn configures only its own loggers; without this the app's INFO lines
# (startup timings, visual query scores) would be dropped. On Cloud Run,
# stderr goes straight to Cloud Logging.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up the database before serving, tear it down after.

    Schema creation and pool warm-up happen here rather than at import time
    so that a database that's unreachable fails the startup probe instead of
    leaving a half-alive process serving 500s. Each step is timed, because
    on a scale-to-zero host these are the cold start.
    """
    started = time.monotonic()
    db.init_db()
    db.open_pool()
    app.state.visual = None
    if settings.visual_search_enabled:
        # Imported here so a server with visual search off never loads
        # onnxruntime at all.
        from app.services.visual_search import service as visual_service

        app.state.visual = await run_in_threadpool(visual_service.startup)
    logger.info("startup complete in %.2fs", time.monotonic() - started)
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Bill OCR → Excel Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills.router)
app.include_router(visual_search.router)
