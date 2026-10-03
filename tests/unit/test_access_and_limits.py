"""The access gates and upload bounds — no database needed.

These guard the change that made the API private. Before it, the whole book
of record was readable by anyone with the URL, and /ingest would accept a
write from them too.
"""

import pytest
from fastapi import HTTPException

from app import auth, config
from app.config import _api_key
from app.routers.bills import _validate_batch, _validate_image

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Upload:
    """Stand-in for UploadFile. `size` mirrors the real one: multipart sets it,
    but it is None when the part declared no length."""

    filename = "bill.jpg"

    def __init__(self, size: int | None = 1024):
        self.size = size


# --- The app-access gate --------------------------------------------------

def test_unconfigured_app_password_refuses_every_request(monkeypatch):
    """Fails closed. An unconfigured service returning 503 is a far better
    failure than one silently serving the whole book to the internet."""
    monkeypatch.setattr(auth.settings, "app_password", "")
    with pytest.raises(HTTPException) as exc:
        auth.require_app_access("anything")
    assert exc.value.status_code == 503


def test_empty_header_cannot_match_empty_configured_password(monkeypatch):
    monkeypatch.setattr(auth.settings, "app_password", "")
    with pytest.raises(HTTPException) as exc:
        auth.require_app_access("")
    assert exc.value.status_code == 503


@pytest.mark.parametrize("sent", [None, "", "wrong", "secret ", " secret"])
def test_missing_or_wrong_app_password_is_401(monkeypatch, sent):
    monkeypatch.setattr(auth.settings, "app_password", "secret")
    with pytest.raises(HTTPException) as exc:
        auth.require_app_access(sent)
    assert exc.value.status_code == 401


def test_correct_app_password_passes(monkeypatch):
    monkeypatch.setattr(auth.settings, "app_password", "secret")
    assert auth.require_app_access("secret") is None


# --- The admin gate keeps base price separate -----------------------------

def test_no_admin_header_is_not_an_error_just_not_admin(monkeypatch):
    """Staff search without the admin header; they get results, minus `rate`."""
    monkeypatch.setattr(auth.settings, "admin_password", "owner-pw")
    assert auth.admin_access(None) is False


def test_wrong_admin_password_is_401_not_a_silent_downgrade(monkeypatch):
    """Otherwise a typo'd password looks like "no base price data"."""
    monkeypatch.setattr(auth.settings, "admin_password", "owner-pw")
    with pytest.raises(HTTPException) as exc:
        auth.admin_access("typo")
    assert exc.value.status_code == 401


def test_admin_disabled_entirely_when_unset(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_password", "")
    with pytest.raises(HTTPException):
        auth.admin_access("")


def test_correct_admin_password_grants_base_price(monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_password", "owner-pw")
    assert auth.admin_access("owner-pw") is True


# --- Upload bounds --------------------------------------------------------

def test_empty_upload_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_batch([])
    assert exc.value.status_code == 400


def test_batch_over_the_file_count_cap_is_rejected(monkeypatch):
    """Each image costs an OCR call and is extracted serially at 10-20s, so an
    unbounded batch is both a cost and a request-timeout problem."""
    from app.routers import bills

    monkeypatch.setattr(bills.settings, "max_upload_files", 3)
    _validate_batch([Upload() for _ in range(3)])
    with pytest.raises(HTTPException) as exc:
        _validate_batch([Upload() for _ in range(4)])
    assert exc.value.status_code == 400


def test_batch_over_the_total_body_cap_is_rejected_before_any_ocr(monkeypatch):
    """Vercel rejects a request body over 4.5MB at the platform level, before
    any of this code runs. Catching it here turns an opaque 413 into a message
    naming the actual size. Two 2.7MB phone photos are over the line."""
    from app.routers import bills

    monkeypatch.setattr(bills.settings, "max_upload_files", 3)
    monkeypatch.setattr(bills.settings, "max_request_bytes", 4 * 1024 * 1024)

    one_photo = Upload(size=2_700_000)
    _validate_batch([one_photo])  # 2.7MB — fine

    with pytest.raises(HTTPException) as exc:
        _validate_batch([Upload(size=2_700_000), Upload(size=2_500_000)])
    assert exc.value.status_code == 413
    assert "5.0MB" in exc.value.detail


def test_undeclared_part_sizes_do_not_bypass_the_total_cap(monkeypatch):
    """size is None when multipart declared no length, so _validate_batch
    cannot check the total — the cumulative read in _extract_batch is what
    catches it, and this asserts the up-front check at least lets it through
    rather than erroring on None."""
    from app.routers import bills

    monkeypatch.setattr(bills.settings, "max_upload_files", 3)
    _validate_batch([Upload(size=None), Upload(size=None)])


def test_oversized_image_is_413(monkeypatch):
    from app.routers import bills

    monkeypatch.setattr(bills.settings, "max_upload_bytes", 1024)
    with pytest.raises(HTTPException) as exc:
        _validate_image("big.jpg", JPEG + b"\x00" * 2048)
    assert exc.value.status_code == 413


@pytest.mark.parametrize("payload", [b"not an image at all", b"<html>", b"%PDF-1.4", b""])
def test_non_image_payloads_are_rejected(payload):
    """file.content_type is whatever the client chose to send, so the bytes
    are what gets checked."""
    with pytest.raises(HTTPException) as exc:
        _validate_image("evil.jpg", payload)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("payload", [JPEG, PNG, b"RIFF....WEBP" + b"\x00" * 32])
def test_real_image_headers_pass(payload):
    _validate_image("bill.jpg", payload)


# --- Config parsing ------------------------------------------------------
#
# Tested through the helpers rather than by reloading app.config: a reload
# builds a NEW settings object while app.auth, app.main and app.routers.bills
# still hold the old one, so every later test in the session silently reads
# different settings than the code under test. That cost an afternoon once.

def test_cors_has_no_wildcard_default(monkeypatch):
    """"*" let any site on the internet call this API, and is also invalid
    alongside credentialed requests, so browsers reject it."""
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert config._origins("CORS_ORIGINS") == []


def test_cors_origins_are_split_and_trimmed(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", " https://a.com , ,https://b.com ")
    assert config._origins("CORS_ORIGINS") == ["https://a.com", "https://b.com"]


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("TRUE", True), (" true ", True),
    ("false", False), ("1", False), ("yes", False), ("", False),
])
def test_flags_are_only_true_for_true(monkeypatch, value, expected):
    monkeypatch.setenv("SOME_FLAG", value)
    assert config._flag("SOME_FLAG") is expected


def test_docs_are_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    assert config._flag("ENABLE_DOCS") is False


def test_schema_creation_defaults_on(monkeypatch):
    """Right for local dev and a first deploy; turn off once the schema exists
    so serverless cold starts stop re-running CREATE TABLE."""
    monkeypatch.delenv("DB_AUTO_INIT", raising=False)
    assert config._flag("DB_AUTO_INIT", True) is True
