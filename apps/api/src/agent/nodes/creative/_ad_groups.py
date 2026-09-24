"""The pinned plan's ad groups the approved brief covers — where Stage 4.2 writes copy.

Every Stage 4.2 copy node writes per ad group of the G7-approved brief, and
each needs the same three things for it: the brief's lines, the campaign and
the plan's ad group (its keywords, market and language). One lookup, so 4.2.1,
4.2.2 and 4.2.4 agree on which ad groups are Search ones and 4.2.5 on which
are asset groups, and a brief naming something the pinned plan does not have
fails the same way everywhere.

The leading underscore keeps `registry.discover()` from walking this module.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.export.plan_contract import PlannedAdGroup, PlannedCampaign
from agent.nodes.base import NodeContractError
from agent.schemas.creative_brief import AdGroupBrief, CreativeBrief
from agent.schemas.guardrails import ClaimRef
from agent.schemas.search_ads import Variant

SEARCH = "search"
#: What `LintTarget` gets when the plan names no market or language — the
#: same fallback as Stage 03's lint route (`routes_guidelines.py`).
UNKNOWN_MARKET = "*"
DEFAULT_LANGUAGE = "en"

#: Appended to a copy node's system prompt for variant B (4.2.4): the same rules,
#: a different lead, and A's copy shown so that B is a second message rather than
#: a rewording of the first.
VARIANT_B_RULES = """
This is variant B of an A/B test. Lead with the ad group's angle_b, not with the
message of VARIANT A. VARIANT A is the copy already written for this ad group:
write nothing that says what one of its lines says in other words.
"""


@dataclass(frozen=True, slots=True)
class Slot:
    brief: AdGroupBrief
    campaign: PlannedCampaign
    group: PlannedAdGroup

    @property
    def campaign_type(self) -> str:
        return self.campaign.type.strip().lower()

    @property
    def keywords(self) -> tuple[str, ...]:
        terms = (keyword.term.strip() for keyword in self.group.keywords)
        return tuple(dict.fromkeys(term for term in terms if term))

    @property
    def market(self) -> str:
        return (self.group.market or self.campaign.market or "").strip() or UNKNOWN_MARKET

    @property
    def language(self) -> str:
        return (self.campaign.language or "").strip() or DEFAULT_LANGUAGE


def slots(
    brief: CreativeBrief, campaigns: Sequence[PlannedCampaign], *, types: Collection[str]
) -> list[Slot]:
    """The brief's ad groups that sit in a campaign of one of `types`, in brief order."""
    by_ref = {(campaign.campaign_ref or campaign.name): campaign for campaign in campaigns}
    found: list[Slot] = []
    for item in brief.ad_groups:
        campaign = by_ref.get(item.campaign_ref)
        if campaign is None:
            raise NodeContractError(
                f"the brief names campaign {item.campaign_ref!r}, which the pinned plan "
                f"does not have"
            )
        if campaign.type.strip().lower() not in types:
            continue
        group = next((g for g in campaign.ad_groups if g.name == item.ad_group_ref), None)
        if group is None:
            raise NodeContractError(
                f"the brief names ad group {item.ad_group_ref!r} in {item.campaign_ref!r}, "
                f"which the pinned plan does not have"
            )
        found.append(Slot(brief=item, campaign=campaign, group=group))
    return found


def search_slots(brief: CreativeBrief, campaigns: Sequence[PlannedCampaign]) -> list[Slot]:
    """The brief's ad groups that sit in a Search campaign — an RSA exists nowhere else."""
    return slots(brief, campaigns, types=(SEARCH,))


def where(slot: Slot) -> str:
    return f"{slot.brief.campaign_ref} / {slot.brief.ad_group_ref}"


# ---------------------------------------------------------------------------
# the sections every copy prompt shows, built one way
# ---------------------------------------------------------------------------


def ad_group_section(slot: Slot, variant: Variant = "A") -> dict[str, Any]:
    """The ad group a prompt writes for. A leads with its primary message, B with `angle_b`."""
    lead = (
        {"primary_message": slot.brief.primary_message.text}
        if variant == "A"
        else {"angle_b": slot.brief.angle_b.text}
    )
    return {
        "campaign": slot.brief.campaign_ref,
        "ad_group": slot.brief.ad_group_ref,
        "theme": slot.brief.theme,
        **lead,
        "landing_url": str(slot.brief.landing_url),
        "language": slot.language,
    }


def brief_section(brief: CreativeBrief) -> dict[str, Any]:
    return {
        "objective": brief.objective.text,
        "audience": [line.text for line in brief.audience],
        "angle": brief.angle.text,
        "offer": (
            f"an offer on {brief.offer.sku_or_set} exists; name it in words, never figures"
            if brief.offer is not None
            else "none"
        ),
    }


def proof_points(licensed: Sequence[ClaimRef]) -> list[dict[str, str]]:
    """The claims the pin licenses — the only facts copy may state (law 34)."""
    return [{"claim_id": str(claim.claim_id), "text": claim.normalized_text} for claim in licensed]


def render_prompt(sections: Mapping[str, Any]) -> str:
    return "\n\n".join(
        f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=1)}"
        for name, value in sections.items()
    )
