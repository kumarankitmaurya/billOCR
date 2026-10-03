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

# uvicorn configures only its own loggers; without this the app's INFO lines
# (startup timings) would be dropped. On a hosted runtime, stderr goes to
# the platform's log collector.
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
    logger.info("startup complete in %.2fs", time.monotonic() - started)
    try:
        yield
    finally:
        db.close_pool()


# /docs and /openapi.json enumerate every endpoint, and /health is the
# liveness probe, so nothing needs them in production. ENABLE_DOCS=true turns
# them back on for local work.
app = FastAPI(
    title="Bill OCR → Excel Service",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)


@app.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness probe for the hosting platform.

    Deliberately does not touch the database: the pool is opened in the
    lifespan above, so a process that answers here has already connected
    once. Making this a query would turn a brief Neon hiccup into a
    restart loop, which is worse than serving a stale-but-alive instance.
    """
    return {"status": "ok"}


# allow_credentials is deliberately absent: the frontend authenticates with
# the X-App-Password / X-Admin-Password headers, not cookies, so credentialed
# requests are never made — and asking for them is what made the old wildcard
# origin invalid, since browsers reject "*" on a credentialed request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not settings.cors_origins:
    logger.warning(
        "CORS_ORIGINS is not set — no browser origin can call this API. "
        "Set it to the frontend's origin (e.g. https://billocr-ui.vercel.app)."
    )

app.include_router(bills.router)
