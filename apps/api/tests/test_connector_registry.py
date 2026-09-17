"""The registry, and the import costs it exists to avoid."""

from __future__ import annotations

import subprocess
import sys

import pytest

from agent.config import Settings
from agent.connectors import (
    CONNECTOR_NAMES,
    BaseConnector,
    ConnectorContext,
    ConnectorError,
    build_connector,
    connector_class,
    credential_kind_for,
    source_for,
)
from agent.db.models import CredentialKind, EvidenceSource

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="


def test_every_prd_connector_is_registered() -> None:
    """PRD §9's five, and only those.

    A sixth lived here — `serp`, the live Google result page §9.3 asks for and
    the keyword vendor only half answers. It was removed with its Bright Data
    account, so §9.3's ad half now has no source and the report says so rather
    than a node inferring it.
    """
    assert set(CONNECTOR_NAMES) == {
        "google_ads",
        "transparency",
        "dataforseo",
        "web_crawler",
        "csv_ingest",
    }


@pytest.mark.parametrize("name", CONNECTOR_NAMES)
def test_every_connector_builds_and_declares_its_source(name: str) -> None:
    built = build_connector(name, ConnectorContext(settings=Settings(app_encryption_key=TEST_KEY)))
    assert isinstance(built, BaseConnector)
    assert built.name == name
    assert built.source is source_for(name)


def test_every_declared_source_is_a_real_enum_member() -> None:
    assert {source_for(name) for name in CONNECTOR_NAMES} <= set(EvidenceSource)


def test_only_the_paid_apis_need_a_credential() -> None:
    """`transparency` and `web_crawler` read public pages; `csv_ingest` reads an upload."""
    needing = {name for name in CONNECTOR_NAMES if credential_kind_for(name)}
    assert needing == {"google_ads", "dataforseo"}


def test_credential_kinds_match_the_schema_enum() -> None:
    """A kind the `credential` table cannot store is a runtime failure at save time."""
    for name in CONNECTOR_NAMES:
        kind = credential_kind_for(name)
        if kind is not None:
            assert kind in {member.value for member in CredentialKind}


def test_an_unknown_connector_is_named_in_the_error() -> None:
    with pytest.raises(ConnectorError, match="semrush"):
        connector_class("semrush")


def test_importing_the_registry_pulls_in_neither_chromium_nor_onnx() -> None:
    """The API process imports this on any request that lists connectors.

    Paying a Playwright and ONNX-runtime import on that path would add seconds
    of cold start for a response that is five short strings. Checked in a clean
    interpreter because the test session has already imported half the world.
    """
    code = (
        "import sys; import agent.connectors as c; "
        "assert c.CONNECTOR_NAMES; "
        "print('playwright' in sys.modules, 'fastembed' in sys.modules, "
        "'onnxruntime' in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "APP_ENCRYPTION_KEY": TEST_KEY, "PYTHONPATH": "src"},
    )
    assert result.stdout.strip() == "False False False"
