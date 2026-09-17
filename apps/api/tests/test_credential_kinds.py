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
WEBSHARE = {"api_key": "ws-api-key-abc123"}


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
        (CredentialKind.WEBSHARE, WEBSHARE),
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


def test_a_proxy_account_is_one_key_and_nothing_else() -> None:
    """No proxy host, no port, no username: the key is the whole credential.

    Asserted on `webshare` since the SERP source was removed. The claim is the
    same one and it matters for the same reason — `connectors/proxy.account`
    reads the username and password off the key rather than asking a person for
    them — and here `connector` is None because the thing the key authenticates
    is a transport several connectors borrow, tested by `proxy.probe`.
    """
    spec = spec_for(CredentialKind.WEBSHARE)
    cleaned = spec.validate(WEBSHARE)

    assert cleaned == WEBSHARE
    assert spec.connector is None
    assert [field.name for field in spec.fields] == ["api_key"]
    assert unseal(spec, spec.seal(cleaned)) == WEBSHARE


def test_a_one_key_credential_shows_only_its_last_four() -> None:
    """There is no non-secret half left to show, so `meta` must not invent one."""
    spec = spec_for(CredentialKind.WEBSHARE)
    meta = spec.meta(WEBSHARE)

    assert meta == {"last4": "c123"}
    assert WEBSHARE["api_key"] not in set(map(str, meta.values()))


def test_a_credential_sealed_before_the_change_still_unseals() -> None:
    """A workspace that connected DataForSEO last week must not have to retype it.

    The old rows are JSON objects under a kind that is now single-field — this
    one held a `login`/`password` pair. The values they carry are no longer what
    the connector wants — that is a reconnect, and it says so — but the vault
    must still be able to read them back rather than hand a connector the raw
    JSON as if it were a key.
    """
    spec = spec_for(CredentialKind.DATAFORSEO)
    legacy = json.dumps({"login": "ops@example.com", "password": "pw"})

    assert unseal(spec, legacy) == {"login": "ops@example.com", "password": "pw"}
    # And a real key, which is not JSON, still lands on the one field.
    assert unseal(spec, "dfs-api-key-abc123") == {"api_key": "dfs-api-key-abc123"}


def test_smtp_is_not_writable_through_the_interface() -> None:
    """SMTP is deployment configuration (PRD §18 law 9), not workspace state."""
    assert CredentialKind.SMTP not in KIND_SPECS
    with pytest.raises(ValueError, match="smtp"):
        spec_for(CredentialKind.SMTP)


def test_the_run_path_and_the_test_path_decode_a_secret_the_same_way() -> None:
    """One decoder, or a source connects and then goes missing mid-run.

    `routes_credentials._run_test` and `nodes/gather._credentials` both turn a
    sealed string back into connector-shaped values. While they were two pieces
    of code, `gather` guessed — a bare string became `{"token": ...}` — and that
    guess held only because every kind it reached was multi-field. A one-key
    kind broke it silently: the card said Working and the run skipped the
    source. This asserts they agree, for the single-field kinds that exposed it.
    """
    from agent.nodes.gather import _CREDENTIAL_KIND

    cases = {
        CredentialKind.WEBSHARE: {"api_key": "ws-real-key"},
        CredentialKind.DATAFORSEO: {"api_key": "ops@example.com:hunter2"},
        CredentialKind.GOOGLE_ADS: GOOGLE_ADS,
    }
    for kind, values in cases.items():
        spec = spec_for(kind)
        sealed = spec.seal(spec.validate(values))
        decoded = unseal(spec, sealed)
        assert decoded == values, kind.value
        # And the field the connector will ask for is actually present.
        assert spec.fields[0].name in decoded, kind.value

    # Every connector `gather` can reach has a spec, or `spec_for` raises at
    # run time on a path no test covers.
    for connector, kind in _CREDENTIAL_KIND.items():
        assert spec_for(kind) is not None, connector


def test_every_env_backed_kind_has_a_settings_field_of_the_same_name() -> None:
    """The one invariant holding three separate lists together.

    `KindSpec.env_var` lowercased is read off `Settings` by
    `credentials.env_secret`. Nothing enforces that at import time, and a
    mismatch does not raise — `getattr(..., None)` returns None and the source
    silently reports itself unconfigured. So it is asserted here instead.
    """
    from agent.config import Settings
    from agent.credential_kinds import KIND_SPECS

    for spec in KIND_SPECS.values():
        if spec.env_var is None:
            continue
        field = spec.env_var.lower()
        assert field in Settings.model_fields, f"{spec.kind.value} names {spec.env_var}"


def test_google_ads_is_not_configurable_from_a_file() -> None:
    """Its secret is a refresh token consent mints, so there is nothing to paste."""
    assert spec_for(CredentialKind.GOOGLE_ADS).env_var is None


def test_the_environment_supplies_a_key_when_no_row_does() -> None:
    from agent.config import get_settings
    from agent.credentials import env_secret

    get_settings.cache_clear()
    try:
        import os

        os.environ["DATAFORSEO_API_KEY"] = "dfs-from-the-file"
        get_settings.cache_clear()
        assert env_secret(CredentialKind.DATAFORSEO) == "dfs-from-the-file"
        # Whitespace-only is not a key: a variable left blank in a compose file
        # must read as unconfigured, not as an empty secret the vendor rejects.
        os.environ["DATAFORSEO_API_KEY"] = "   "
        get_settings.cache_clear()
        assert env_secret(CredentialKind.DATAFORSEO) is None
    finally:
        os.environ.pop("DATAFORSEO_API_KEY", None)
        get_settings.cache_clear()
