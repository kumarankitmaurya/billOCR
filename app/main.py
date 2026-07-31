"""FastAPI application entry point.

Run with:
    uvicorn app.main:app --reload --port 8000
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import bills

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Bill OCR → Excel Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bills.router)

# Serve the frontend's CSS/JS assets under /static, keeping the API routes
# above unaffected since FastAPI matches routes in registration order.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def serve_index() -> FileResponse:
    """Serve the single-page frontend at the root URL."""
    return FileResponse(STATIC_DIR / "index.html")
