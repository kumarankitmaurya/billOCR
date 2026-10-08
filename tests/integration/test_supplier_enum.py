"""Supplier is a closed set.

It used to be free text that ingest would get-or-create, and because supplier
is the workbook key, one supplier typed two ways became two ledgers that no
price search could join. The migrated book arrived holding "Dindayal Jalan"
and "Dindayal Jalan Textiles Pvt.Ltd" with two copies of the same bill.
"""

from .conftest import ADMIN_PASSWORD, make_result

ADMIN = {"X-Admin-Password": ADMIN_PASSWORD}


def add(client, name):
    return client.post("/api/bills/suppliers", json={"name": name}, headers=ADMIN)


def test_ingest_refuses_a_supplier_that_is_not_in_the_set(client, owner):
    """The whole point: a new spelling is an error, not a new supplier."""
    add(client, "Dindayal Jalan")
    response = owner.post(
        "/api/bills/ingest",
        json={"results": [make_result(supplier="Dindayal Jalan Textiles Pvt.Ltd")]},
    )
    assert response.status_code == 422
    assert "Unknown supplier" in response.json()["detail"]


def test_the_error_names_the_valid_choices(client, owner):
    add(client, "Dindayal Jalan")
    detail = owner.post(
        "/api/bills/ingest", json={"results": [make_result(supplier="Typo Traders")]}
    ).json()["detail"]
    assert "Dindayal Jalan" in detail


def test_a_known_supplier_still_ingests(client, owner):
    add(client, "Dindayal Jalan")
    response = owner.post("/api/bills/ingest", json={"results": [make_result()]})
    assert response.status_code == 200
    assert response.json()["suppliers"] == ["Dindayal Jalan"]


def test_nothing_is_written_when_one_bill_in_a_batch_is_unknown(client, owner):
    """Validated for the whole batch up front. Per-bill checking would commit
    the earlier bills before hitting the bad one — the same half-written-then-
    rejected shape /extract used to have."""
    add(client, "Dindayal Jalan")
    response = owner.post(
        "/api/bills/ingest",
        json={
            "results": [
                make_result(bill_no="GOOD-1"),
                make_result(bill_no="BAD-1", supplier="Not A Supplier"),
            ],
            # No override, so each bill's own supplier is used.
        },
    )
    assert response.status_code == 422
    assert client.get("/api/bills/search", params={"name": "GREEN"}).json() == []


def test_the_set_is_listed_for_the_frontend(client):
    """Served from the database so the separately-deployed UI cannot drift
    from what ingest will actually accept."""
    add(client, "B Traders")
    add(client, "A Traders")
    assert client.get("/api/bills/suppliers").json() == ["A Traders", "B Traders"]


def test_adding_a_supplier_needs_admin(client):
    """Adding one changes the shape of the book, so it is the owner's call —
    and making it implicit is what produced the duplicates."""
    assert client.post("/api/bills/suppliers", json={"name": "Nope"}).status_code == 401
    assert "Nope" not in client.get("/api/bills/suppliers").json()


def test_adding_the_same_supplier_twice_is_harmless(client):
    assert add(client, "Twice Traders").json()["created"] is True
    assert add(client, "Twice Traders").json()["created"] is False


def test_a_blank_supplier_name_is_refused(client):
    assert add(client, "   ").status_code == 422


def test_preview_refuses_an_unknown_supplier_before_spending_an_ocr_call(client):
    """An OCR call costs money and 20 seconds; finding out at save time that
    the supplier was wrong wastes both."""
    add(client, "Dindayal Jalan")
    response = client.post(
        "/api/bills/preview",
        data={"supplier": "Who Dis"},
        files=[("files", ("bill.jpg", b"\xff\xd8\xff" + b"0" * 64, "image/jpeg"))],
    )
    assert response.status_code == 422
    assert "Unknown supplier" in response.json()["detail"]
