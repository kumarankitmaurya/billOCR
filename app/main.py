"""FastAPI application entry point.

API-only backend — the frontend is the billOCR-ui React app (a sibling
repo), which talks to this over the CORS-open /api/bills/* endpoints. See
FRONTEND.md for the API contract.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db
from app.config import settings
from app.routers import bills

# uvicorn configures only its own loggers; without this the app's INFO lines
# (startup timings) would be dropped. On a hosted runtime, stderr goes to
# the platform's log collector.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Set when database setup failed at startup, so /health can report the reason
# instead of leaving an operator to guess from a platform error page.
_startup_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Set up the database before serving, tear it down after.

    Schema creation and pool warm-up happen here rather than at import time
    so that a database that's unreachable fails the startup probe instead of
    leaving a half-alive process serving 500s. Each step is timed, because
    on a scale-to-zero host these are the cold start.
    """
    started = time.monotonic()
    try:
        if settings.db_auto_init:
            db.init_db()
        db.open_pool()
        logger.info("startup complete in %.2fs", time.monotonic() - started)
    except Exception:
        # Deliberately not fatal. Raising here aborts ASGI startup, which on a
        # serverless host means every request — including /health — fails with
        # an opaque platform-level 500, and the actual reason is only visible
        # in the build logs. Starting anyway keeps /health answering and able
        # to say what is wrong, which is the entire point of a health probe.
        global _startup_error
        # Only the exception class, never its message. /health is the one
        # unauthenticated endpoint, and a psycopg connection failure spells out
        # every resolved host and IP of the database — which is not something
        # to publish. The full traceback goes to the logs instead.
        _startup_error = type(sys.exc_info()[1]).__name__
        logger.exception("database setup failed — serving anyway; /health will report it")
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
async def health() -> JSONResponse:
    """Liveness probe, and the first place to look when a deploy misbehaves.

    Reports configuration rather than querying the database: a probe that
    runs a query turns a brief Neon hiccup into a restart loop. But it does
    say whether the database is configured and whether startup succeeded,
    because "a server error has occurred" from the platform tells an operator
    nothing, and this is the one endpoint reachable without a password.
    """
    problems = []
    if not settings.database_url:
        problems.append("DATABASE_URL is not set")
    if not settings.app_password:
        problems.append("APP_PASSWORD is not set (every request will 503)")
    if _startup_error:
        problems.append(f"database setup failed at startup: {_startup_error} (see logs)")

    body: dict[str, object] = {"status": "error" if problems else "ok"}
    if problems:
        body["problems"] = problems
    return JSONResponse(body, status_code=200 if not problems else 503)


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
