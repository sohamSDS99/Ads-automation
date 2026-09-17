"""The key vault through the API.

The property every test here circles is the same one: a secret goes in and
never comes back out. The others are about scope — who may write a shared
credential, and who may see a personal one.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.credentials import open_credential
from agent.db.models import AuditLog, Credential, CredentialKind, CredentialScope
from agent.llm.openrouter import KeyStatus, OpenRouterError
from tests.integration.conftest import ApiClient

OPENROUTER_KEY = "sk-or-v1-0123456789abcdef"
DATAFORSEO = {"api_key": "ops@example.com:hunter2-not-real"}
GOOGLE_ADS_VALUES = {
    "developer_token": "dev-token-not-real",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "customer_id": "123-456-7890",
}


async def store(admin: ApiClient, **overrides: Any) -> dict[str, Any]:
    body = {"kind": "openrouter", "values": {"api_key": OPENROUTER_KEY}, **overrides}
    response = await admin.post("/credentials", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_stored_credential_reports_only_a_hint(admin: ApiClient) -> None:
    stored = await store(admin)

    assert stored["meta"] == {"last4": OPENROUTER_KEY[-4:]}
    assert OPENROUTER_KEY not in json.dumps(stored)


async def test_no_response_anywhere_carries_the_secret(admin: ApiClient) -> None:
    await store(admin)
    listing = await admin.get("/credentials")

    assert listing.status_code == 200
    body = listing.text
    assert OPENROUTER_KEY not in body
    for forbidden in ("ciphertext", "nonce"):
        assert forbidden not in body


async def test_the_ciphertext_in_the_database_decrypts_to_what_was_sent(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The seal has to be reversible by the run path, or nothing can spend the key."""
    stored = await store(admin)
    row = (
        await db.execute(sa.select(Credential).where(Credential.id == uuid.UUID(stored["id"])))
    ).scalar_one()

    assert open_credential(row) == OPENROUTER_KEY


async def test_a_multi_field_credential_seals_as_json(admin: ApiClient, db: AsyncSession) -> None:
    """`google_ads` is the only kind that still holds several values.

    It is not several *typed* values — consent supplies five of the six — but
    the sealed secret is still a JSON object, and `gather._credentials` reads it
    back as one.
    """
    stored = await store(admin, kind="google_ads", values=GOOGLE_ADS_VALUES)
    row = (
        await db.execute(sa.select(Credential).where(Credential.id == uuid.UUID(stored["id"])))
    ).scalar_one()

    assert json.loads(open_credential(row)) == GOOGLE_ADS_VALUES
    # The customer id is an account id, not a secret, so it is showable. The
    # developer token is not, and only its last four characters survive.
    assert stored["meta"]["customer_id"] == GOOGLE_ADS_VALUES["customer_id"]
    assert GOOGLE_ADS_VALUES["refresh_token"] not in json.dumps(stored)


async def test_a_one_key_credential_seals_the_bare_value(
    admin: ApiClient, db: AsyncSession
) -> None:
    """And the three source kinds that a person types are all one key now."""
    stored = await store(admin, kind="dataforseo", values=DATAFORSEO)
    row = (
        await db.execute(sa.select(Credential).where(Credential.id == uuid.UUID(stored["id"])))
    ).scalar_one()

    assert open_credential(row) == DATAFORSEO["api_key"], "no JSON envelope around one value"
    assert stored["meta"] == {"last4": "real"}
    assert DATAFORSEO["api_key"] not in json.dumps(stored)


async def test_the_catalogue_of_kinds_travels_with_the_list(admin: ApiClient) -> None:
    """So the browser never hardcodes which fields a Google Ads credential needs."""
    body = (await admin.get("/credentials")).json()
    kinds = {kind["kind"]: kind for kind in body["kinds"]}

    assert set(kinds) == {"openrouter", "google_ads", "dataforseo", "brightdata"}
    google = {field["name"] for field in kinds["google_ads"]["fields"]}
    assert {"developer_token", "refresh_token", "customer_id"} <= google
    assert kinds["google_ads"]["fields"][0]["secret"] is True
    # Every field but the developer token arrives from consent, so the form has
    # one box. This is the assertion the browser's form derives itself from.
    typed = google - set(kinds["google_ads"]["oauth_fields"])
    assert typed == {"developer_token"}

    # And every other kind is a single key, with nothing showable beside it.
    for kind in ("openrouter", "brightdata", "dataforseo"):
        fields = kinds[kind]["fields"]
        assert [field["name"] for field in fields] == ["api_key"], kind
        assert fields[0]["secret"] is True, kind
        assert fields[0]["required"] is True, kind


async def test_smtp_cannot_be_stored_as_a_credential(admin: ApiClient) -> None:
    """It is deployment configuration, not workspace state (PRD §18 law 9)."""
    response = await admin.post(
        "/credentials", json={"kind": "smtp", "values": {"host": "smtp.example.com"}}
    )
    assert response.status_code == 422


async def test_a_missing_required_field_names_the_field(admin: ApiClient) -> None:
    response = await admin.post(
        "/credentials", json={"kind": "dataforseo", "values": {"nonsense": "x"}}
    )
    assert response.status_code == 422
    assert "api_key" in response.json()["detail"]


# --- who may write what -----------------------------------------------------


async def test_an_operator_cannot_store_a_workspace_credential(signed_in_as: Any) -> None:
    operator = await signed_in_as("operator")
    response = await operator.post(
        "/credentials", json={"kind": "openrouter", "values": {"api_key": OPENROUTER_KEY}}
    )
    assert response.status_code == 403
    assert response.json()["missing_permission"] == "credential_write"


async def test_anyone_may_store_their_own_personal_key(signed_in_as: Any) -> None:
    """PRD §13.4 H: a personal override is the caller's own secret and own spend."""
    viewer = await signed_in_as("viewer")
    response = await viewer.post(
        "/credentials",
        json={"kind": "openrouter", "scope": "user", "values": {"api_key": "sk-or-personal"}},
    )
    assert response.status_code == 201, response.text
    assert response.json()["scope"] == "user"


async def test_a_personal_key_is_written_for_the_caller_and_nobody_else(
    signed_in_as: Any, db: AsyncSession
) -> None:
    viewer = await signed_in_as("viewer")
    me = (await viewer.get("/auth/me")).json()
    stored = (
        await viewer.post(
            "/credentials",
            json={"kind": "openrouter", "scope": "user", "values": {"api_key": "sk-or-personal"}},
        )
    ).json()

    assert stored["user_id"] == me["id"]


async def test_one_persons_key_is_invisible_to_another(admin: ApiClient, signed_in_as: Any) -> None:
    operator = await signed_in_as("operator")
    await operator.post(
        "/credentials",
        json={"kind": "openrouter", "scope": "user", "values": {"api_key": "sk-or-theirs"}},
    )

    listing = (await admin.get("/credentials")).json()
    assert [row for row in listing["credentials"] if row["scope"] == "user"] == []


async def test_another_persons_key_cannot_be_deleted(admin: ApiClient, signed_in_as: Any) -> None:
    """Not a 403 — whose personal key exists is not the caller's business."""
    operator = await signed_in_as("operator")
    theirs = (
        await operator.post(
            "/credentials",
            json={"kind": "openrouter", "scope": "user", "values": {"api_key": "sk-or-theirs"}},
        )
    ).json()

    response = await admin.delete(f"/credentials/{theirs['id']}")
    assert response.status_code == 404


async def test_a_project_credential_needs_a_real_project(admin: ApiClient) -> None:
    response = await admin.post(
        "/credentials",
        json={
            "kind": "openrouter",
            "scope": "project",
            "project_id": str(uuid.uuid4()),
            "values": {"api_key": OPENROUTER_KEY},
        },
    )
    assert response.status_code == 404


# --- delete -----------------------------------------------------------------


async def test_deleting_a_credential_removes_it_and_records_who(
    admin: ApiClient, db: AsyncSession
) -> None:
    stored = await store(admin)

    response = await admin.delete(f"/credentials/{stored['id']}")
    assert response.status_code == 204

    remaining = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Credential)
            .where(Credential.id == uuid.UUID(stored["id"]))
        )
    ).scalar_one()
    assert remaining == 0

    actions = (
        (
            await db.execute(
                sa.select(AuditLog.action).where(AuditLog.target_id == uuid.UUID(stored["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert "credential.created" in actions
    assert "credential.deleted" in actions


async def test_an_operator_cannot_delete_a_shared_credential(
    admin: ApiClient, signed_in_as: Any
) -> None:
    stored = await store(admin)
    operator = await signed_in_as("operator")

    response = await operator.delete(f"/credentials/{stored['id']}")
    assert response.status_code == 403


# --- test -------------------------------------------------------------------


async def test_a_rejected_key_is_a_200_with_ok_false(
    admin: ApiClient, db: AsyncSession, monkeypatch: Any
) -> None:
    """The call the user asked for succeeded; the answer is that the key is bad.

    A 4xx here would be indistinguishable from a malformed request, and the
    screen would have to guess which it was.

    The upstream is stubbed rather than called: an integration suite that
    depends on OpenRouter being reachable fails for reasons that have nothing to
    do with this code. How the real response is parsed is covered in
    `tests/test_openrouter_account.py` against a mock transport.
    """
    stored = await store(admin, values={"api_key": "sk-or-definitely-not-valid"})

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise OpenRouterError("OpenRouter rejected this key.")

    monkeypatch.setattr("agent.api.routes_credentials.probe_key", reject)

    response = await admin.post(f"/credentials/{stored['id']}/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["detail"] == "OpenRouter rejected this key."

    row = (
        await db.execute(sa.select(Credential).where(Credential.id == uuid.UUID(stored["id"])))
    ).scalar_one()
    await db.refresh(row)
    assert row.last_test_ok is False
    assert row.last_tested_at is not None


async def test_a_working_key_records_the_balance_as_a_hint(
    admin: ApiClient, db: AsyncSession, monkeypatch: Any
) -> None:
    stored = await store(admin)

    async def accept(*_args: Any, **_kwargs: Any) -> KeyStatus:
        return KeyStatus(
            label="ads-research",
            usage_usd=Decimal("4.25"),
            limit_usd=Decimal("50"),
            remaining_usd=Decimal("45.75"),
            is_free_tier=False,
        )

    monkeypatch.setattr("agent.api.routes_credentials.probe_key", accept)

    body = (await admin.post(f"/credentials/{stored['id']}/test")).json()
    assert body["ok"] is True
    assert body["meta"]["remaining_usd"] == "45.75"

    # The hint is merged into the row, so the settings screen can show it
    # without testing again — and the last4 from the write survives.
    listing = (await admin.get("/credentials")).json()
    row = next(item for item in listing["credentials"] if item["id"] == stored["id"])
    assert row["meta"]["remaining_usd"] == "45.75"
    assert row["meta"]["last4"] == OPENROUTER_KEY[-4:]
    assert OPENROUTER_KEY not in json.dumps(listing)


async def test_the_scopes_line_up_with_the_check_constraint(admin: ApiClient) -> None:
    """A workspace credential must carry neither a project nor a user."""
    stored = await store(admin)
    assert stored["scope"] == CredentialScope.WORKSPACE.value
    assert stored["project_id"] is None
    assert stored["user_id"] is None
    assert stored["kind"] == CredentialKind.OPENROUTER.value
