"""Where catalog photos live.

Cloud Run's filesystem is ephemeral, so photos can't sit next to the app.
They also can't sit in Postgres: Neon's free tier is 0.5GB total, and a
catalog of ~1,000 designs at three photos each would eat past that on
images alone, crowding out the data that actually needs to be in a
database. So: object storage, with only the reference kept in `product_image`.

The stored copy is the *uncropped* original (downscaled, EXIF stripped), so
the catalog can be re-embedded later with a real crop step without asking
anyone to re-photograph their stock.
"""

import io
import logging
import uuid
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageOps

from app.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "product-images"


class ImageStore(Protocol):
    def put(self, data: bytes, content_type: str = "image/jpeg") -> str: ...
    def get(self, ref: str) -> bytes: ...
    def delete(self, ref: str) -> None: ...
    def url(self, ref: str) -> str: ...


def normalise_for_storage(image: Image.Image, max_px: int) -> bytes:
    """Downscale, fix rotation, and re-encode as JPEG without metadata.

    Re-encoding is what drops EXIF — which on a phone photo includes the GPS
    coordinates of the shop. Nothing downstream needs it.
    """
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((max_px, max_px), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85, optimize=True)
    return buffer.getvalue()


class LocalImageStore:
    """Filesystem-backed store, for tests and local development."""

    def __init__(self, root: str) -> None:
        self._root = Path(root) / _PREFIX
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, content_type: str = "image/jpeg") -> str:
        name = f"{uuid.uuid4().hex}.jpg"
        (self._root / name).write_bytes(data)
        return f"file://{name}"

    def _path(self, ref: str) -> Path:
        return self._root / ref.removeprefix("file://")

    def get(self, ref: str) -> bytes:
        return self._path(ref).read_bytes()

    def delete(self, ref: str) -> None:
        self._path(ref).unlink(missing_ok=True)

    def url(self, ref: str) -> str:
        return f"/api/bills/visual-search/image/{ref.removeprefix('file://')}"


class GcsImageStore:
    """Google Cloud Storage, which is where these live in production.

    Objects are named with a uuid and served straight from GCS: the app
    never proxies image bytes, which keeps them off a small instance's
    memory and bandwidth. Readable by URL but not listable, so the bucket
    can't be enumerated.

    Credentials come from the runtime's own service account — no key file.
    """

    def __init__(self, bucket_name: str) -> None:
        from google.cloud import storage

        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._bucket_name = bucket_name

    def put(self, data: bytes, content_type: str = "image/jpeg") -> str:
        name = f"{_PREFIX}/{uuid.uuid4().hex}.jpg"
        blob = self._bucket.blob(name)
        blob.cache_control = "public, max-age=31536000, immutable"
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self._bucket_name}/{name}"

    def _blob_name(self, ref: str) -> str:
        return ref.removeprefix(f"gs://{self._bucket_name}/")

    def get(self, ref: str) -> bytes:
        return self._bucket.blob(self._blob_name(ref)).download_as_bytes()

    def delete(self, ref: str) -> None:
        self._bucket.blob(self._blob_name(ref)).delete()

    def url(self, ref: str) -> str:
        return f"https://storage.googleapis.com/{self._bucket_name}/{self._blob_name(ref)}"


def get_image_store() -> ImageStore:
    if settings.image_store == "gcs":
        if not settings.gcs_bucket:
            raise RuntimeError("IMAGE_STORE=gcs but GCS_BUCKET is not set")
        return GcsImageStore(settings.gcs_bucket)
    return LocalImageStore(settings.upload_dir)
