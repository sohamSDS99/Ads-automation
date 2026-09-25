"""4.2.4 `variant_b` — the contract and the parts that decide (PRD §11 4.2.4, §12.2).

The node's database and model work is proved in
`tests/integration/test_s4p6_descriptions_variant_b.py`. What is proved here is
what makes B a second RSA and not a rewording of the first:

* **a B that paraphrases A fails validation** — `distinctness_vs_a` under
  `variant_min_distance` is a schema error, not a warning;
* B needs a hypothesis and a primary metric, and the ad B carries is built
  from B's own copy, as judged by 4.2.3's path;
* `ResponsiveSearchAd` (§12.2): a B carries a hypothesis and its distance from A.
"""

from __future__ import annotations

import uuid
from itertools import combinations
from typing import Any

import pytest
from pydantic import ValidationError

from agent.creative import metrics
from agent.nodes.creative.n4_2_4_variant_b import ad_ref, ad_texts, hypothesis
from agent.schemas.creative_brief import AdGroupBrief
from agent.schemas.search_ads import (
    DescriptionGroup,
    HeadlineGroup,
    PairReport,
    ResponsiveSearchAd,
    VariantBGroup,
)

LICENSED = uuid.UUID(int=201)
CONTEXT = {"licensed_claim_ids": frozenset({LICENSED})}
HEADS = [uuid.UUID(int=300 + i) for i in range(3)]
RESERVE_HEAD = uuid.UUID(int=310)
DESCS = [uuid.UUID(int=400 + i) for i in range(2)]
LINT = {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []}


def _headlines() -> dict[str, Any]:
    texts = ["Stop Chasing Old Sheets", "Inspections Without Panic", "Book A Walkthrough"]
    candidates = [
        {
            "asset_id": str(asset_id),
            "text": text,
            "default_text": text,
            "category": "benefit",
            "dki": False,
            "lint": LINT,
            "outcome": "selected",
        }
        for asset_id, text in zip(HEADS, texts, strict=True)
    ]
    candidates.append(
        {
            "asset_id": str(RESERVE_HEAD),
            "text": "Inspection Day, Sorted",
            "default_text": "Inspection Day, Sorted",
            "category": "benefit",
            "dki": False,
            "lint": LINT,
            "outcome": "reserve",
        }
    )
    return {
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "campaign_type": "search",
        "market": "*",
        "language": "en",
        "variant": "B",
        "candidates": candidates,
        "selected": [str(h) for h in HEADS],
        "reserve": [str(RESERVE_HEAD)],
        "quota_report": {"lines": [], "limit": 15, "selected": 3, "met": True},
        "near_duplicate_trigram": 0.8,
    }


def _descriptions() -> dict[str, Any]:
    text = "Inspectors ask, you answer: SDS updates within 24 hours."
    return {
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "variant": "B",
        "descriptions": [
            {
                "asset_id": str(asset_id),
                "text": text,
                "claim_ids": [str(LICENSED)],
                "claim_span": [28, 55],
                "lint": LINT,
            }
            for asset_id in DESCS
        ],
        "paths": ["sds", "inspections"],
    }


def _report(heads: list[uuid.UUID] = HEADS, descs: list[uuid.UUID] = DESCS) -> dict[str, Any]:
    kinds = {**{h: "H" for h in heads}, **{d: "D" for d in descs}}
    pairs = [
        {"a": str(a), "b": str(b), "kind": kinds[a] + kinds[b], "label": "reads_well"}
        for a, b in combinations([*heads, *descs], 2)
    ]
    return {
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "variant": "B",
        "headlines": [str(h) for h in heads],
        "descriptions": [str(d) for d in descs],
        "pairs": pairs,
        "repair_rounds": 0,
    }


HYPOTHESIS = "Variant B, led by “inspections”, beats variant A on cost per lead."


def _ad(**overrides: Any) -> dict[str, Any]:
    ad: dict[str, Any] = {
        "ad_ref": "c-sds-us/sds software/B",
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "variant": "B",
        "angle": "Pass every inspection without a scramble",
        "hypothesis": HYPOTHESIS,
        "headlines": [str(h) for h in HEADS],
        "descriptions": [str(d) for d in DESCS],
        "paths": ["sds", "inspections"],
        "final_url": "https://example.com/sds",
        "pair_report": _report(),
        "distinctness_vs_a": 0.71,
    }
    ad.update(overrides)
    return ad


def _group(**overrides: Any) -> dict[str, Any]:
    group: dict[str, Any] = {
        "campaign_ref": "c-sds-us",
        "ad_group_ref": "sds software",
        "headlines": _headlines(),
        "descriptions": _descriptions(),
        "ad_b": _ad(),
        "distinctness_vs_a": 0.71,
        "variant_min_distance": 0.65,
        "hypothesis": HYPOTHESIS,
        "primary_metric": "cost per lead",
    }
    group.update(overrides)
    return group


def _validate(**overrides: Any) -> VariantBGroup:
    return VariantBGroup.model_validate(_group(**overrides), context=CONTEXT)


# ---------------------------------------------------------------------------
# a B that paraphrases A fails validation
# ---------------------------------------------------------------------------


def test_a_distinct_b_validates() -> None:
    group = _validate()
    assert group.ad_b.variant == "B" and group.distinctness_vs_a >= group.variant_min_distance


def test_a_b_that_paraphrases_a_fails_validation() -> None:
    with pytest.raises(ValidationError, match="paraphrases A"):
        _validate(distinctness_vs_a=0.20, ad_b=_ad(distinctness_vs_a=0.20))


def test_the_minimum_distance_itself_is_distinct_enough() -> None:
    assert _validate(distinctness_vs_a=0.65, ad_b=_ad(distinctness_vs_a=0.65))


def test_the_ad_and_its_group_report_one_distance_and_one_hypothesis() -> None:
    with pytest.raises(ValidationError, match="distinctness_vs_a"):
        _validate(ad_b=_ad(distinctness_vs_a=0.90))
    with pytest.raises(ValidationError, match="hypothesis"):
        _validate(ad_b=_ad(hypothesis="Something else entirely."))


# ---------------------------------------------------------------------------
# a hypothesis and a primary metric are required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["hypothesis", "primary_metric"])
def test_b_needs_a_hypothesis_and_a_primary_metric(field: str) -> None:
    group = _group()
    del group[field]
    with pytest.raises(ValidationError):
        VariantBGroup.model_validate(group, context=CONTEXT)
    with pytest.raises(ValidationError):
        _validate(**{field: "   "})


# ---------------------------------------------------------------------------
# B is B's own copy, through 4.2.3's path
# ---------------------------------------------------------------------------


def test_the_ad_is_variant_b() -> None:
    with pytest.raises(ValidationError):
        _validate(ad_b=_ad(variant="A", pair_report={**_report(), "variant": "A"}))


def test_the_copy_is_variant_b() -> None:
    with pytest.raises(ValidationError, match="variant B"):
        _validate(headlines={**_headlines(), "variant": "A"})
    with pytest.raises(ValidationError, match="variant B"):
        _validate(descriptions={**_descriptions(), "variant": "A"})


def test_the_ad_carries_only_bs_own_assets() -> None:
    borrowed = uuid.UUID(int=999)  # an A headline, say
    heads = [HEADS[0], HEADS[1], borrowed]
    with pytest.raises(ValidationError, match="not B's own"):
        _validate(ad_b=_ad(headlines=[str(h) for h in heads], pair_report=_report(heads=heads)))


def test_a_swapped_in_reserve_is_bs_own() -> None:
    heads = [HEADS[0], HEADS[1], RESERVE_HEAD]
    assert _validate(ad_b=_ad(headlines=[str(h) for h in heads], pair_report=_report(heads=heads)))


def test_the_group_names_one_ad_group() -> None:
    with pytest.raises(ValidationError, match="ad group"):
        _validate(ad_group_ref="another group")


def test_the_descriptions_are_still_claim_bound() -> None:
    with pytest.raises(ValidationError, match="not licensed"):
        VariantBGroup.model_validate(
            _group(), context={"licensed_claim_ids": frozenset({uuid.UUID(int=1)})}
        )


# ---------------------------------------------------------------------------
# ResponsiveSearchAd (§12.2)
# ---------------------------------------------------------------------------


def test_an_rsa_b_carries_a_hypothesis_and_its_distance() -> None:
    assert ResponsiveSearchAd.model_validate(_ad())
    with pytest.raises(ValidationError, match="hypothesis"):
        ResponsiveSearchAd.model_validate(_ad(hypothesis=None))
    with pytest.raises(ValidationError, match="hypothesis"):
        ResponsiveSearchAd.model_validate(_ad(hypothesis=" "))
    with pytest.raises(ValidationError, match="distinctness_vs_a"):
        ResponsiveSearchAd.model_validate(_ad(distinctness_vs_a=None))


def test_an_rsa_a_has_no_distance_from_itself() -> None:
    a = _ad(variant="A", hypothesis=None, distinctness_vs_a=None)
    a["pair_report"] = {**a["pair_report"], "variant": "A"}
    assert ResponsiveSearchAd.model_validate(a)
    with pytest.raises(ValidationError, match="distinctness_vs_a"):
        ResponsiveSearchAd.model_validate({**a, "distinctness_vs_a": 0.5})


def test_an_rsa_carries_exactly_the_combination_it_was_judged_as() -> None:
    with pytest.raises(ValidationError, match="pair_report"):
        ResponsiveSearchAd.model_validate(_ad(headlines=[str(h) for h in HEADS[:2]]))
    with pytest.raises(ValidationError, match="pair_report"):
        ResponsiveSearchAd.model_validate(_ad(pair_report={**_report(), "variant": "A"}))
    with pytest.raises(ValidationError, match="pair_report"):
        ResponsiveSearchAd.model_validate(_ad(ad_group_ref="another group"))


# ---------------------------------------------------------------------------
# the parts of the node that decide
# ---------------------------------------------------------------------------


def _brief_group() -> AdGroupBrief:
    source = [{"stage": "S2", "node_id": "2.4.2"}]
    return AdGroupBrief.model_validate(
        {
            "campaign_ref": "c-sds-us",
            "ad_group_ref": "sds software",
            "theme": "SDS management",
            "primary_message": {"text": "Keep every SDS current", "sources": source},
            "landing_url": "https://example.com/sds",
            "kpi": "cost per qualified lead",
            "angle_b": {"text": "Pass every inspection without a scramble", "sources": source},
        }
    )


def test_the_hypothesis_is_stated_in_the_briefs_own_words() -> None:
    text = hypothesis(_brief_group())
    assert "Pass every inspection without a scramble" in text
    assert "Keep every SDS current" in text
    assert "cost per qualified lead" in text


def test_the_ad_ref_names_campaign_ad_group_and_variant() -> None:
    assert ad_ref("c-sds-us", "sds software", "B") == "c-sds-us/sds software/B"


def test_the_texts_an_ad_carries_are_its_headlines_then_its_descriptions() -> None:
    headlines = HeadlineGroup.model_validate(_headlines())
    descriptions = DescriptionGroup.model_validate(_descriptions(), context=CONTEXT)
    heads = [HEADS[2], RESERVE_HEAD]
    report = PairReport.model_validate(_report(heads=heads, descs=DESCS[:1]))
    assert ad_texts(report, headlines, descriptions) == [
        "Book A Walkthrough",
        "Inspection Day, Sorted",
        "Inspectors ask, you answer: SDS updates within 24 hours.",
    ]


def test_distinctness_is_measured_on_what_the_ads_carry() -> None:
    same = ["Keep Every SDS Current", "SDS updates within 24 hours."]
    assert metrics.distinctness(same, same) == 0.0
    reworded = ["Keep Each SDS Current", "SDS updates inside 24 hours."]
    assert metrics.distinctness(same, reworded) < 0.65
