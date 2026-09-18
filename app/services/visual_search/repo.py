"""SQL for visual search: catalog photos in, nearest designs and their prices out."""

import numpy as np
from psycopg.types.json import Jsonb

from app import db

# Facets are matched against keys in `attrs`; anything else is rejected so
# a key name can never reach the SQL as anything but a bound parameter.
FACETS = ("color", "fabric", "border", "work")


def design_key(company: str, product: str) -> tuple[str, str]:
    """How a photo is matched to bill lines: trimmed, case-folded names."""
    return company.strip().upper(), product.strip().upper()


def insert_image(
    company: str, product: str, image_ref: str, embedding: np.ndarray, attrs: dict | None
) -> int:
    """Add one catalog photo. Always a new row: a design can have many photos."""
    with db._connect() as conn:
        return conn.execute(
            "INSERT INTO product_image (company, product, image_ref, embedding, attrs) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (company, product, image_ref, embedding, Jsonb(attrs) if attrs is not None else None),
        ).fetchone()[0]


def nearest(embedding: np.ndarray, limit: int, facets: dict[str, str] | None = None) -> list[dict]:
    """The `limit` catalog photos closest to `embedding`, best first.

    Score is cosine similarity (1 - cosine distance). No threshold here: a
    `WHERE distance < x` would stop Postgres using the HNSW index, so the
    caller filters after the LIMIT instead.
    """
    clauses = ["embedding IS NOT NULL"]
    params: list = []
    for key, value in (facets or {}).items():
        if key not in FACETS:
            raise ValueError(f"unknown facet: {key}")
        clauses.append("lower(attrs->>%s) = lower(%s)")
        params.extend([key, value])

    with db._connect() as conn:
        # LOCAL, so it ends with this transaction — required behind a
        # transaction-mode pooler, where a plain SET would leak to whichever
        # client gets the server connection next.
        conn.execute("SET LOCAL hnsw.ef_search = 100")
        rows = conn.execute(
            f"""
            SELECT id, company, product, image_ref, 1 - (embedding <=> %s) AS score
            FROM product_image
            WHERE {' AND '.join(clauses)}
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            [embedding, *params, embedding, limit],
        ).fetchall()

    return [
        {"id": id_, "company": company, "product": product, "image_ref": ref, "score": float(score)}
        for id_, company, product, ref, score in rows
    ]


def price_history(designs: list[tuple[str, str]]) -> dict[tuple[str, str], list[dict]]:
    """Every bill line for each design, newest first, keyed by design_key().

    One query for all designs. Matched on names only, not supplier, so two
    suppliers selling the same company/product share a history — each entry
    still says which supplier it came from.
    """
    if not designs:
        return {}
    keys = [design_key(c, p) for c, p in designs]

    with db._connect() as conn:
        rows = conn.execute(
            """
            SELECT upper(btrim(company.name)), upper(btrim(line.product)),
                   supplier.name, bill.bill_no, bill.bill_date,
                   line.pcs, line.rate, line.final_price
            FROM line
            JOIN bill ON line.bill_id = bill.id
            JOIN company ON line.company_id = company.id
            JOIN supplier ON bill.supplier_id = supplier.id
            WHERE (upper(btrim(company.name)), upper(btrim(line.product)))
                  IN (SELECT * FROM unnest(%s::text[], %s::text[]))
            ORDER BY bill.bill_date DESC, bill.id DESC, line.line_order
            """,
            ([k[0] for k in keys], [k[1] for k in keys]),
        ).fetchall()

    history: dict[tuple[str, str], list[dict]] = {key: [] for key in keys}
    for company, product, supplier, bill_no, bill_date, pcs, rate, final_price in rows:
        history[(company, product)].append(
            {
                "supplier": supplier,
                "bill_no": bill_no,
                "bill_date": bill_date,
                "pcs": pcs,
                "rate": rate,
                "final_price": final_price,
            }
        )
    return history
