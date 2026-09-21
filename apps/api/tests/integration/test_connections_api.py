"""Connections through the API.

The property every test here circles: no secret crosses this boundary in either
direction. There is nothing to POST a key to, and nothing that reads one back —
what a workspace owns is the decision to use a source the deployment already
holds, and what the screen may see is the *names* of the variables plus a last
four of what a successful test found in them.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.credential_kinds import KIND_SPECS
from agent.db.models import AuditLog, CredentialKind, SourceConnection
from agent.llm.openrouter import KeyStatus, OpenRouterError
from tests.integration.conftest import ApiClient

#: What `conftest.stack_environment` puts in `OPENROUTER_API_KEY`. The model key
#: is the one source the suite's environment configures, which makes it the one
#: that can be connected and every other one a "not configured" case.
OPENROUTER_KEY = "sk-or-test-key"

GOOGLE_ADS_ENV = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token-not-real",
    "GOOGLE_ADS_CLIENT_ID": "client-id",
    "GOOGLE_ADS_CLIENT_SECRET": "client-secret",
    "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token",
    "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
}


@pytest.fixture
def working_key(monkeypatch: Any) -> None:
    """Stub the model surface so connecting does not depend on OpenRouter being up.

    An integration suite that reaches a vendor fails for reasons that have
    nothing to do with this code. How a real response is parsed is covered in
    `tests/test_openrouter_account.py` against a mock transport.
    """

    async def accept(*_args: Any, **_kwargs: Any) -> KeyStatus:
        return KeyStatus(
            label="ads-research",
            usage_usd=Decimal("4.25"),
            limit_usd=Decimal("50"),
            remaining_usd=Decimal("45.75"),
            is_free_tier=False,
        )

    monkeypatch.setattr("agent.api.routes_connections.probe_key", accept)


async def connect(admin: ApiClient, kind: str = "openrouter") -> dict[str, Any]:
    response = await admin.post(f"/connections/{kind}/connect")
    assert response.status_code == 200, response.text
    return response.json()


# --- the catalogue ----------------------------------------------------------


async def test_every_source_is_listed_whether_connected_or_not(admin: ApiClient) -> None:
    """One list, derived from the registry — a new source gets a card for free."""
    body = (await admin.get("/connections")).json()
    sources = {row["kind"]: row for row in body["sources"]}

    assert set(sources) == {kind.value for kind in KIND_SPECS}
    assert all(row["connected"] is False for row in sources.values())
    # Only the model surface stops a run; every other source thins the report.
    assert [kind for kind, row in sources.items() if row["required_for_runs"]] == ["openrouter"]


async def test_a_source_names_its_variables_and_never_their_values(admin: ApiClient) -> None:
    listing = await admin.get("/connections")
    body = listing.text
    sources = {row["kind"]: row for row in listing.json()["sources"]}

    assert sources["openrouter"]["env_vars"] == ["OPENROUTER_API_KEY"]
    assert sources["openrouter"]["configured"] is True
    assert sources["openrouter"]["missing_env_vars"] == []
    assert OPENROUTER_KEY not in body


async def test_an_unconfigured_source_names_exactly_what_to_set(admin: ApiClient) -> None:
    """The screen's whole instruction to the operator, computed not written."""
    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    google = sources["google_ads"]

    assert google["configured"] is False
    assert set(google["missing_env_vars"]) == set(GOOGLE_ADS_ENV)
    # The manager id is the one value that may stay unset, so it is offered but
    # never demanded.
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" in google["env_vars"]
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" not in google["missing_env_vars"]


# --- connect ----------------------------------------------------------------


async def test_connecting_asks_for_nothing_and_proves_itself(
    admin: ApiClient, db: AsyncSession, working_key: None
) -> None:
    """One click: no body, no form, and the verdict comes back with the decision."""
    stored = await connect(admin)

    assert stored["connected"] is True
    assert stored["last_test_ok"] is True
    assert stored["meta"]["remaining_usd"] == "45.75"
    assert OPENROUTER_KEY not in json.dumps(stored)

    row = (
        await db.execute(
            sa.select(SourceConnection).where(SourceConnection.kind == CredentialKind.OPENROUTER)
        )
    ).scalar_one()
    assert row.last_test_ok is True


async def test_a_failing_upstream_still_connects_and_says_why(
    admin: ApiClient, monkeypatch: Any
) -> None:
    """An upstream that is briefly down is not a reason to strand an administrator.

    The decision is recorded, the card reports "not working", and the sentence
    is the upstream's own — which is the difference between a screen that can
    be acted on and one that says "something went wrong".
    """

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise OpenRouterError("OpenRouter rejected this key.")

    monkeypatch.setattr("agent.api.routes_connections.probe_key", reject)

    stored = await connect(admin)
    assert stored["connected"] is True
    assert stored["last_test_ok"] is False
    assert stored["last_test_detail"] == "OpenRouter rejected this key."


async def test_an_unconfigured_source_cannot_be_connected(admin: ApiClient) -> None:
    """A card that says connected next to a run that skips the source is the lie
    this screen exists to stop telling."""
    response = await admin.post("/connections/google_ads/connect")

    assert response.status_code == 422
    body = response.json()
    assert set(body["missing_env_vars"]) == set(GOOGLE_ADS_ENV)
    assert "GOOGLE_ADS_DEVELOPER_TOKEN" in body["detail"]


async def test_connecting_twice_keeps_the_first_decision(
    admin: ApiClient, db: AsyncSession, working_key: None
) -> None:
    """Connecting an already-connected source re-tests it; it does not re-decide it."""
    first = await connect(admin)
    second = await connect(admin)

    assert second["connected_at"] == first["connected_at"]
    assert second["connected_by"] == first["connected_by"]

    rows = (await db.execute(sa.select(sa.func.count()).select_from(SourceConnection))).scalar_one()
    assert rows == 1


async def test_connecting_records_who_and_from_where(
    admin: ApiClient, db: AsyncSession, working_key: None
) -> None:
    stored = await connect(admin)

    assert stored["connected_by_name"]
    actions = (
        (await db.execute(sa.select(AuditLog.action).order_by(AuditLog.created_at))).scalars().all()
    )
    assert "source.connected" in actions
    assert "source.tested" not in actions, "connecting is one act, not two rows"


# --- disconnect -------------------------------------------------------------


async def test_disconnecting_removes_the_decision_and_nothing_else(
    admin: ApiClient, db: AsyncSession, working_key: None
) -> None:
    await connect(admin)

    assert (await admin.delete("/connections/openrouter")).status_code == 204

    rows = (await db.execute(sa.select(sa.func.count()).select_from(SourceConnection))).scalar_one()
    assert rows == 0
    # The deployment's key is untouched: it was never ours to delete.
    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    assert sources["openrouter"]["configured"] is True
    assert sources["openrouter"]["connected"] is False
    assert sources["openrouter"]["last_test_ok"] is None

    actions = (await db.execute(sa.select(AuditLog.action))).scalars().all()
    assert "source.disconnected" in actions


async def test_disconnecting_something_already_off_is_not_an_error(admin: ApiClient) -> None:
    """The caller asked for this source to be off, and it is off."""
    assert (await admin.delete("/connections/openrouter")).status_code == 204
    assert (await admin.delete("/connections/openrouter")).status_code == 204


# --- test -------------------------------------------------------------------


async def test_a_rejected_key_is_a_200_with_ok_false(
    admin: ApiClient, monkeypatch: Any, working_key: None
) -> None:
    """The call the caller asked for succeeded; the answer is that the key is bad.

    A 4xx here would be indistinguishable from a malformed request, and the
    screen would have to guess which it was.
    """
    await connect(admin)

    async def reject(*_args: Any, **_kwargs: Any) -> None:
        raise OpenRouterError("OpenRouter rejected this key.")

    monkeypatch.setattr("agent.api.routes_connections.probe_key", reject)

    response = await admin.post("/connections/openrouter/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["recorded"] is True

    sources = {row["kind"]: row for row in (await admin.get("/connections")).json()["sources"]}
    assert sources["openrouter"]["last_test_ok"] is False


async def test_testing_a_disconnected_source_keeps_no_state(
    admin: ApiClient, db: AsyncSession, working_key: None
) -> None:
    """It answers "would this work if I switched it on", and inventing a row to
    remember that would make a test look like a decision."""
    body = (await admin.post("/connections/openrouter/test")).json()

    assert body["ok"] is True
    assert body["recorded"] is False
    rows = (await db.execute(sa.select(sa.func.count()).select_from(SourceConnection))).scalar_one()
    assert rows == 0


async def test_any_member_may_test_but_only_an_admin_may_switch(
    signed_in_as: Any, working_key: None
) -> None:
    """The person watching a run skip a source is often not the one who can fix it."""
    viewer = await signed_in_as("viewer")

    assert (await viewer.post("/connections/openrouter/test")).status_code == 200

    refusal = await viewer.post("/connections/openrouter/connect")
    assert refusal.status_code == 403
    assert refusal.json()["missing_permission"] == "credential_write"
    assert (await viewer.delete("/connections/openrouter")).status_code == 403


async def test_an_operator_cannot_switch_a_source_off(
    admin: ApiClient, signed_in_as: Any, working_key: None
) -> None:
    await connect(admin)
    operator = await signed_in_as("operator")

    assert (await operator.delete("/connections/openrouter")).status_code == 403


# --- what a run sees --------------------------------------------------------


async def test_a_configured_but_disconnected_source_is_not_resolvable(
    db: AsyncSession, workspace_id: Any, admin_user: Any
) -> None:
    """The whole point of the decision being separate from the configuration.

    One deployment serves several workspaces. A key being *present* is not the
    same as a workspace being entitled to spend it, so resolution refuses until
    somebody says otherwise — and names which of the two is missing.
    """
    from agent.credentials import MissingCredential, resolve_values

    with pytest.raises(MissingCredential) as refused:
        await resolve_values(db, workspace_id=workspace_id, kind=CredentialKind.OPENROUTER)
    assert refused.value.reason == "not_connected"

    db.add(
        SourceConnection(
            workspace_id=workspace_id,
            kind=CredentialKind.OPENROUTER,
            connected_by=admin_user.id,
        )
    )
    await db.commit()

    values = await resolve_values(db, workspace_id=workspace_id, kind=CredentialKind.OPENROUTER)
    assert values == {"api_key": OPENROUTER_KEY}


async def test_a_connected_but_unconfigured_source_names_the_variables(
    db: AsyncSession, workspace_id: Any, admin_user: Any
) -> None:
    """The other half: a decision survives a deployment that lost its key."""
    from agent.credentials import MissingCredential, resolve_values

    db.add(
        SourceConnection(
            workspace_id=workspace_id,
            kind=CredentialKind.DATAFORSEO,
            connected_by=admin_user.id,
        )
    )
    await db.commit()

    with pytest.raises(MissingCredential) as refused:
        await resolve_values(db, workspace_id=workspace_id, kind=CredentialKind.DATAFORSEO)
    assert refused.value.reason == "not_configured"
    assert "DATAFORSEO_API_KEY" in refused.value.detail
