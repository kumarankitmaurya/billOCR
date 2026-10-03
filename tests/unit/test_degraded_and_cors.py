"""How the service behaves when the database is missing, and who may call it.

Both exist because of how the first Vercel deploy failed: an opaque platform
500 on every path, with the real cause only in build logs.
"""

import pytest
from starlette.testclient import TestClient


def client(monkeypatch, **env):
    """A TestClient with settings patched before the lifespan runs."""
    from app import config, main

    for key, value in env.items():
        monkeypatch.setattr(config.settings, key, value)
        monkeypatch.setattr(main.settings, key, value, raising=False)
    monkeypatch.setattr(main, "_startup_error", None)
    return TestClient(main.app)


# --- Degraded: configured with no database ---------------------------------

def test_db_backed_endpoint_returns_503_not_500(monkeypatch):
    """A 500 with no JSON body makes billOCR-ui show "Server returned 500",
    which sends the shopkeeper hunting for a problem with their photo."""
    with client(monkeypatch, database_url="", app_password="pw") as cl:
        response = cl.get("/api/bills/search", headers={"X-App-Password": "pw"})
    assert response.status_code == 503
    assert "database" in response.json()["detail"].lower()


def test_the_503_message_does_not_leak_connection_details(monkeypatch):
    with client(monkeypatch, database_url="", app_password="pw") as cl:
        detail = cl.get("/api/bills/search", headers={"X-App-Password": "pw"}).json()["detail"]
    for leaked in ("postgresql://", "neon.tech", "5432", "password authentication"):
        assert leaked not in detail


def test_providers_still_works_without_a_database(monkeypatch):
    """It touches no table, so the UI can still load its dropdown and check
    the password while the database is down."""
    with client(monkeypatch, database_url="", app_password="pw") as cl:
        response = cl.get("/api/bills/providers", headers={"X-App-Password": "pw"})
    assert response.status_code == 200


def test_the_gate_still_applies_while_degraded(monkeypatch):
    """A broken database must not become a way around the password."""
    with client(monkeypatch, database_url="", app_password="pw") as cl:
        assert cl.get("/api/bills/search").status_code == 401


# --- CORS ------------------------------------------------------------------
#
# Exercised against a throwaway app built from main.cors_kwargs(), because the
# real app wires its middleware once at import: patching settings afterwards
# changes nothing the middleware reads.

PREVIEW_REGEX = r"https://billocr-ui-[a-z0-9-]+\.vercel\.app"


def cors_client(monkeypatch, origins=None, regex=None):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    from app import config, main

    monkeypatch.setattr(config.settings, "cors_origins", origins or [])
    monkeypatch.setattr(config.settings, "cors_origin_regex", regex)

    probe = FastAPI()
    probe.add_middleware(CORSMiddleware, **main.cors_kwargs())

    @probe.get("/api/bills/providers")
    def _providers():
        return []

    return TestClient(probe)


def preflight(cl, origin):
    return cl.options(
        "/api/bills/providers",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-app-password",
        },
    ).headers.get("access-control-allow-origin")


@pytest.mark.parametrize("origin", [
    "https://billocr-ui-abc123-ankit.vercel.app",
    "https://billocr-ui-git-main-ankit.vercel.app",
])
def test_vercel_preview_origins_are_allowed_by_regex(monkeypatch, origin):
    """Every preview deployment gets its own hostname, so an exact origin list
    only ever matches production."""
    assert preflight(cors_client(monkeypatch, regex=PREVIEW_REGEX), origin) == origin


@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "https://billocr-ui-abc.vercel.app.evil.com",
    "https://some-other-app.vercel.app",
])
def test_unrelated_origins_are_refused(monkeypatch, origin):
    """Note the second case: a regex anchored loosely would match it."""
    assert preflight(cors_client(monkeypatch, regex=PREVIEW_REGEX), origin) is None


def test_the_custom_auth_header_is_allowed(monkeypatch):
    """X-App-Password is a non-standard header, so it needs a preflight pass."""
    cl = cors_client(monkeypatch, origins=["https://billocr-ui.vercel.app"])
    response = cl.options("/api/bills/providers", headers={
        "Origin": "https://billocr-ui.vercel.app",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-app-password",
    })
    assert "x-app-password" in response.headers.get("access-control-allow-headers", "").lower()


def test_no_cors_config_means_no_cross_origin_caller(monkeypatch):
    """The safe direction to fail — and correct when the frontend proxies
    /api/* through a rewrite, since the browser then sees one origin."""
    assert preflight(cors_client(monkeypatch), "https://billocr-ui.vercel.app") is None
