"""Which market a SERP probe is aimed at.

Small, and load-bearing. `gl` and `hl` decide which country's result page the
proxy buys, so getting them wrong does not fail — it quietly researches the
wrong market and every number downstream is about somebody else's customers.

A project already stores ISO codes (PRD §6), so there is no vendor lookup table
to get wrong. What is worth asserting is the empty case: the helpers return `""`
for a project with no market, and `connectors/serp.py` falls back to the
configured default rather than sending `gl=` and getting whatever Google infers
from the proxy's exit node.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.nodes.stage_1_3 import _country, _language


@dataclass
class _Project:
    markets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Ctx:
    project: _Project


def ctx(*markets: dict[str, Any]) -> Any:
    return _Ctx(project=_Project(markets=list(markets)))


NORWAY = {"country": "NO", "language": "nb", "currency": "NOK"}
US = {"country": "US", "language": "en", "currency": "USD"}


def test_the_first_market_is_the_one_probed() -> None:
    assert _country(ctx(NORWAY, US)) == "no"
    assert _language(ctx(NORWAY, US)) == "nb"


def test_the_codes_are_lowercased_for_the_query_string() -> None:
    """Stored as `NO`, sent as `gl=no`. Google accepts either; one spelling is easier to read."""
    assert _country(ctx(US)) == "us"
    assert _language(ctx(US)) == "en"


def test_a_project_with_no_market_defers_to_the_configured_default() -> None:
    """Empty, not a guess — `SerpConnector.fetch` reads `serp_country` when this is falsy."""
    assert _country(ctx()) == ""
    assert _language(ctx()) == ""


def test_a_half_filled_market_does_not_produce_a_half_filled_query() -> None:
    assert _country(ctx({"currency": "EUR"}, NORWAY)) == "no"
    assert _language(ctx({"country": "DE"}, NORWAY)) == "nb"
