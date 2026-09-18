"""Refuse to mix vectors from two different models.

An embedding is only comparable with embeddings from the same model, same
weights, same preprocessing. If the configured model changes while the
catalog still holds the old model's vectors, every query would return
confident-looking nonsense. So the model that produced the stored vectors
is recorded in `visual_search_meta`, and the service won't index or query
while the configured model disagrees with it.

The only sanctioned way to switch is scripts/reembed_catalog.py, which
rebuilds every vector and then updates the meta row. The check re-runs at
most once a minute, so a running service recovers after a re-embed without
a redeploy.
"""

import logging
import threading
import time

from app import db

logger = logging.getLogger(__name__)

RECHECK_SECONDS = 60


class ModelState:
    """Whether the configured model matches the vectors in the database."""

    def __init__(self, model_key: str, dim: int) -> None:
        self.model_key = model_key
        self.dim = dim
        self.ready = False
        self.reason: str | None = "not checked yet"
        self._checked_at = 0.0
        self._lock = threading.Lock()

    def check(self, force: bool = False) -> bool:
        """Re-read the meta row if it's been a while, and return readiness."""
        with self._lock:
            if not force and time.monotonic() - self._checked_at < RECHECK_SECONDS:
                return self.ready
            self._checked_at = time.monotonic()
            self.ready, self.reason = self._evaluate()
            if not self.ready:
                logger.error("visual search disabled: %s", self.reason)
            return self.ready

    def _evaluate(self) -> tuple[bool, str | None]:
        with db._connect() as conn:
            meta = conn.execute("SELECT model, dim FROM visual_search_meta").fetchone()
            has_images = conn.execute("SELECT EXISTS (SELECT 1 FROM product_image)").fetchone()[0]

            if meta is None and not has_images:
                # Fresh catalog: whatever is configured now becomes the record.
                conn.execute(
                    "INSERT INTO visual_search_meta (model, dim) VALUES (%s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (self.model_key, self.dim),
                )
                meta = conn.execute("SELECT model, dim FROM visual_search_meta").fetchone()

        if meta is None:
            return False, (
                "product_image has vectors but no record of which model made them; "
                "run scripts/reembed_catalog.py --confirm"
            )

        stored_model, stored_dim = meta
        if stored_model != self.model_key or stored_dim != self.dim:
            return False, (
                f"stored vectors are from {stored_model} (dim {stored_dim}) but the configured "
                f"model is {self.model_key} (dim {self.dim}); "
                "run scripts/reembed_catalog.py --confirm to switch"
            )
        return True, None
