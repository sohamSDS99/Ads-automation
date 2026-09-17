"""How a credential is sealed, and what the vault is allowed to remember."""

from __future__ import annotations

import json

import pytest

from agent.credential_kinds import KIND_SPECS, spec_for, unseal
from agent.db.models import CredentialKind

OPENROUTER = {"api_key": "sk-or-v1-abcdef123456"}
DATAFORSEO = {"api_key": "ops@example.com:hunter2"}
GOOGLE_ADS = {
    "developer_token": "dev-token",
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
    "customer_id": "123-456-7890",
}
BRIGHTDATA = {"api_key": "brd-api-key-abc123"}


def test_a_single_field_kind_seals_the_bare_value() -> None:
    """The run path passes the OpenRouter secret straight to the gateway.

    `executor._build_gateway` does no decoding, so sealing JSON here would send
    `{"api_key": "..."}` as the bearer token and every run would 401.
    """
    spec = spec_for(CredentialKind.OPENROUTER)
    assert spec.seal(OPENROUTER) == "sk-or-v1-abcdef123456"


def test_a_multi_field_kind_seals_json_that_gather_can_read() -> None:
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    sealed = spec.seal(GOOGLE_ADS)
    assert json.loads(sealed) == GOOGLE_ADS


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        (CredentialKind.OPENROUTER, OPENROUTER),
        (CredentialKind.DATAFORSEO, DATAFORSEO),
        (CredentialKind.GOOGLE_ADS, GOOGLE_ADS),
        (CredentialKind.BRIGHTDATA, BRIGHTDATA),
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


def test_every_source_asks_a_person_for_exactly_one_value() -> None:
    """The whole point of the change: one field per card, and no second step.

    `typed_fields` — not `fields` — is what the settings and wizard screens
    render, so it is what this asserts on. `google_ads` still holds six values;
    five of them arrive from consent, and the sixth is the developer token.
    """
    # Derived, not listed: a hardcoded tuple here passes a correct change that
    # adds a kind — `webshare` was the case that found this — by not looking
    # at it at all.
    for kind in KIND_SPECS:
        spec = spec_for(kind)
        typed = spec.typed_fields
        assert len(typed) == 1, f"{kind.value} asks for {[f.name for f in typed]}"
        assert typed[0].required, f"{kind.value}'s one field must not be optional"


def test_a_serp_account_is_one_key_and_nothing_else() -> None:
    """No proxy host, no port, no username: the key is the whole credential."""
    spec = spec_for(CredentialKind.BRIGHTDATA)
    cleaned = spec.validate(BRIGHTDATA)

    assert cleaned == BRIGHTDATA
    assert spec.connector == "serp"
    assert [field.name for field in spec.fields] == ["api_key"]
    assert unseal(spec, spec.seal(cleaned)) == BRIGHTDATA


def test_a_serp_key_shows_only_its_last_four() -> None:
    """There is no non-secret half left to show, so `meta` must not invent one."""
    spec = spec_for(CredentialKind.BRIGHTDATA)
    meta = spec.meta(BRIGHTDATA)

    assert meta == {"last4": "c123"}
    assert BRIGHTDATA["api_key"] not in set(map(str, meta.values()))


def test_a_credential_sealed_before_the_change_still_unseals() -> None:
    """A workspace that connected Bright Data last week must not have to retype it.

    The old rows are JSON objects under a kind that is now single-field. The
    values they carry are no longer what the connector wants — that is a
    reconnect, and it says so — but the vault must still be able to read them
    back rather than hand a connector the raw JSON as if it were a key.
    """
    spec = spec_for(CredentialKind.BRIGHTDATA)
    legacy = json.dumps({"username": "brd-customer-hl_abc123-zone-serp1", "password": "pw"})

    assert unseal(spec, legacy) == {
        "username": "brd-customer-hl_abc123-zone-serp1",
        "password": "pw",
    }
    # And a real key, which is not JSON, still lands on the one field.
    assert unseal(spec, "brd-api-key-abc123") == {"api_key": "brd-api-key-abc123"}


def test_smtp_is_not_writable_through_the_interface() -> None:
    """SMTP is deployment configuration (PRD §18 law 9), not workspace state."""
    assert CredentialKind.SMTP not in KIND_SPECS
    with pytest.raises(ValueError, match="smtp"):
        spec_for(CredentialKind.SMTP)
