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

    # Groq (free tier — Llama 3.2 Vision)
    groq_api_key: str | None = os.getenv("GROQ_API_KEY")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.2-90b-vision-preview")

    # --- General settings ---

    # Where uploaded bill images are temporarily written before OCR.
    upload_dir: str = os.getenv("UPLOAD_DIR", "uploads")

    # CORS origins allowed to call the API (comma-separated).
    cors_origins: list[str] = os.getenv("CORS_ORIGINS", "*").split(",")


settings = Settings()

# Directory must exist before any file is written to it.
os.makedirs(settings.upload_dir, exist_ok=True)
