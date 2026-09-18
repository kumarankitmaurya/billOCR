"""Rebuild every catalog vector with the currently configured model.

The only supported way to change the embedding model (or its preprocessing,
or the crop step). Vectors from different models can't be compared, so the
service refuses to serve visual search while the configured model differs
from the one recorded in `visual_search_meta` — this script is what brings
them back into agreement:

  1. Deletes the meta row, so running services answer 503 meanwhile.
  2. Drops the HNSW index and nulls every embedding.
  3. Changes the column's vector size if the dimension changed.
  4. Re-embeds each photo from its stored (uncropped) original.
  5. Rebuilds the index and records the new model.

Run it with the same image and environment as the service (in production,
as a Cloud Run Job), so the runtime that embeds the catalog is the runtime
that embeds queries:

    python scripts/reembed_catalog.py --confirm

Uses the DIRECT connection string if DIRECT_DATABASE_URL is set, since this
is one long-running session doing DDL.
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from pgvector.psycopg import register_vector  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.services.visual_search.crop import crop_to_garment  # noqa: E402
from app.services.visual_search.embedder import ImageEmbedder, load_image  # noqa: E402
from app.services.visual_search.image_store import get_image_store  # noqa: E402

INDEX_SQL = (
    "CREATE INDEX product_image_embedding_hnsw ON product_image "
    "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--confirm", action="store_true", help="actually do it")
    args = parser.parse_args()

    model_key, dim = settings.visual_model_key, settings.visual_embedding_dim
    if not args.confirm:
        print(f"Would re-embed the whole catalog with {model_key} (dim {dim}). Pass --confirm.")
        return 1

    url = os.environ.get("DIRECT_DATABASE_URL") or settings.database_url
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    db.init_db()  # make sure the tables exist before touching them
    embedder = ImageEmbedder(settings.visual_model_path, dim, settings.ort_intra_op_threads)
    store = get_image_store()
    started = time.monotonic()

    with psycopg.connect(url, prepare_threshold=None) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM visual_search_meta")
            conn.execute("DROP INDEX IF EXISTS product_image_embedding_hnsw")
            conn.execute("UPDATE product_image SET embedding = NULL")
            conn.execute(f"ALTER TABLE product_image ALTER COLUMN embedding TYPE vector({int(dim)})")
        register_vector(conn)
        conn.commit()

        rows = conn.execute("SELECT id, image_ref FROM product_image ORDER BY id").fetchall()
        print(f"re-embedding {len(rows)} photos with {model_key}")
        failed = []
        for n, (image_id, ref) in enumerate(rows, 1):
            try:
                image = load_image(store.get(ref), max_bytes=sys.maxsize, max_px=settings.visual_stored_max_px)
                vector = embedder.embed(crop_to_garment(image))
            except Exception as exc:  # keep going; report at the end
                failed.append((image_id, ref, exc))
                continue
            conn.execute("UPDATE product_image SET embedding = %s WHERE id = %s", (vector, image_id))
            conn.commit()
            if n % 50 == 0:
                print(f"  {n}/{len(rows)}")

        with conn.transaction():
            conn.execute(INDEX_SQL)
            conn.execute(
                "INSERT INTO visual_search_meta (model, dim) VALUES (%s, %s) "
                "ON CONFLICT (id) DO UPDATE SET model = EXCLUDED.model, dim = EXCLUDED.dim, "
                "updated_at = now()",
                (model_key, dim),
            )

    print(f"done in {time.monotonic() - started:.1f}s: {len(rows) - len(failed)} embedded, {len(failed)} failed")
    for image_id, ref, exc in failed:
        print(f"  FAILED id={image_id} {ref}: {exc}", file=sys.stderr)
    # Failed rows keep a NULL embedding and are skipped by queries.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
