"""4.3.1 `sitelinks_callouts_snippets` — the parts that decide (PRD §11 4.3.1).

The node's database, URL-check and model work is proved in
`tests/integration/test_s4p8_extras.py`. Here:

* the URLs a sitelink may point at are on the project's domain, one per page,
  and nothing else is offered to the model;
* the draft schema takes a URL only from that pool and a snippet header only
  from `extras.snippet_headers`, in the counts the spec sheet sets;
* each extra is linted as the surface whose asset type is its own spec.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent.nodes.creative.n4_3_1_sitelinks_callouts_snippets import (
    NEEDS,
    SURFACES,
    Counts,
    Page,
    candidate_pages,
    draft_model,
)
from agent.schemas.guardrails import SURFACE_ASSET_TYPES

DOMAIN = "sdsmanager.com"
POOL = [
    Page("https://sdsmanager.com/pricing", "Pricing"),
    Page("https://sdsmanager.com/demo", None),
]
HEADERS = ("Service catalog", "Types")


def test_each_extra_is_linted_as_its_own_asset_type() -> None:
    assert set(SURFACES) == set(NEEDS) == {"sitelink", "callout", "structured_snippet"}
    for asset_type, surface in SURFACES.items():
        assert SURFACE_ASSET_TYPES[surface] == asset_type


def test_the_pool_is_on_domain_and_one_url_per_page() -> None:
    found = [
        ("https://sdsmanager.com/pricing", "Pricing"),
        ("https://www.sdsmanager.com/pricing/", "Pricing again"),
        ("https://partner.net/sds", "Partner"),
        ("https://sdsmanager.com.evil.net/", "Lookalike"),
        ("https://help.sdsmanager.com/start", None),
        ("mailto:sales@sdsmanager.com", None),
    ]
    assert candidate_pages(found, domain=DOMAIN) == [
        Page("https://sdsmanager.com/pricing", "Pricing"),
        Page("https://help.sdsmanager.com/start", None),
    ]


def _answer(**overrides: Any) -> dict[str, Any]:
    return {
        "sitelinks": [
            {
                "link_text": "See pricing",
                "line1": "Plans for every team",
                "line2": "Compare the tiers",
                "final_url": "https://sdsmanager.com/pricing",
            }
        ],
        "callouts": ["Audit-ready SDS", "Free onboarding"],
        "snippet": {"header": "Types", "values": ["Chemicals", "Labels", "Reports"]},
        **overrides,
    }


def _model() -> Any:
    return draft_model(
        [page.url for page in POOL],
        Counts(sitelinks=1, callouts=2, snippet_values=(3, 4)),
        headers=HEADERS,
    )


def test_a_draft_in_the_counts_the_spec_sets_is_accepted() -> None:
    _model().model_validate(_answer())


def test_a_sitelink_url_outside_the_pool_is_refused() -> None:
    with pytest.raises(ValidationError):
        _model().model_validate(
            _answer(
                sitelinks=[
                    {
                        "link_text": "Partner",
                        "line1": "a",
                        "line2": "b",
                        "final_url": "https://partner.net/sds",
                    }
                ]
            )
        )


def test_a_snippet_header_google_does_not_list_is_refused() -> None:
    with pytest.raises(ValidationError):
        _model().model_validate(_answer(snippet={"header": "Features", "values": ["a", "b", "c"]}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"callouts": ["Only one"]},
        {"snippet": {"header": "Types", "values": ["a", "b"]}},
        {"snippet": {"header": "Types", "values": ["a", "b", "c", "d", "e"]}},
    ],
)
def test_counts_outside_the_spec_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate(_answer(**overrides))


def test_an_asset_type_with_no_spec_is_not_asked_for() -> None:
    model = draft_model(
        [page.url for page in POOL],
        Counts(sitelinks=0, callouts=2, snippet_values=None),
        headers=HEADERS,
    )
    assert set(model.model_fields) == {"callouts"}
