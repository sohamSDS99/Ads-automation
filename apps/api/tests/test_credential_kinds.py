"""How a credential is sealed, and what the vault is allowed to remember."""

from __future__ import annotations

import json

import pytest

from agent.credential_kinds import KIND_SPECS, spec_for, unseal
from agent.db.models import CredentialKind

OPENROUTER = {"api_key": "sk-or-v1-abcdef123456"}
DATAFORSEO = {"login": "ops@example.com", "password": "hunter2"}
GOOGLE_ADS = {
    "developer_token": "dev-token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "customer_id": "123-456-7890",
}


def test_a_single_field_kind_seals_the_bare_value() -> None:
    """The run path passes the OpenRouter secret straight to the gateway.

    `executor._build_gateway` does no decoding, so sealing JSON here would send
    `{"api_key": "..."}` as the bearer token and every run would 401.
    """
    spec = spec_for(CredentialKind.OPENROUTER)
    assert spec.seal(OPENROUTER) == "sk-or-v1-abcdef123456"


def test_a_multi_field_kind_seals_json_that_gather_can_read() -> None:
    spec = spec_for(CredentialKind.DATAFORSEO)
    sealed = spec.seal(DATAFORSEO)
    assert json.loads(sealed) == DATAFORSEO


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        (CredentialKind.OPENROUTER, OPENROUTER),
        (CredentialKind.DATAFORSEO, DATAFORSEO),
        (CredentialKind.GOOGLE_ADS, GOOGLE_ADS),
    ],
)
def test_sealing_then_unsealing_returns_the_connector_shape(
    kind: CredentialKind, values: dict[str, str]
) -> None:
    spec = spec_for(kind)
    assert unseal(spec, spec.seal(values)) == values


def test_meta_carries_hints_and_never_a_secret() -> None:
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    meta = spec.meta(GOOGLE_ADS)

    assert meta["customer_id"] == "123-456-7890"
    assert meta["last4"] == "oken"
    secrets = {values for key, values in GOOGLE_ADS.items() if key != "customer_id"}
    assert not secrets & set(map(str, meta.values()))


def test_a_missing_required_field_is_named() -> None:
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    with pytest.raises(ValueError, match="refresh_token"):
        spec.validate({key: value for key, value in GOOGLE_ADS.items() if key != "refresh_token"})


def test_an_optional_field_may_be_absent() -> None:
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    assert "login_customer_id" not in spec.validate(GOOGLE_ADS)


def test_an_unknown_field_is_dropped_rather_than_sealed() -> None:
    """A field this build has never heard of must not end up inside a secret."""
    spec = spec_for(CredentialKind.DATAFORSEO)
    cleaned = spec.validate({**DATAFORSEO, "api_key_v3": "something-new"})
    assert cleaned == DATAFORSEO


def test_smtp_is_not_writable_through_the_interface() -> None:
    """SMTP is deployment configuration (PRD §18 law 9), not workspace state."""
    assert CredentialKind.SMTP not in KIND_SPECS
    with pytest.raises(ValueError, match="smtp"):
        spec_for(CredentialKind.SMTP)
