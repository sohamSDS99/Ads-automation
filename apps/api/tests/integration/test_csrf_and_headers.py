"""CSRF double-submit and the security headers."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Workspace
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient
from tests.integration.conftest import Workspace as Ws


async def test_a_post_without_the_header_is_refused(admin: ApiClient, db: AsyncSession) -> None:
    """ACCEPTANCE: POST without X-CSRF-Token returns 403."""
    response = await admin.raw.patch("/api/v1/workspace", json={"name": "Hijacked"})
    assert response.status_code == 403
    assert response.json()["type"] == "/problems/csrf-token-invalid"

    name = (await db.execute(sa.select(Workspace.name))).scalar_one()
    assert name != "Hijacked"


async def test_a_post_with_the_wrong_token_is_refused(admin: ApiClient) -> None:
    response = await admin.raw.patch(
        "/api/v1/workspace",
        json={"name": "Hijacked"},
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert response.status_code == 403
    assert response.json()["type"] == "/problems/csrf-token-invalid"


async def test_a_stale_cookie_cannot_be_replayed_against_a_session(
    admin: ApiClient, workspace: Ws
) -> None:
    """With a session, the session's token is the authority — not whatever cookie arrived."""
    forged = "forged-csrf-value"
    admin.raw.cookies.set("csrf", forged, domain="testserver")
    response = await admin.raw.patch(
        "/api/v1/workspace", json={"name": "Hijacked"}, headers={"X-CSRF-Token": forged}
    )
    assert response.status_code == 403


async def test_a_get_needs_no_token(admin: ApiClient) -> None:
    """SSE arrives as a GET and cannot send a header, so GET must stay exempt."""
    assert (await admin.raw.get("/api/v1/auth/me")).status_code == 200


async def test_an_anonymous_visitor_is_given_a_token_to_sign_in_with(
    client: ApiClient, workspace: Ws
) -> None:
    """The login page has no session yet, so it primes the cookie from /auth/csrf."""
    primed = await client.raw.get("/api/v1/auth/csrf")
    assert primed.status_code == 200
    token = primed.json()["csrf_token"]
    assert client.raw.cookies.get("csrf") == token

    signed_in = await client.raw.post(
        "/api/v1/auth/login",
        json={"email": workspace.admin_email, "password": ADMIN_PASSWORD},
        headers={"X-CSRF-Token": token},
    )
    assert signed_in.status_code == 200


async def test_signing_in_rotates_the_csrf_token(client: ApiClient, workspace: Ws) -> None:
    """A token minted before sign-in must not still work after it."""
    before = (await client.raw.get("/api/v1/auth/csrf")).json()["csrf_token"]
    await client.login(workspace.admin_email, ADMIN_PASSWORD)
    after = client.raw.cookies.get("csrf")
    assert after and after != before


async def test_every_response_carries_the_security_headers(client: ApiClient) -> None:
    response = await client.raw.get("/api/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_hsts_is_absent_over_plain_http(client: ApiClient) -> None:
    """Pinning HSTS from an http:// origin poisons the browser's cache for localhost."""
    response = await client.raw.get("/api/v1/health")
    assert "strict-transport-security" not in response.headers


async def test_hsts_is_present_when_cookies_are_secure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from agent.config import get_settings
    from agent.main import create_app

    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/api/v1/health")
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")
    get_settings.cache_clear()


async def test_a_csrf_failure_still_gets_the_security_headers(admin: ApiClient) -> None:
    """The headers middleware wraps the session layer, not the other way round."""
    response = await admin.raw.patch("/api/v1/workspace", json={"name": "x"})
    assert response.status_code == 403
    assert response.headers["x-frame-options"] == "DENY"
