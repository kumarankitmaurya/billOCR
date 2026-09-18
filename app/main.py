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
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings
from app.routers import bills

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
