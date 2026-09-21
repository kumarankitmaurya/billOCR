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

    # Upper bound on pooled connections. Deliberately small: Cloud Run runs
    # one worker per instance and caps instances, so a big pool buys nothing
    # and just eats Neon's connection budget.
    db_pool_max_size: int = int(os.getenv("DB_POOL_MAX_SIZE", "4"))

    # CORS origins allowed to call the API (comma-separated).
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")

    # Shared password gating admin-only data in /api/bills/search (base
    # price/rate — confidential, never shown to a plain search). Empty
    # means admin access is disabled entirely: no header value, including
    # an empty one, will match. Set a real value in .env to enable it.
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

settings = Settings()

# No UPLOAD_DIR / OUTPUT_DIR: uploads are read into memory and discarded
# (app/routers/bills.py), and workbooks are streamed from a buffer without
# touching disk (app/services/excel_export.py). The service keeps no files,
# which is also why its host needs no persistent disk.
