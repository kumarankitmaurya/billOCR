"""Application settings loaded from environment variables / .env file."""

import os
import re

from dotenv import load_dotenv

# Load variables from a .env file in the project root, if present.
load_dotenv()

# .env.example ships placeholders like `your_gemini_api_key_here`. Copied to
# .env and left unedited they are non-empty, so every truthiness check treats
# them as a configured key: the provider is advertised as available, tried
# first, and fails auth on every request before falling back. Treat them as
# unset instead.
_PLACEHOLDER = re.compile(r"^(your[_-]|<|changeme|xxx+$)", re.IGNORECASE)


def _api_key(name: str) -> str | None:
    """An API key from the environment, or None if unset or still a placeholder."""
    value = (os.getenv(name) or "").strip()
    return None if not value or _PLACEHOLDER.match(value) else value


class Settings:
    """Central place for all configurable values."""

    # --- Provider API keys (set whichever ones you have) ---

    # Google Gemini
    gemini_api_key: str | None = _api_key("GEMINI_API_KEY")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Groq (free tier — Qwen vision). The old Llama 3.2 Vision models were
    # decommissioned; Qwen3 is the current vision-capable line on Groq.
    groq_api_key: str | None = _api_key("GROQ_API_KEY")
    groq_model: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

    # --- General settings ---

    # Postgres connection string for the supplier/company/bill/line book of
    # record. Use Neon's *pooled* endpoint (the `-pooler` hostname): it's
    # PgBouncer in transaction mode, which is what a web service wants. The
    # direct endpoint is only for migrations. Required — there's no local
    # fallback, so a missing value fails at startup rather than silently
    # writing somewhere unexpected.
    database_url: str = os.getenv("DATABASE_URL", "")

    # --- Upload limits ---
    #
    # /preview and /extract spend a paid OCR call per image and hold each one
    # in memory (base64 inflates it by a third), so both are bounded. The file
    # cap is 3 rather than 10 because every image is extracted serially at
    # 10-20s each, and a bigger batch outlives the platform's request timeout.
    max_upload_files: int = int(os.getenv("MAX_UPLOAD_FILES", "3"))
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024)))

    # Total across the whole request, not per file. Vercel Functions reject a
    # request body over 4.5MB with a platform-level 413 before any of this
    # code runs, so the cap sits just under that and produces a message the
    # shopkeeper can act on instead. A 2.7MB phone photo fits; two do not —
    # billOCR-ui needs to downscale before upload (server-side redaction
    # cannot help here, it runs after the body has already arrived).
    max_request_bytes: int = int(os.getenv("MAX_REQUEST_BYTES", str(4 * 1024 * 1024)))

    # Create the schema at startup. True is right for local development and a
    # first deploy; set it false once the schema exists so a serverless cold
    # start doesn't re-run CREATE TABLE and take an advisory lock every time.
    db_auto_init: bool = os.getenv("DB_AUTO_INIT", "true").lower() == "true"

    # Serve /docs and /openapi.json. Off by default: they enumerate every
    # endpoint, and /health is the liveness probe now, so nothing needs them
    # in production.
    enable_docs: bool = os.getenv("ENABLE_DOCS", "false").lower() == "true"

    # Upper bound on pooled connections. Deliberately small: the service runs
    # as a Vercel Function, where Fluid compute shares one instance across
    # concurrent invocations and scales to zero. A big pool buys nothing there
    # and just eats Neon's connection budget. See also min_size=0 in app/db.py.
    db_pool_max_size: int = int(os.getenv("DB_POOL_MAX_SIZE", "4"))

    # CORS origins allowed to call the API (comma-separated). No wildcard
    # default: "*" let any site on the internet call this API, and it is
    # also invalid when paired with credentialed requests, so browsers
    # reject it. Unset means no cross-origin caller is allowed, which is the
    # safe direction to fail — set your frontend's real origin.
    cors_origins: list[str] = [
        origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()
    ]

    # --- The two credentials ---
    #
    # There are deliberately two, because the shop has two roles. Staff look
    # products up by selling price; only the owner sees what the shop paid.
    # Collapsing them into one password would publish the cost column to
    # everyone who can reach the API.

    # APP_PASSWORD gates the API at all, as the X-App-Password header: every
    # endpoint requires it. Empty means the service refuses every request
    # rather than serving the whole book of record to the internet — there is
    # no "open by default" mode. Set it in .env for local development too.
    app_password: str = os.getenv("APP_PASSWORD", "")

    # ADMIN_PASSWORD additionally unlocks base price (rate) in
    # /api/bills/search, as the X-Admin-Password header. Empty means admin
    # access is disabled entirely: no header value, including an empty one,
    # will match — so `rate` is simply never returned.
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

settings = Settings()

# No UPLOAD_DIR / OUTPUT_DIR: uploads are read into memory and discarded
# (app/routers/bills.py), and workbooks are streamed from a buffer without
# touching disk (app/services/excel_export.py). The service keeps no files,
# which is also why its host needs no persistent disk.
