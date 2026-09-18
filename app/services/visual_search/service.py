"""Visual search, put together: index catalog photos, find designs by photo.

Everything here is synchronous and CPU-bound (model inference, image
decoding); the router runs it in a threadpool so it doesn't stall the event
loop for other requests on the same instance.
"""

import logging
import time
from dataclasses import dataclass

from app.config import settings
from app.services.visual_search import attr_tagger, repo
from app.services.visual_search.crop import crop_to_garment
from app.services.visual_search.embedder import ImageEmbedder, load_image
from app.services.visual_search.errors import NotReady
from app.services.visual_search.image_store import ImageStore, get_image_store, normalise_for_storage
from app.services.visual_search.state import ModelState

logger = logging.getLogger(__name__)


@dataclass
class VisualSearch:
    embedder: ImageEmbedder
    store: ImageStore
    state: ModelState

    def require_ready(self) -> None:
        if not self.state.check():
            raise NotReady(self.state.reason)

    def _embed_bytes(self, data: bytes):
        image = load_image(data, settings.visual_upload_max_bytes, settings.visual_stored_max_px)
        return image, self.embedder.embed(crop_to_garment(image))

    def index_images(self, company: str, product: str, files: list[bytes]) -> dict:
        """Store and embed each photo of one design.

        Every photo is decoded before anything is written, so one bad file in
        a batch fails the whole request instead of leaving half of it indexed.
        """
        self.require_ready()

        prepared = []
        for data in files:
            image, vector = self._embed_bytes(data)
            prepared.append((normalise_for_storage(image, settings.visual_stored_max_px), vector))

        indexed = []
        attrs_tagged = False
        for jpeg, vector in prepared:
            attrs = attr_tagger.tag(jpeg) if settings.visual_attr_tagging_enabled else None
            attrs_tagged = attrs_tagged or attrs is not None
            ref = self.store.put(jpeg)
            image_id = repo.insert_image(company, product, ref, vector, attrs)
            indexed.append({"id": image_id, "image_url": self.store.url(ref)})

        history = repo.price_history([(company, product)])
        return {
            "company": company,
            "product": product,
            "indexed": indexed,
            # False usually means a typo in the name: the photo is indexed,
            # but no bill line will ever be joined to it.
            "has_price_history": bool(history[repo.design_key(company, product)]),
            "attrs_tagged": attrs_tagged,
        }

    def query(self, data: bytes, k: int, facets: dict[str, str], is_admin: bool) -> dict:
        """Find the designs that look most like the photo, with their prices."""
        self.require_ready()

        started = time.monotonic()
        _, vector = self._embed_bytes(data)
        hits = repo.nearest(vector, settings.visual_candidate_rows, facets)

        # Several photos can belong to one design: keep each design's best.
        designs: dict[tuple[str, str], dict] = {}
        for hit in hits:
            key = repo.design_key(hit["company"], hit["product"])
            card = designs.get(key)
            if card is None:
                card = designs[key] = {
                    "company": hit["company"],
                    "product": hit["product"],
                    "score": hit["score"],
                    "images": [],
                }
            card["images"].append(
                {"id": hit["id"], "url": self.store.url(hit["image_ref"]), "score": round(hit["score"], 4)}
            )

        ranked = sorted(designs.items(), key=lambda item: item[1]["score"], reverse=True)[:k]
        best_score = ranked[0][1]["score"] if ranked else None
        confident = [(key, card) for key, card in ranked if card["score"] >= settings.visual_match_threshold]

        history = repo.price_history([(card["company"], card["product"]) for _, card in confident])
        results = [_price_card(card, history[key], is_admin) for key, card in confident]

        logger.info(
            "visual query: %d hits, %d designs, best=%s, %d above %.2f, %.0fms",
            len(hits),
            len(designs),
            f"{best_score:.4f}" if best_score is not None else None,
            len(results),
            settings.visual_match_threshold,
            (time.monotonic() - started) * 1000,
        )
        return {
            "status": "match" if results else "no_confident_match",
            "threshold": settings.visual_match_threshold,
            # Reported even when nothing clears the threshold — it's what
            # the threshold gets tuned from.
            "best_score": round(best_score, 4) if best_score is not None else None,
            "model": self.state.model_key,
            "results": results,
        }


def _price_card(card: dict, history: list[dict], is_admin: bool) -> dict:
    """Add prices to a match. Base price (`rate`) is removed, not nulled, for non-admins."""
    latest = next((h for h in history if h["final_price"] is not None), None)
    dates = [h["bill_date"] for h in history]

    result = {
        **card,
        "score": round(card["score"], 4),
        "latest_final_price": latest["final_price"] if latest else None,
        "latest_final_price_date": latest["bill_date"] if latest else None,
        "first_seen": min(dates) if dates else None,
        "last_seen": max(dates) if dates else None,
        "history": [dict(h) for h in history],
    }
    if is_admin:
        result["latest_rate"] = history[0]["rate"] if history else None
    else:
        for entry in result["history"]:
            del entry["rate"]
    return result


def startup() -> VisualSearch:
    """Load the model, warm it up and check it against the stored vectors.

    Blocking; the lifespan runs it in a threadpool. A model mismatch doesn't
    fail startup — the rest of the app still serves, and visual search
    answers 503 with the reason until the catalog is re-embedded.
    """
    started = time.monotonic()
    embedder = ImageEmbedder(
        settings.visual_model_path, settings.visual_embedding_dim, settings.ort_intra_op_threads
    )
    embedder.warmup()
    state = ModelState(settings.visual_model_key, settings.visual_embedding_dim)
    state.check(force=True)
    logger.info("visual search ready=%s in %.2fs", state.ready, time.monotonic() - started)
    return VisualSearch(embedder=embedder, store=get_image_store(), state=state)
