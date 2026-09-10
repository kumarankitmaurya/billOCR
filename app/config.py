"""Application settings loaded from environment variables / .env file."""

import os

from dotenv import load_dotenv

# Load variables from a .env file in the project root, if present.
load_dotenv()


class Settings:
    """Central place for all configurable values."""

    # --- Provider API keys (set whichever ones you have) ---

    # Google Gemini
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Groq (free tier — Qwen vision). The old Llama 3.2 Vision models were
    # decommissioned; Qwen3 is the current vision-capable line on Groq.
    groq_api_key: str | None = os.getenv("GROQ_API_KEY")
    groq_model: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")

    # --- General settings ---

    # Where uploaded bill images are temporarily written before OCR.
    upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")

    # Where generated supplier workbooks (.xlsx) are saved on disk, in
    # addition to being streamed back as the download response.
    output_dir: str = os.getenv("OUTPUT_DIR", "output")

    # SQLite database file: the supplier/company/bill/line book of record.
    db_path: str = os.getenv("DB_PATH", "bills.db")

    # CORS origins allowed to call the API (comma-separated).
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")


settings = Settings()

# Directories must exist before any file is written to them.
os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(settings.output_dir, exist_ok=True)
