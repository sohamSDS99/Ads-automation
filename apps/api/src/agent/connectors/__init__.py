"""Evidence sources. One registry, six connectors (PRD §9).

Nodes and routes look a connector up by name rather than importing its class, so
adding a source is one entry here and the rest of the system does not change.
Construction is lazy: importing this package must not import Playwright or the
ONNX runtime, because the `api` process imports it on every request that lists
connectors and opens neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorContext,
    ConnectorDegraded,
    ConnectorError,
    ConnectorRateLimited,
    ConnectorStatus,
    EvidenceDraft,
)
from agent.db.models import EvidenceSource

if TYPE_CHECKING:
    pass

__all__ = [
    "CONNECTOR_NAMES",
    "BaseConnector",
    "ConnectorAuthError",
    "ConnectorContext",
    "ConnectorDegraded",
    "ConnectorError",
    "ConnectorRateLimited",
    "ConnectorStatus",
    "EvidenceDraft",
    "build_connector",
    "connector_class",
    "credential_kind_for",
]

#: Every connector, in the order the setup wizard should present them.
CONNECTOR_NAMES: tuple[str, ...] = (
    "google_ads",
    "transparency",
    "dataforseo",
    "web_crawler",
    "csv_ingest",
)

#: Which connectors need a stored secret, and under which `CredentialKind`.
#: `transparency` and `web_crawler` read public pages; `csv_ingest` reads an
#: upload. Those three are deliberately absent.
_CREDENTIAL_KIND: dict[str, str] = {
    "google_ads": "google_ads",
    "dataforseo": "dataforseo",
}

_SOURCES: dict[str, EvidenceSource] = {
    "google_ads": EvidenceSource.GOOGLE_ADS,
    "transparency": EvidenceSource.TRANSPARENCY,
    "dataforseo": EvidenceSource.DATAFORSEO,
    "web_crawler": EvidenceSource.WEB,
    "csv_ingest": EvidenceSource.CSV,
}


def credential_kind_for(name: str) -> str | None:
    """The `CredentialKind` this connector needs, or None if it needs no secret."""
    return _CREDENTIAL_KIND.get(name)


def source_for(name: str) -> EvidenceSource:
    try:
        return _SOURCES[name]
    except KeyError:
        raise ConnectorError(f"unknown connector: {name!r}") from None


def connector_class(name: str) -> type[BaseConnector]:
    """Import and return one connector class. Imports happen here, not at module load."""
    if name == "google_ads":
        from agent.connectors.google_ads import GoogleAdsConnector

        return GoogleAdsConnector
    if name == "dataforseo":
        from agent.connectors.dataforseo import DataForSEOConnector

        return DataForSEOConnector
    if name == "transparency":
        from agent.connectors.transparency import TransparencyConnector

        return TransparencyConnector
    if name == "web_crawler":
        from agent.connectors.web_crawler import WebCrawlerConnector

        return WebCrawlerConnector
    if name == "csv_ingest":
        from agent.connectors.csv_ingest import CsvIngestConnector

        return CsvIngestConnector
    raise ConnectorError(f"unknown connector: {name!r}")


def build_connector(name: str, context: ConnectorContext | None = None) -> BaseConnector:
    """Construct a connector by name with its context already attached."""
    return connector_class(name)(context)
