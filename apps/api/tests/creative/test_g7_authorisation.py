"""What approving a brief authorises, in numbers (Stage 04 PRD §15.4 D, S4-P18).

"Authorises 20 RSAs, 36 images, 4 videos and up to $38.40 of media spend" is
G7's own reading of the brief it hashes: RSAs from the ad groups the brief
covers, media from the plan `calc/` priced — never a second computation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from agent.creative import brief as briefs
from agent.creative import g7
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_input import CreativeInput
from tests.creative.helpers import creative_input
from tests.creative.test_brief import CALC_IDS, _draft, _parts

PRICED = {
    "jobs": {"image": 36, "video": 4},
    "media_usd": "38.40",
    "total_usd": "41.10",
    "confidence": "medium",
    "fits": True,
}


def _typed(campaign_type: str) -> CreativeInput:
    fields = creative_input().model_dump(mode="json")
    for campaign in fields["account_structure"]["campaigns"]:
        campaign["type"] = campaign_type
    return CreativeInput.model_validate(fields)


def _brief(inp: CreativeInput, estimate: dict[str, Any]) -> CreativeBrief:
    parts = _parts(inp)
    return briefs.assemble(
        _draft(parts),
        inp=inp,
        menu=parts["menu"],
        claims=parts["claims"],
        slots=parts["slots"],
        non_negotiable=briefs.non_negotiables(inp),
        visual=briefs.visual_constraints(inp, briefs.product_depiction(inp, {})),
        plan=briefs.media_plan(inp, estimate=estimate, ratio_plan={}, calc_evidence_ids=CALC_IDS),
    )


def test_two_rsas_per_search_ad_group_and_the_media_plan_as_priced() -> None:
    inp = _typed("search")
    brief = _brief(inp, PRICED)
    assert brief.ad_groups, "the fixture plan has a Search ad group"

    authorised = g7.authorises(brief, inp)

    assert authorised.rsas == g7.RSAS_PER_AD_GROUP * len(brief.ad_groups)
    assert (authorised.images, authorised.videos) == (36, 4)
    assert authorised.media_usd == Decimal("38.40")


@pytest.mark.parametrize("campaign_type", ["performance_max", ""])
def test_a_campaign_that_carries_no_rsas_authorises_none(campaign_type: str) -> None:
    inp = _typed(campaign_type)
    assert g7.authorises(_brief(inp, PRICED), inp).rsas == 0


def test_a_text_only_brief_authorises_no_media() -> None:
    inp = _typed("search")
    authorised = g7.authorises(
        _brief(inp, {**PRICED, "jobs": {"image": 0, "video": 0}, "media_usd": "0.00"}), inp
    )
    assert (authorised.images, authorised.videos, authorised.media_usd) == (0, 0, Decimal("0.00"))
