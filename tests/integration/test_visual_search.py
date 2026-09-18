"""Visual search over HTTP: real model, real Postgres+pgvector, local image store.

Two kinds of test:

* Mechanics — auth, error codes, grouping, `rate` gating, the model-mismatch
  503. These use generated patterned images, which is enough to check the
  plumbing but says nothing about how well sarees are recognised.

* Retrieval quality — `test_real_sarees_match_their_own_design`, which needs
  real photos in tests/fixtures/sarees/ (see that test's docstring) and
  skips without them. It prints the score matrix the threshold is tuned from.
"""

import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageEnhance

from .conftest import ADMIN_PASSWORD, ROOT, VISUAL_AVAILABLE, make_result

pytestmark = [
    pytest.mark.visual,
    pytest.mark.skipif(not VISUAL_AVAILABLE, reason="model not fetched: python scripts/fetch_model.py --dest ./models"),
]

ADMIN = {"X-Admin-Password": ADMIN_PASSWORD}
BASE = "/api/bills/visual-search"
FIXTURES = ROOT / "tests" / "fixtures" / "sarees"


def _jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _pattern(seed: int) -> Image.Image:
    """A deterministic, recognisably distinct 'fabric' per seed.

    Structured motifs rather than random noise: the model sees every
    random-dot image as roughly the same thing (~0.95 apart), whereas these
    measure ~0.90-0.95 against their own reshoot and <0.75 against each other.
    """
    image = Image.new("RGB", (480, 720))
    draw = ImageDraw.Draw(image)
    kind = seed % 4
    if kind == 1:  # checks
        draw.rectangle([0, 0, 480, 720], fill=(200, 20, 40))
        for i in range(8):
            for j in range(12):
                if (i + j) % 2:
                    draw.rectangle([i * 60, j * 60, i * 60 + 60, j * 60 + 60], fill=(250, 220, 60))
    elif kind == 2:  # horizontal stripes
        draw.rectangle([0, 0, 480, 720], fill=(20, 40, 160))
        for y in range(0, 720, 60):
            draw.rectangle([0, y, 480, y + 25], fill=(240, 240, 240))
    elif kind == 3:  # plain body, temple border
        draw.rectangle([0, 0, 480, 720], fill=(20, 120, 60))
        draw.rectangle([0, 600, 480, 720], fill=(210, 170, 40))
        for x in range(0, 480, 40):
            draw.polygon([(x, 600), (x + 20, 570), (x + 40, 600)], fill=(210, 170, 40))
    else:  # rings
        draw.rectangle([0, 0, 480, 720], fill=(250, 240, 220))
        for x in range(0, 480, 120):
            for y in range(0, 720, 120):
                draw.ellipse([x + 10, y + 10, x + 110, y + 110], outline=(120, 20, 120), width=12)
    return image


def _reshoot(image: Image.Image) -> Image.Image:
    """The same 'fabric' photographed again: cropped, tilted, a bit darker."""
    w, h = image.size
    image = image.crop((int(w * 0.06), int(h * 0.04), int(w * 0.95), int(h * 0.97))).rotate(3, expand=True)
    return ImageEnhance.Brightness(image).enhance(0.88)


def _ingest(company, product, *, rate=500.0, final_price=640.0, bill_no="B-1", bill_date="2025-08-01", client):
    article = {"company": company, "product": product, "pcs": 2, "rate": rate,
               "tax_pct": None, "margin_pct": None, "final_price": final_price}
    response = client.post(
        "/api/bills/ingest",
        json={"results": [make_result(bill_no=bill_no, bill_date=bill_date, articles=[article])]},
    )
    assert response.status_code == 200, response.text


def _index(client, company, product, *images, headers=ADMIN):
    files = [("files", (f"p{i}.jpg", _jpeg(img) if isinstance(img, Image.Image) else img, "image/jpeg"))
             for i, img in enumerate(images)]
    return client.post(f"{BASE}/index", data={"company": company, "product": product},
                       files=files, headers=headers)


def _query(client, image, headers=None, **form):
    data = _jpeg(image) if isinstance(image, Image.Image) else image
    return client.post(f"{BASE}/query", files={"file": ("q.jpg", data, "image/jpeg")},
                       data={k: str(v) for k, v in form.items()}, headers=headers or {})


def test_status_reports_ready(client):
    body = client.get(f"{BASE}/status").json()
    assert body["enabled"] is True and body["ready"] is True
    assert body["dim"] == 768
    assert body["model"].startswith("Marqo/marqo-fashionSigLIP@")


def test_indexing_is_admin_only(client):
    assert _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1), headers={}).status_code == 401
    assert _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1),
                  headers={"X-Admin-Password": "nope"}).status_code == 401


def test_index_validates_input(client):
    assert _index(client, "  ", "MILK CAKE", _pattern(1)).status_code == 422
    assert _index(client, "ROHAN", "MILK", b"not an image").status_code == 422
    assert _query(client, b"not an image").status_code == 422
    assert _query(client, _pattern(1), k=0).status_code == 422


def test_oversized_upload_is_413(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "visual_upload_max_bytes", 1000)
    assert _query(client, _pattern(1)).status_code == 413


def test_index_flags_designs_with_no_bills(client):
    _ingest("ROHAN FAB SRT", "MILK CAKE", client=client)

    known = _index(client, "rohan fab srt ", "Milk Cake", _pattern(1)).json()
    assert known["has_price_history"] is True, "matching should ignore case and outer spaces"
    assert len(known["indexed"]) == 1

    typo = _index(client, "ROHAN FAB SRT", "MILK CAKEE", _pattern(2)).json()
    assert typo["has_price_history"] is False

    image = client.get(known["indexed"][0]["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"


def test_query_finds_design_with_price_history_and_gates_rate(client):
    _ingest("ROHAN FAB SRT", "MILK CAKE", rate=500.0, final_price=640.0, bill_no="B-1",
            bill_date="2025-07-01", client=client)
    _ingest("ROHAN FAB SRT", "MILK CAKE", rate=520.0, final_price=None, bill_no="B-2",
            bill_date="2025-08-01", client=client)
    _ingest("JAI MATA DI SRT", "GREEN TEA", client=client, bill_no="B-3")

    milk = _pattern(1)
    _index(client, "ROHAN FAB SRT", "MILK CAKE", milk, _pattern(1).rotate(180))
    _index(client, "JAI MATA DI SRT", "GREEN TEA", _pattern(2))

    public = _query(client, _reshoot(milk)).json()
    assert public["status"] == "match", public
    top = public["results"][0]
    assert (top["company"], top["product"]) == ("ROHAN FAB SRT", "MILK CAKE")
    assert top["score"] >= public["threshold"]

    # Two photos of one design come back as one card.
    products = [(r["company"], r["product"]) for r in public["results"]]
    assert len(products) == len(set(products))

    # Newest non-null final price, not simply the newest bill.
    assert top["latest_final_price"] == 640.0
    assert top["latest_final_price_date"] == "2025-07-01"
    assert (top["first_seen"], top["last_seen"]) == ("2025-07-01", "2025-08-01")
    assert [h["bill_no"] for h in top["history"]] == ["B-2", "B-1"]

    assert "latest_rate" not in top
    assert all("rate" not in h for h in top["history"]), "rate must be absent, not null"

    admin = _query(client, _reshoot(milk), headers=ADMIN).json()["results"][0]
    assert admin["latest_rate"] == 520.0
    assert [h["rate"] for h in admin["history"]] == [520.0, 500.0]

    assert _query(client, _reshoot(milk), headers={"X-Admin-Password": "nope"}).status_code == 401


def test_unrelated_photo_is_no_confident_match(client):
    _ingest("ROHAN FAB SRT", "MILK CAKE", client=client)
    _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1))

    body = _query(client, (ROOT / "preview.jpeg").read_bytes()).json()
    assert body["status"] == "no_confident_match", body
    assert body["results"] == []
    assert body["best_score"] is not None and body["best_score"] < body["threshold"]


def test_empty_catalog_is_no_confident_match(client):
    body = _query(client, _pattern(1)).json()
    assert body == {**body, "status": "no_confident_match", "best_score": None, "results": []}


def test_model_mismatch_blocks_index_and_query(client):
    from app import db

    _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1))
    with db._connect() as conn:
        conn.execute("UPDATE visual_search_meta SET model = 'some/other-model@abc:fp32'")
    client.app.state.visual.state.check(force=True)

    assert _query(client, _pattern(1)).status_code == 503
    blocked = _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1))
    assert blocked.status_code == 503
    assert "reembed_catalog" in blocked.json()["detail"]

    status = client.get(f"{BASE}/status").json()
    assert status["ready"] is False and "some/other-model" in status["reason"]


def test_real_sarees_match_their_own_design(client):
    """Retrieval quality on real photos. Needs, per design:

        tests/fixtures/sarees/<slug>/index.jpg   the catalog photo
        tests/fixtures/sarees/<slug>/query.jpg   a fresh photo of the same saree
        tests/fixtures/sarees/manifest.json      {"<slug>": {"company": ..., "product": ...}}

    3-5 designs is enough to start. Prints true-match vs best-wrong-match
    scores; set VISUAL_MATCH_THRESHOLD between the two columns.
    """
    manifest_path = FIXTURES / "manifest.json"
    if not manifest_path.is_file():
        pytest.skip(f"no real saree photos in {FIXTURES} — see this test's docstring")
    manifest = json.loads(manifest_path.read_text())

    for n, (slug, design) in enumerate(manifest.items()):
        _ingest(design["company"], design["product"], bill_no=f"R-{n}", client=client)
        response = _index(client, design["company"], design["product"], (FIXTURES / slug / "index.jpg").read_bytes())
        assert response.status_code == 200, response.text

    rows, failures = [], []
    for slug, design in manifest.items():
        body = _query(client, (FIXTURES / slug / "query.jpg").read_bytes(), k=20).json()
        want = (design["company"].strip().upper(), design["product"].strip().upper())
        # Read raw scores from the index rather than the thresholded results.
        from app.services.visual_search import repo

        visual = client.app.state.visual
        from app.services.visual_search.embedder import load_image

        vector = visual.embedder.embed(load_image((FIXTURES / slug / "query.jpg").read_bytes(), 10**9))
        scores: dict[tuple[str, str], float] = {}
        for hit in repo.nearest(vector, 64):
            key = repo.design_key(hit["company"], hit["product"])
            scores[key] = max(scores.get(key, -1.0), hit["score"])
        true_score = scores.get(want, float("nan"))
        wrong = max((s for k, s in scores.items() if k != want), default=float("nan"))
        rows.append((slug, true_score, wrong))

        top = body["results"][0] if body["results"] else None
        if not top or (top["company"].strip().upper(), top["product"].strip().upper()) != want:
            failures.append(slug)

    print("\n  design                      true    best-wrong   margin")
    for slug, true_score, wrong in rows:
        print(f"  {slug:<26} {true_score:6.4f}   {wrong:6.4f}    {true_score - wrong:+.4f}")

    assert not failures, f"not ranked first (or below threshold): {failures}"

    # The bill photo is not a saree.
    body = _query(client, (ROOT / "preview.jpeg").read_bytes()).json()
    assert body["status"] == "no_confident_match", body


def test_reembed_script_recovers_from_a_model_mismatch(client):
    import os
    import subprocess
    import sys

    from app import db

    _ingest("ROHAN FAB SRT", "MILK CAKE", client=client)
    _index(client, "ROHAN FAB SRT", "MILK CAKE", _pattern(1))
    with db._connect() as conn:
        conn.execute("UPDATE visual_search_meta SET model = 'some/other-model@abc:fp32'")
    assert client.app.state.visual.state.check(force=True) is False

    script = ROOT / "scripts" / "reembed_catalog.py"
    assert subprocess.run([sys.executable, str(script)], capture_output=True).returncode == 1, \
        "must refuse without --confirm"
    result = subprocess.run([sys.executable, str(script), "--confirm"],
                            capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 embedded, 0 failed" in result.stdout

    assert client.app.state.visual.state.check(force=True) is True
    body = _query(client, _reshoot(_pattern(1))).json()
    assert body["status"] == "match" and body["results"][0]["product"] == "MILK CAKE"
