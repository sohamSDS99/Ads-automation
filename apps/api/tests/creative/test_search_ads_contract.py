"""`schemas/search_ads.py` — the 4.2.1, 4.2.2 and 4.2.3 contracts (PRD §11 4.2, §12.2).

The rules the PRD states as validators are validators here, so an output that
breaks them cannot be constructed: DKI `{KeyWord:default}` is measured on its
default text; a description with no licensed claim fails schema; a pin exists
only for an `order_dependent` pair.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from agent.schemas.search_ads import (
    DKI_TOKENS,
    ClaimBoundDescriptionsOutput,
    DkiError,
    HeadlineCandidate,
    PairReport,
    dki_default,
    render_default,
)

LICENSED = uuid.UUID(int=201)
UNLICENSED = uuid.UUID(int=202)


# ---------------------------------------------------------------------------
# dynamic keyword insertion
# ---------------------------------------------------------------------------


def test_the_default_text_is_what_a_reader_sees_when_no_keyword_fits() -> None:
    text = "Buy {KeyWord:SDS Software} Today"
    assert dki_default(text) == "SDS Software"
    assert render_default(text) == "Buy SDS Software Today"


def test_a_headline_without_insertion_renders_as_itself() -> None:
    assert dki_default("Keep Every SDS Current") is None
    assert render_default("Keep Every SDS Current") == "Keep Every SDS Current"


@pytest.mark.parametrize("token", ["keyword", "Keyword", "KeyWord", "KEYWord", "KeyWORD"])
def test_every_capitalisation_google_documents_is_accepted(token: str) -> None:
    assert render_default(f"{{{token}:SDS Tools}} Online") == "SDS Tools Online"


def test_the_documented_capitalisations_are_exactly_googles_five() -> None:
    assert DKI_TOKENS == ("keyword", "Keyword", "KeyWord", "KEYWord", "KeyWORD")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{KEYWORD:SDS Tools} Online", "not a keyword insertion"),
        ("{kEyWoRd:SDS Tools}", "not a keyword insertion"),
        ("{LOCATION(City)} SDS Help", "not a keyword insertion"),
        ("{KeyWord:SDS} and {KeyWord:Tools}", "one keyword insertion"),
        ("{KeyWord:} Online", "empty default"),
        ("{KeyWord:   } Online", "empty default"),
        ("Buy {KeyWord:SDS Software Today", "unbalanced"),
        ("Buy SDS} Software", "unbalanced"),
    ],
)
def test_a_malformed_insertion_is_refused(text: str, message: str) -> None:
    with pytest.raises(DkiError, match=message):
        render_default(text)


def _candidate(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "asset_id": str(uuid.UUID(int=1)),
        "text": "Buy {KeyWord:SDS Software} Today",
        "default_text": "Buy SDS Software Today",
        "category": "keyword",
        "keyword_ref": "sds software",
        "claim_ids": [],
        "dki": True,
        "lint": {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []},
        "outcome": "selected",
        "reason": None,
    }
    fields.update(overrides)
    return fields


def test_a_candidate_carries_its_default_text() -> None:
    assert HeadlineCandidate.model_validate(_candidate()).default_text == "Buy SDS Software Today"


def test_a_candidate_whose_default_text_is_not_its_rendering_is_refused() -> None:
    with pytest.raises(ValidationError, match="default_text"):
        HeadlineCandidate.model_validate(
            _candidate(default_text="Buy {KeyWord:SDS Software} Today")
        )


def test_a_candidate_whose_dki_flag_disagrees_with_its_text_is_refused() -> None:
    with pytest.raises(ValidationError, match="dki"):
        HeadlineCandidate.model_validate(_candidate(dki=False))


def test_a_selected_candidate_must_have_passed_lint() -> None:
    failed = {"verdict": "fail", "ruleset_version": "1.0+aa", "rule_ids": ["asset_spec.length.v1"]}
    with pytest.raises(ValidationError, match="lint"):
        HeadlineCandidate.model_validate(_candidate(lint=failed))
    assert HeadlineCandidate.model_validate(_candidate(lint=failed, outcome="failed_lint"))


# ---------------------------------------------------------------------------
# 4.2.2's output: a description with no licensed claim fails schema
# ---------------------------------------------------------------------------


def _description(claim_ids: list[str], **overrides: Any) -> dict[str, Any]:
    text = "SDS updates within 24 hours, across every site."
    fields: dict[str, Any] = {
        "asset_id": str(uuid.uuid4()),
        "text": text,
        "claim_ids": claim_ids,
        "claim_span": [0, 27],
        "lint": {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []},
    }
    fields.update(overrides)
    return fields


def _descriptions(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "ad_groups": [
            {
                "campaign_ref": "c-sds-us",
                "ad_group_ref": "sds software",
                "descriptions": list(items),
                "paths": ["sds", "software"],
                "reserve": [],
                "exception_candidates": [],
            }
        ]
    }


def _licensed() -> dict[str, Any]:
    return {"licensed_claim_ids": frozenset({LICENSED})}


def test_descriptions_bound_to_licensed_claims_validate() -> None:
    payload = _descriptions(_description([str(LICENSED)]))
    parsed = ClaimBoundDescriptionsOutput.model_validate(payload, context=_licensed())
    assert parsed.ad_groups[0].descriptions[0].claim_ids == [LICENSED]


def test_a_description_with_no_claim_fails_schema() -> None:
    with pytest.raises(ValidationError, match="claim_ids"):
        ClaimBoundDescriptionsOutput.model_validate(
            _descriptions(_description([])), context=_licensed()
        )


def test_a_description_citing_an_unlicensed_claim_fails_schema() -> None:
    with pytest.raises(ValidationError, match="not licensed"):
        ClaimBoundDescriptionsOutput.model_validate(
            _descriptions(_description([str(UNLICENSED)])), context=_licensed()
        )


def test_the_licence_check_cannot_be_skipped_by_omitting_the_pin() -> None:
    with pytest.raises(ValidationError, match="licensed_claim_ids"):
        ClaimBoundDescriptionsOutput.model_validate(_descriptions(_description([str(LICENSED)])))


def test_a_claim_span_outside_the_text_is_refused() -> None:
    with pytest.raises(ValidationError, match="claim_span"):
        ClaimBoundDescriptionsOutput.model_validate(
            _descriptions(_description([str(LICENSED)], claim_span=[10, 500])),
            context=_licensed(),
        )


def test_more_than_four_descriptions_are_refused() -> None:
    with pytest.raises(ValidationError, match="descriptions"):
        ClaimBoundDescriptionsOutput.model_validate(
            _descriptions(*(_description([str(LICENSED)]) for _ in range(5))),
            context=_licensed(),
        )


# ---------------------------------------------------------------------------
# 4.2.3's pair report
# ---------------------------------------------------------------------------

H1, H2, H3, D1, R1 = (str(uuid.UUID(int=n)) for n in (11, 12, 13, 21, 31))


def _report(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "headlines": [H1, H2, R1],
        "descriptions": [D1],
        "pairs": [
            {"a": H1, "b": H2, "kind": "HH", "flags": [], "label": "order_dependent"},
            {"a": H1, "b": R1, "kind": "HH", "flags": [], "label": "reads_well"},
            {"a": H2, "b": R1, "kind": "HH", "flags": [], "label": "reads_well"},
            {"a": H1, "b": D1, "kind": "HD", "flags": [], "label": "reads_well"},
            {"a": H2, "b": D1, "kind": "HD", "flags": [], "label": "reads_well"},
            {"a": R1, "b": D1, "kind": "HD", "flags": [], "label": "reads_well"},
        ],
        "swaps": [{"out": H3, "in_from_reserve": R1, "why": "offer_conflict with " + D1}],
        "pins": [
            {"asset_id": H1, "position": "H1", "why": "order_dependent"},
            {"asset_id": H2, "position": "H2", "why": "order_dependent"},
        ],
        "repair_rounds": 1,
        "unresolved": [],
    }
    fields.update(overrides)
    return fields


def test_a_consistent_pair_report_validates() -> None:
    report = PairReport.model_validate(_report())
    assert report.flags_version == "combinatorics.pair_flags_v1"


def test_a_pin_on_a_pair_that_is_not_order_dependent_is_refused() -> None:
    pins = [{"asset_id": R1, "position": "H1", "why": "force the message"}]
    with pytest.raises(ValidationError, match="order_dependent"):
        PairReport.model_validate(_report(pins=pins))


def test_a_pin_on_a_flagged_order_dependent_pair_is_refused() -> None:
    report = _report()
    report["pairs"][0]["flags"] = ["near_duplicate"]
    report["unresolved"] = [[H1, H2]]
    with pytest.raises(ValidationError, match="order_dependent"):
        PairReport.model_validate(report)


def test_a_swap_whose_replacement_is_not_in_the_ad_is_refused() -> None:
    swaps = [{"out": H3, "in_from_reserve": str(uuid.UUID(int=99)), "why": "x"}]
    with pytest.raises(ValidationError, match="in_from_reserve"):
        PairReport.model_validate(_report(swaps=swaps))


def test_a_pair_naming_an_asset_outside_the_ad_is_refused() -> None:
    report = _report()
    report["pairs"][1]["b"] = H3
    with pytest.raises(ValidationError, match="not in the ad"):
        PairReport.model_validate(report)


def test_unresolved_must_be_exactly_the_bad_pairs() -> None:
    report = _report()
    report["pairs"][3]["label"] = "contradictory"
    with pytest.raises(ValidationError, match="unresolved"):
        PairReport.model_validate(report)
    report["unresolved"] = [[H1, D1]]
    assert PairReport.model_validate(report).unresolved == [(uuid.UUID(H1), uuid.UUID(D1))]


def test_more_than_one_repair_round_is_refused() -> None:
    with pytest.raises(ValidationError, match="repair_rounds"):
        PairReport.model_validate(_report(repair_rounds=2))
