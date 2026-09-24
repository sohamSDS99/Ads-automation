"""4.2.5 `asset_group_text` — Performance Max, Demand Gen and Display text (PRD §11 4.2.5).

**`not_required` when the slate has none of those types** — a complete, normal
outcome, read from `CreativeInput.channel_slate`: a Search-only slate has no
asset group to write for, so no model is asked and no asset is written. The
same answer, saying why, when the slate has one but the run's brief covers no
asset group in a campaign of that type (a scoped run, or a plan with none).

Otherwise, per asset group the approved brief covers in a campaign of a type on
the slate:

1. **The specs come from the pin** — `headline`, `long_headline`,
   `description` and `business_name` for the campaign type, with the limits
   the prompt states and the counts the schema takes. A missing one is
   `spec_missing`, naming every gap at once; Stage 04 never guesses a Google
   limit (§9.5).
2. **COPYWRITE writes the set** — exactly each spec's `max_count` of
   headlines, long headlines and descriptions, and the business name, answering
   a schema in which every description's `claim_ids` is a non-empty list from
   an enum of the claims the pin licenses: a description is claim-bound
   wherever it runs (law 34; §12.2 "min 1 for kind='description'").
3. **Every line is linted at creation** through `lint_adapter` (law 33), as the
   surface whose Stage 03 asset type is the spec it was written against
   (`SURFACES`), so the pin's limits for that type apply to it and no others.
4. **What the lint found decides**, as in 4.2.2: an unlicensed claim-shaped
   span withholds the line whole — never an asset — and becomes an exception
   candidate; any other failure stays `draft`; a pass is `linted`. Fewer
   passing than a spec's `min_count` fails the node.

Nothing is written until every asset group has been built; an attempt that
follows a failed one first clears what that attempt wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.creative import exceptions
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    RunStage,
)
from agent.export.plan_contract import ChannelSlate
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._ad_groups import (
    Slot,
    brief_section,
    proof_points,
    render_prompt,
    slots,
    where,
)
from agent.nodes.creative._text_assets import (
    clear_earlier_attempts,
    lint_ref,
    required_specs,
    text_asset,
)
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import licensed_or_fail, screen
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintResult, LintTarget, RuleSet
from agent.schemas.search_ads import AssetGroupText, AssetGroupTextOutput

NODE_ID = "4.2.5"

#: §11 4.2.5: "Per PMax / Demand Gen / Display asset group". Plan vocabulary
#: (`stage_2_3.CampaignType`).
TYPES: Final = ("performance_max", "demand_gen", "display")
NAMES: Final[Mapping[str, str]] = {
    "performance_max": "Performance Max",
    "demand_gen": "Demand Gen",
    "display": "Display",
}

#: The spec each text is written against, and the fields it needs from it.
NEEDS: Final[Mapping[str, tuple[str, ...]]] = {
    "headline": ("max_chars", "max_count"),
    "long_headline": ("max_chars", "max_count"),
    "description": ("max_chars", "max_count"),
    "business_name": ("max_chars",),
}

#: The surface each text is linted as. Stage 03 names Performance Max's text
#: surfaces and no Demand Gen or Display ones, so those two take the surfaces
#: whose asset type (`SURFACE_ASSET_TYPES`) is the spec they are written
#: against: the pin's limits for *their* campaign type then apply, because a
#: spec rule is scoped by campaign type and asset type, not by surface name.
#: `display_text` is not used: it has no asset type, so every asset-typed rule
#: would apply to it at once. Owed a ruling (docs/stage-04-questions.md § S4-P6).
SURFACES: Final[Mapping[str, Mapping[str, str]]] = {
    "performance_max": {
        "headline": "pmax_headline",
        "long_headline": "long_headline",
        "description": "pmax_description",
        "business_name": "business_name",
    },
    "demand_gen": {
        "headline": "pmax_headline",
        "long_headline": "long_headline",
        "description": "asset_group_description",
        "business_name": "business_name",
    },
    "display": {
        "headline": "pmax_headline",
        "long_headline": "long_headline",
        "description": "asset_group_description",
        "business_name": "business_name",
    },
}

KINDS: Final[Mapping[str, CreativeAssetKind]] = {
    "headline": CreativeAssetKind.HEADLINE,
    "long_headline": CreativeAssetKind.LONG_HEADLINE,
    "description": CreativeAssetKind.DESCRIPTION,
    "business_name": CreativeAssetKind.BUSINESS_NAME,
}

SYSTEM = """You write the text assets of one Google {type_name} asset group. Google's
automation combines them, so every line must read well on its own and beside any
other.

Rules, all of them hard:
- Write exactly {headlines} headlines of at most {headline_chars} characters,
  exactly {long_headlines} long headlines of at most {long_headline_chars}
  characters, exactly {descriptions} descriptions of at most {description_chars}
  characters, and the business name in at most {business_name_chars} characters.
  Count characters, spaces included.
- Every description states at least one claim from PROOF POINTS and cites its
  claim_id in claim_ids. No other line asserts a fact about the product.
- Write no numbers, prices, percentages, dates or deadlines that a cited claim
  does not state.
- The business name is the advertiser's name as its customers know it.
- No exclamation marks. Plain, specific language in the brand's voice. Never use a
  word from NEVER.
"""


# ---------------------------------------------------------------------------
# the part that decides — pure, and tested on its own
# ---------------------------------------------------------------------------


def slate_types(slate: ChannelSlate) -> set[str]:
    """The campaign types the frozen plan's channel slate runs."""
    return {entry.campaign_type.strip().lower() for entry in slate.slate}


def not_required_reason(slate: Collection[str], *, groups: int) -> str | None:
    """Why there is no asset-group text to write, or None when there is."""
    wanted = [campaign_type for campaign_type in TYPES if campaign_type in slate]
    if not wanted:
        return (
            "the channel slate has no Performance Max, Demand Gen or Display campaign, so "
            "there is no asset group to write text for"
        )
    if not groups:
        return (
            f"the channel slate has {', '.join(wanted)}, but the approved brief covers no "
            "asset group in a campaign of that type in this run's scope"
        )
    return None


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(licensed: Sequence[uuid.UUID], *, counts: Mapping[str, int]) -> type[BaseModel]:
    """The set's schema: exact counts, and descriptions that cite a licensed claim."""
    if not licensed:
        raise ValueError(
            "no claim is licensed at the pin, so no description can carry one (law 34)"
        )
    line = Annotated[str, Field(min_length=1)]
    claim_ids: Any = list[Literal[tuple(str(claim) for claim in licensed)]]  # type: ignore[misc]
    description = create_model(
        "AssetGroupDescriptionDraft",
        __base__=_Draft,
        text=(str, Field(min_length=1)),
        claim_ids=(claim_ids, Field(min_length=1)),
    )

    def exactly(count: int) -> Any:
        return Field(min_length=count, max_length=count)

    return create_model(
        "AssetGroupTextDraft",
        __base__=_Draft,
        headlines=(list[line], exactly(counts["headline"])),
        long_headlines=(list[line], exactly(counts["long_headline"])),
        descriptions=(list[description], exactly(counts["description"])),
        business_name=(str, Field(min_length=1)),
    )


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Built:
    group: AssetGroupText
    rows: list[CreativeAsset]


class AssetGroupTextNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="asset_group_text",
        stage="4.2",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.1.1",),
        task_class=TaskClass.COPYWRITE,
        input_model=CreativeBrief,
        output_model=AssetGroupTextOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # The brief, the plan's channel slate and account structure, the pin.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        await clear_earlier_attempts(ctx, NODE_ID)
        slate = slate_types(creative.input.channel_slate)
        wanted = [campaign_type for campaign_type in TYPES if campaign_type in slate]
        found = (
            slots(brief, creative.input.account_structure.campaigns, types=wanted) if wanted else []
        )
        reason = not_required_reason(slate, groups=len(found))
        if reason is not None:
            return AssetGroupTextOutput(status="not_required", why=reason)

        # The run's start, not the wall clock, as in 4.2.1.
        now = ctx.run.started_at or datetime.now(UTC)
        ruleset = creative.linter.ruleset
        specs = _specs(ruleset, dict.fromkeys(slot.campaign_type for slot in found))
        licensed = licensed_or_fail(ruleset, now)
        built = [
            await _write(ctx, brief, slot, specs[slot.campaign_type], licensed, now)
            for slot in found
        ]

        for item in built:
            ctx.db.add_all(item.rows)
        await ctx.db.flush()
        types = sorted({slot.campaign_type for slot in found})
        return AssetGroupTextOutput(
            status="required",
            why=f"{len(built)} asset group(s) in the slate's {', '.join(types)} campaigns",
            asset_groups=[item.group for item in built],
        )


def _specs(ruleset: RuleSet, types: Collection[str]) -> dict[str, dict[str, AssetSpec]]:
    """Every type's specs, or one failure naming every type's gaps."""
    found: dict[str, dict[str, AssetSpec]] = {}
    gaps: list[str] = []
    for campaign_type in types:
        try:
            found[campaign_type] = required_specs(ruleset, campaign_type, NEEDS)
        except NodeContractError as exc:
            gaps.append(str(exc))
    if gaps:
        raise NodeContractError("; ".join(gaps))
    return found


async def _write(
    ctx: RunContext,
    brief: CreativeBrief,
    slot: Slot,
    specs: Mapping[str, AssetSpec],
    licensed: list[ClaimRef],
    now: datetime,
) -> Built:
    creative = ctx.require_creative()
    ruleset = creative.linter.ruleset
    campaign_type = slot.campaign_type
    surfaces = SURFACES[campaign_type]
    counts = {name: int(specs[name].max_count or 0) for name in NEEDS if name != "business_name"}
    licensed_ids = [claim.claim_id for claim in licensed]

    draft: Any = await ctx.complete(
        draft_model(licensed_ids, counts=counts),
        system=SYSTEM.format(
            type_name=NAMES[campaign_type],
            headlines=counts["headline"],
            long_headlines=counts["long_headline"],
            descriptions=counts["description"],
            **{f"{name}_chars": specs[name].max_chars for name in NEEDS},
        ),
        user=_user_prompt(brief, slot, licensed),
        task_class=TaskClass.COPYWRITE,
    )

    written: list[tuple[str, str, list[uuid.UUID]]] = [
        *(("headline", text, []) for text in draft.headlines),
        *(("long_headline", text, []) for text in draft.long_headlines),
        *(
            (
                "description",
                item.text,
                list(dict.fromkeys(uuid.UUID(str(claim)) for claim in item.claim_ids)),
            )
            for item in draft.descriptions
        ),
        ("business_name", draft.business_name, []),
    ]

    spans: list[str] = []
    passed: dict[str, list[dict[str, Any]]] = {name: [] for name in NEEDS}
    rows: list[CreativeAsset] = []
    for asset_type, text, claim_ids in written:
        asset_id = uuid.uuid4()
        surface = surfaces[asset_type]
        result: LintResult = creative.linter.lint_candidate(
            LintTarget(
                ref=str(asset_id),
                surface=surface,
                campaign_type=campaign_type,
                market=slot.market,
                language=slot.language,
                text=text,
                generated_by_ai=True,
            ),
            now=now,
        )
        unlicensed = exceptions.unlicensed_spans(result, text=text, ruleset=ruleset)
        fate = screen(result.verdict, unlicensed=unlicensed, quoted=True)
        if fate == "exception":
            # Law 34: withheld whole, never an asset.
            spans.extend(unlicensed)
            continue
        if fate == "eligible":
            passed[asset_type].append(
                {
                    "asset_id": asset_id,
                    "text": text,
                    "lint": lint_ref(result),
                    **({"claim_ids": claim_ids} if asset_type == "description" else {}),
                }
            )
        rows.append(
            text_asset(
                ctx,
                asset_id=asset_id,
                node_id=NODE_ID,
                campaign_ref=slot.brief.campaign_ref,
                ad_group_ref=slot.brief.ad_group_ref,
                kind=KINDS[asset_type],
                surface=surface,
                variant=None,
                category=None,
                text=text,
                fields={},
                claim_ids=claim_ids,
                status=CreativeAssetStatus.LINTED
                if fate == "eligible"
                else CreativeAssetStatus.DRAFT,
                lint=result,
            )
        )

    short = [
        f"{len(passed[name])} {name}(s), the spec's minimum is {minimum}"
        for name in NEEDS
        if len(passed[name]) < (minimum := _minimum(name, specs[name]))
    ]
    if short:
        raise NodeContractError(
            f"{where(slot)} ({campaign_type}): too few lines passed lint — {'; '.join(short)}"
        )

    candidates = exceptions.candidates(spans)
    group = AssetGroupText.model_validate(
        {
            "campaign_ref": slot.brief.campaign_ref,
            "ad_group_ref": slot.brief.ad_group_ref,
            "campaign_type": campaign_type,
            "market": slot.market,
            "language": slot.language,
            "headlines": passed["headline"],
            "long_headlines": passed["long_headline"],
            "descriptions": passed["description"],
            "business_name": passed["business_name"][0],
            "exception_candidates": candidates,
        },
        context={"licensed_claim_ids": frozenset(licensed_ids)},
    )
    await ctx.progress(
        f"{where(slot)} ({campaign_type}): "
        + ", ".join(f"{len(passed[name])} {name}(s)" for name in NEEDS)
        + f" passed lint; {sum(c.occurrences for c in candidates)} unlicensed claim span(s) "
        f"withheld as {len(candidates)} exception candidate(s)"
    )
    return Built(group=group, rows=rows)


def _minimum(asset_type: str, spec: AssetSpec) -> int:
    """How many must pass: the spec's `min_count`; one headline, description and name at least."""
    floor = 0 if asset_type == "long_headline" else 1
    return max(spec.min_count or 0, floor)


def _user_prompt(brief: CreativeBrief, slot: Slot, licensed: Sequence[ClaimRef]) -> str:
    return render_prompt(
        {
            "ASSET GROUP": {
                "campaign": slot.brief.campaign_ref,
                "asset_group": slot.brief.ad_group_ref,
                "campaign_type": NAMES[slot.campaign_type],
                "theme": slot.brief.theme,
                "primary_message": slot.brief.primary_message.text,
                "landing_url": str(slot.brief.landing_url),
                "language": slot.language,
            },
            "BRIEF": brief_section(brief),
            "KEYWORDS": list(slot.keywords),
            "PROOF POINTS": proof_points(licensed),
            "VOICE": list(brief.non_negotiables.voice_words),
            "NEVER": list(brief.non_negotiables.never_terms),
        }
    )


ASSET_GROUP_TEXT = AssetGroupTextNode()
