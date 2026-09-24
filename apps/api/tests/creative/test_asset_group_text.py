"""4.2.5 `asset_group_text` — the contract and the parts that decide (PRD §11 4.2.5, §12.2).

The node's database and model work is proved in
`tests/integration/test_s4p6_descriptions_variant_b.py`. What is proved here:

* `not_required` exactly when the slate has no Performance Max, Demand Gen or
  Display campaign — and, said why, when it has one but the run's brief covers
  no asset group in it;
* every text is linted as a surface whose Stage 03 asset type is the spec it is
  written against, so the pin's limits apply to it and no other limits do;
* a description is claim-bound wherever it runs (law 34, §12.2).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from agent.export.plan_contract import ChannelSlate
from agent.nodes.creative.n4_2_5_asset_group_text import (
    NEEDS,
    SURFACES,
    TYPES,
    draft_model,
    not_required_reason,
    slate_types,
)
from agent.schemas.guardrails import SURFACE_ASSET_TYPES
from agent.schemas.search_ads import AssetGroupText, AssetGroupTextOutput

LICENSED = uuid.UUID(int=201)
CONTEXT = {"licensed_claim_ids": frozenset({LICENSED})}
LINT = {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []}


# ---------------------------------------------------------------------------
# what is written, and what it is linted as
# ---------------------------------------------------------------------------


def test_the_three_asset_group_types() -> None:
    assert TYPES == ("performance_max", "demand_gen", "display")


def test_every_type_has_a_surface_for_every_text_it_writes() -> None:
    for campaign_type in TYPES:
        assert (
            set(SURFACES[campaign_type])
            == set(NEEDS)
            == {
                "headline",
                "long_headline",
                "description",
                "business_name",
            }
        )


def test_each_surface_is_linted_as_the_asset_type_it_is_written_against() -> None:
    # Stage 03 scopes a spec rule by asset type through this bridge; a surface
    # it does not know would take every asset-typed rule at once.
    for campaign_type in TYPES:
        for asset_type, surface in SURFACES[campaign_type].items():
            assert SURFACE_ASSET_TYPES[surface] == asset_type, (campaign_type, asset_type)


# ---------------------------------------------------------------------------
# not_required
# ---------------------------------------------------------------------------


def test_the_slates_types_are_read_as_written_whatever_the_case() -> None:
    slate = ChannelSlate.model_validate(
        {"slate": [{"campaign_type": " Search "}, {"campaign_type": "Performance_Max"}]}
    )
    assert slate_types(slate) == {"search", "performance_max"}
    assert slate_types(ChannelSlate()) == set()


@pytest.mark.parametrize("slate", [set(), {"search"}, {"search", "video", "shopping"}])
def test_a_slate_without_an_asset_group_type_requires_nothing(slate: set[str]) -> None:
    reason = not_required_reason(slate, groups=0)
    assert reason is not None and "no Performance Max, Demand Gen or Display" in reason


def test_a_slate_with_one_but_no_asset_group_in_scope_says_why() -> None:
    reason = not_required_reason({"search", "display"}, groups=0)
    assert reason is not None and "display" in reason and "no asset group" in reason


@pytest.mark.parametrize("campaign_type", TYPES)
def test_an_asset_group_on_the_slate_requires_text(campaign_type: str) -> None:
    assert not_required_reason({"search", campaign_type}, groups=1) is None


# ---------------------------------------------------------------------------
# the schema the model answers in
# ---------------------------------------------------------------------------

COUNTS = {"headline": 3, "long_headline": 1, "description": 2}


def _draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "headlines": ["One Library For Every Site"] * 3,
        "long_headlines": ["Every safety data sheet current, on every site"],
        "descriptions": [{"text": "SDS updates within 24 hours.", "claim_ids": [str(LICENSED)]}]
        * 2,
        "business_name": "Example SDS",
    }
    draft.update(overrides)
    return draft


def test_the_draft_takes_exactly_the_counts_the_specs_allow() -> None:
    model = draft_model((LICENSED,), counts=COUNTS)
    assert model.model_validate(_draft())
    with pytest.raises(ValidationError):
        model.model_validate(_draft(headlines=["One"] * 2))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(long_headlines=[]))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(descriptions=_draft()["descriptions"] * 2))


def test_a_description_in_the_draft_cites_a_licensed_claim() -> None:
    model = draft_model((LICENSED,), counts=COUNTS)
    bare = {"text": "One library for every site.", "claim_ids": []}
    with pytest.raises(ValidationError):
        model.model_validate(_draft(descriptions=[bare] * 2))
    other = {"text": "One library for every site.", "claim_ids": [str(uuid.UUID(int=9))]}
    with pytest.raises(ValidationError):
        model.model_validate(_draft(descriptions=[other] * 2))


def test_an_empty_line_or_business_name_fails_the_draft() -> None:
    model = draft_model((LICENSED,), counts=COUNTS)
    with pytest.raises(ValidationError):
        model.model_validate(_draft(business_name=""))
    with pytest.raises(ValidationError):
        model.model_validate(_draft(headlines=["", "Two", "Three"]))


def test_with_no_licensed_claim_there_is_no_schema_to_answer() -> None:
    with pytest.raises(ValueError, match="licensed"):
        draft_model((), counts=COUNTS)


# ---------------------------------------------------------------------------
# the output
# ---------------------------------------------------------------------------


def _line(text: str = "One Library For Every Site", **extra: Any) -> dict[str, Any]:
    return {"asset_id": str(uuid.uuid4()), "text": text, "lint": LINT, **extra}


def _group(**overrides: Any) -> dict[str, Any]:
    group: dict[str, Any] = {
        "campaign_ref": "c-pmax",
        "ad_group_ref": "sds asset group",
        "campaign_type": "performance_max",
        "market": "*",
        "language": "en",
        "headlines": [_line()],
        "long_headlines": [_line("Every safety data sheet current, on every site")],
        "descriptions": [_line("SDS updates within 24 hours.", claim_ids=[str(LICENSED)])],
        "business_name": _line("Example SDS"),
    }
    group.update(overrides)
    return group


def test_a_complete_asset_group_validates() -> None:
    assert AssetGroupText.model_validate(_group(), context=CONTEXT)


def test_an_asset_group_description_carries_a_licensed_claim() -> None:
    with pytest.raises(ValidationError):
        AssetGroupText.model_validate(
            _group(descriptions=[_line("One library.", claim_ids=[])]), context=CONTEXT
        )
    with pytest.raises(ValidationError, match="not licensed"):
        AssetGroupText.model_validate(
            _group(descriptions=[_line("One library.", claim_ids=[str(uuid.UUID(int=9))])]),
            context=CONTEXT,
        )
    with pytest.raises(ValidationError, match="context"):
        AssetGroupText.model_validate(_group())


def test_text_that_failed_lint_is_not_asset_group_text() -> None:
    failed = {**LINT, "verdict": "fail"}
    with pytest.raises(ValidationError, match="lint"):
        AssetGroupText.model_validate(
            _group(business_name=_line("Example SDS", lint=failed)), context=CONTEXT
        )


def test_not_required_carries_no_asset_group_and_required_at_least_one() -> None:
    assert AssetGroupTextOutput(status="not_required", why="no asset-group type on the slate")
    with pytest.raises(ValidationError):
        AssetGroupTextOutput(status="required", why="the slate has performance_max")
    with pytest.raises(ValidationError):
        AssetGroupTextOutput.model_validate(
            {"status": "not_required", "why": "none", "asset_groups": [_group()]},
            context=CONTEXT,
        )
    assert AssetGroupTextOutput.model_validate(
        {"status": "required", "why": "the slate has performance_max", "asset_groups": [_group()]},
        context=CONTEXT,
    )
