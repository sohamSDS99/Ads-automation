"""Where each source's credential comes from, and what may be said about it.

Every secret is read from the deployment's environment now, so the invariants
worth asserting all sit on that seam: the variable names agree with `Settings`,
a blank variable reads as unconfigured rather than as an empty key, and what
`from_env` produces is the shape connectors already expect.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from agent.config import Settings, get_settings
from agent.credential_kinds import KIND_SPECS, spec_for
from agent.db.models import CredentialKind

GOOGLE_ADS_ENV = {
    "GOOGLE_ADS_DEVELOPER_TOKEN": "dev-token",
    "GOOGLE_ADS_CLIENT_ID": "client-id",
    "GOOGLE_ADS_CLIENT_SECRET": "client-secret",
    "GOOGLE_ADS_REFRESH_TOKEN": "refresh-token",
    "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
}


@pytest.fixture
def env() -> Iterator[None]:
    """Set variables for one test and put the process back as it was."""
    before = dict(os.environ)
    get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(before)
    get_settings.cache_clear()


def _set(**values: str) -> Settings:
    os.environ.update(values)
    get_settings.cache_clear()
    return get_settings()


# --- the seam between a spec and the process environment --------------------


def test_every_field_names_a_settings_field_of_the_same_name() -> None:
    """The one invariant holding two separate lists together.

    `FieldSpec.env_var` lowercased is read off `Settings` by
    `credential_kinds._setting`. Nothing enforces that at import time, and a
    mismatch does not raise — `getattr(..., None)` returns None and the source
    silently reports itself unconfigured, which reads as "the operator forgot"
    rather than "we misspelled it". So it is asserted here instead.
    """
    for spec in KIND_SPECS.values():
        for field in spec.fields:
            assert field.env_var.lower() in Settings.model_fields, (
                f"{spec.kind.value}.{field.name} names {field.env_var}, "
                "which Settings does not hold"
            )


def test_every_variable_is_named_once_across_every_source() -> None:
    """Two sources reading one variable would make disconnecting one a lie."""
    seen: dict[str, str] = {}
    for spec in KIND_SPECS.values():
        for field in spec.fields:
            assert field.env_var not in seen, f"{field.env_var} is claimed twice"
            seen[field.env_var] = spec.kind.value


def test_a_blank_variable_reads_as_unconfigured(env: None) -> None:
    """A variable left empty in a compose file is not a key.

    An empty string would sail past a `is not None` check and reach the vendor
    as an empty bearer token, so the source would report configured and then
    401 on every call.
    """
    spec = spec_for(CredentialKind.DATAFORSEO)
    assert spec.configured(_set(DATAFORSEO_API_KEY="dfs-from-the-file"))
    assert spec.from_env(get_settings()) == {"api_key": "dfs-from-the-file"}

    settings = _set(DATAFORSEO_API_KEY="   ")
    assert not spec.configured(settings)
    assert spec.missing_env_vars(settings) == ("DATAFORSEO_API_KEY",)
    assert spec.from_env(settings) == {}


def test_an_unset_optional_field_is_absent_rather_than_empty(env: None) -> None:
    """`login-customer-id` is a header Google Ads must not receive when empty.

    An account reached directly has no manager, and sending the header with an
    empty value is a different request from not sending it — the API answers it
    as a permission error on an account that is perfectly reachable.
    """
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    settings = _set(**GOOGLE_ADS_ENV)

    assert spec.configured(settings)
    values = spec.from_env(settings)
    assert "login_customer_id" not in values

    settings = _set(GOOGLE_ADS_LOGIN_CUSTOMER_ID="999-888-7777")
    assert spec.from_env(settings)["login_customer_id"] == "999-888-7777"


def test_google_ads_names_every_value_it_needs(env: None) -> None:
    """Six values, five of them required — and the screen can name the missing ones."""
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    settings = _set()

    assert spec.missing_env_vars(settings) == tuple(GOOGLE_ADS_ENV)
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" in spec.env_vars
    assert "GOOGLE_ADS_LOGIN_CUSTOMER_ID" not in spec.required_env_vars


# --- what reaches a connector -----------------------------------------------


def test_from_env_produces_the_shape_every_connector_asks_for(env: None) -> None:
    """`ConnectorContext.credentials` is keyed by field name, not by variable name.

    This is the seam a sealed vault used to sit on, and the one it broke at: a
    single-field kind once arrived at its connector as `{"token": ...}` while
    the connector asked for `api_key`, so a source that tested green skipped
    every run. Reading from the environment keeps the same key names, and this
    asserts it for each source `gather` can reach.
    """
    from agent.nodes.gather import _CREDENTIAL_KIND

    settings = _set(
        OPENROUTER_API_KEY="sk-or-v1-abcdef123456",
        DATAFORSEO_API_KEY="ops@example.com:hunter2",
        WEBSHARE_API_KEY="ws-api-key-abc123",
        **GOOGLE_ADS_ENV,
    )
    for kind, spec in KIND_SPECS.items():
        values = spec.from_env(settings)
        assert spec.fields[0].name in values, kind.value

    # Every connector `gather` can reach has a spec, or `spec_for` raises at
    # run time on a path no test covers.
    for connector, kind in _CREDENTIAL_KIND.items():
        assert spec_for(kind) is not None, connector


def test_only_the_model_surface_stops_a_run() -> None:
    """Every other source thins the report rather than refusing to start (PRD §15 NF4)."""
    blocking = {kind for kind, spec in KIND_SPECS.items() if spec.required_for_runs}
    assert blocking == {CredentialKind.OPENROUTER}


# --- what may be shown ------------------------------------------------------


def test_meta_shows_the_non_secret_fields_and_a_last_four(env: None) -> None:
    spec = spec_for(CredentialKind.GOOGLE_ADS)
    values = spec.from_env(_set(**GOOGLE_ADS_ENV))
    hints = spec.meta(values)

    assert hints["customer_id"] == "123-456-7890"
    assert hints["last4"] == "oken"
    for field in spec.fields:
        if field.secret:
            assert values[field.name] not in hints.values(), field.name


def test_smtp_is_not_a_connectable_source() -> None:
    """SMTP is deployment configuration (PRD §18 law 9) with nothing to switch on."""
    assert CredentialKind.SMTP not in KIND_SPECS
    with pytest.raises(ValueError, match="smtp"):
        spec_for(CredentialKind.SMTP)
