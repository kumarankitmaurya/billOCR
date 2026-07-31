"""Application settings loaded from environment variables / .env file."""

import os

from dotenv import load_dotenv

# Load variables from a .env file in the project root, if present.
load_dotenv()


class Settings:
    """Central place for all configurable values."""

    # Fallback API key used when the client doesn't supply one via the UI.
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")

    # Gemini model used for structured bill extraction.
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # Where uploaded bill images are temporarily written before OCR.
    upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")

    # CORS origins allowed to call the API (comma-separated).
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")


settings = Settings()

# Directory must exist before any file is written to it.
os.makedirs(settings.upload_dir, exist_ok=True)
