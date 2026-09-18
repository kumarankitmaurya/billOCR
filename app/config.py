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

    # --- Visual search (find a design by photo) ---

    # Off by default so the app still boots without the model files present.
    visual_search_enabled: bool = os.getenv("VISUAL_SEARCH_ENABLED", "false").lower() == "true"

    # Which model produced the stored vectors. Vectors from different models
    # aren't comparable, so this is recorded in the database and checked at
    # startup: change it and visual search refuses to serve until
    # scripts/reembed_catalog.py has rebuilt the catalog. Never a silent
    # default — see app/services/visual_search/state.py.
    visual_model_name: str = os.getenv("VISUAL_MODEL_NAME", "Marqo/marqo-fashionSigLIP")
    visual_model_revision: str = os.getenv("VISUAL_MODEL_REVISION", "main")
    visual_model_variant: str = os.getenv("VISUAL_MODEL_VARIANT", "onnx-vision-int8")
    visual_model_path: str = os.getenv("VISUAL_MODEL_PATH", "models/onnx/vision_model_int8.onnx")
    visual_embedding_dim: int = int(os.getenv("VISUAL_EMBEDDING_DIM", "768"))

    # onnxruntime otherwise reads the host's core count, not the container's
    # CPU limit, and oversubscribes a 1-vCPU instance.
    ort_intra_op_threads: int = int(os.getenv("ORT_INTRA_OP_THREADS", "1"))

    # Below this cosine score a hit is reported as "no confident match"
    # rather than returned as the least-bad row.
    #
    # TUNE THIS against real photos before trusting it. SigLIP scores sit in
    # a high, narrow band — two unrelated flat colours already measure ~0.92
    # — so this starting value is very likely too permissive. The visual
    # test prints a true-match vs best-wrong-match matrix for exactly this.
    visual_match_threshold: float = float(os.getenv("VISUAL_MATCH_THRESHOLD", "0.75"))

    # Designs returned per query, and rows pulled from the index before
    # grouping by design (several photos can share one design).
    visual_top_k: int = int(os.getenv("VISUAL_TOP_K", "8"))
    visual_candidate_rows: int = int(os.getenv("VISUAL_CANDIDATE_ROWS", "32"))

    # Upload guards. The stored copy is capped so a 12MP phone photo never
    # becomes a full-size bitmap on a small instance.
    visual_upload_max_bytes: int = int(os.getenv("VISUAL_UPLOAD_MAX_BYTES", "15000000"))
    visual_stored_max_px: int = int(os.getenv("VISUAL_STORED_MAX_PX", "1600"))

    # Optional VLM tagging of colour/fabric/border/work for faceted
    # filtering. Off by default: it costs an extra model call per indexed
    # photo, and it plays no part in matching.
    visual_attr_tagging_enabled: bool = (
        os.getenv("VISUAL_ATTR_TAGGING_ENABLED", "false").lower() == "true"
    )

    # Where catalog photos live. "gcs" in production; "local" for tests.
    image_store: str = os.getenv("IMAGE_STORE", "local")
    gcs_bucket: str = os.getenv("GCS_BUCKET", "")

    @property
    def visual_model_key(self) -> str:
        """Identity recorded alongside the vectors, so a model swap is caught."""
        return f"{self.visual_model_name}@{self.visual_model_revision}:{self.visual_model_variant}"


settings = Settings()

# Directories must exist before any file is written to them.
os.makedirs(settings.upload_dir, exist_ok=True)
os.makedirs(settings.output_dir, exist_ok=True)
