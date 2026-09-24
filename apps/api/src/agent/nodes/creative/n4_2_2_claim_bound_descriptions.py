"""4.2.2 `claim_bound_descriptions` — every description stands on a licensed claim (PRD §11 4.2.2).

Law 34: "Every description carries >=1 claim_id licensed at the pin. An
unlicensed claim-shaped span never ships." For every Search ad group the
approved brief covers:

1. **COPYWRITE writes the pool** — exactly `copy.description_pool_size`
   descriptions and the ad's two display paths, answering a schema in which
   every description's `claim_ids` is a non-empty list drawn from an enum of the
   claims the pin licenses, and `claim_text` quotes the words that state them.
   The model cannot cite what code did not offer, and cannot hand back a
   description that stands on nothing.
2. **Each is linted at creation** through `lint_adapter` against the run's
   current pin (law 33): descriptions as `rsa_description`, paths as `rsa_path`.
3. **What the lint found decides** (`screen`). A candidate carrying an
   unlicensed claim-shaped span — a finding of Stage 03's claim-licence rule,
   read by `creative/exceptions.py` — is withheld whole: it never becomes an
   asset, draft or otherwise, and its spans become the ad group's
   `exception_candidates`. Any other lint failure stays `draft` (law 33). A
   passing description whose quote is not in its text is `dropped`: it does not
   say which of its words state its claim.
4. **Code selects** (`pick`) at most the spec's `max_count`
   (asset_specs.search.description; missing is `spec_missing`, never a guess)
   in written order, never two near-duplicates (`copy.trigram_v1` at or over
   `copy.near_duplicate_trigram`, the pair 4.2.3 would block). Every other
   eligible description is a `reserve` for 4.2.3's repair round.
5. **`claim_ids ⊆ licensed(pin)` is the output's validator.** Each ad group is
   validated through `DescriptionGroup` with the pin's licensed ids in context,
   so a description without a licensed claim cannot leave this node.

Nothing is written until every ad group has been built; an attempt that
follows a failed one first clears what that attempt wrote. `describe` is the
whole per-ad-group path, so 4.2.4 writes variant B's descriptions through it.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.creative import brief as briefs
from agent.creative import exceptions, metrics
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    Evidence,
    RunStage,
)
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._ad_groups import (
    SEARCH,
    VARIANT_B_RULES,
    Slot,
    ad_group_section,
    brief_section,
    proof_points,
    render_prompt,
    search_slots,
    where,
)
from agent.nodes.creative._text_assets import (
    clear_earlier_attempts,
    lint_ref,
    required_specs,
    text_asset,
)
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintResult, LintTarget, RuleSet
from agent.schemas.search_ads import (
    PASSING,
    ClaimBoundDescriptionsOutput,
    DescriptionGroup,
    Variant,
)

NODE_ID = "4.2.2"
SURFACE = "rsa_description"
PATH_SURFACE = "rsa_path"
#: §11 4.2.2 `paths[2]`: the two path fields of a responsive search ad's display URL.
PATHS = 2

Screen = Literal["exception", "failed_lint", "dropped", "eligible"]

SYSTEM = """You write the descriptions of one Google responsive search ad, and the two paths
of its display URL. Code picks the ad's descriptions from what you write.

Rules, all of them hard:
- Write exactly {pool} descriptions. Each is at most {max_chars} characters: count
  them, spaces included.
- Every description states at least one claim from PROOF POINTS and cites its
  claim_id in claim_ids. In claim_text, copy the words of the description that
  state the claim, exactly as they appear in it.
- Say nothing about the product that a cited claim does not say: no other facts,
  and no superlatives, comparisons, guarantees, certifications or endorsements of
  your own.
- Write no numbers, prices, percentages, dates or deadlines that a cited claim
  does not state.
- Write exactly 2 paths, each at most {path_chars} characters, from the ad group's
  keywords or theme.
- No exclamation marks. Plain, specific language in the brand's voice. Never use a
  word from NEVER.
"""


# ---------------------------------------------------------------------------
# the part that decides — pure, and tested on its own
# ---------------------------------------------------------------------------


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(licensed: Sequence[uuid.UUID], *, pool_size: int) -> type[BaseModel]:
    """The pool schema: every description cites at least one claim, and only a licensed one."""
    if not licensed:
        raise ValueError(
            "no claim is licensed at the pin, so no description can carry one (law 34)"
        )
    claim_ids: Any = list[Literal[tuple(str(claim) for claim in licensed)]]  # type: ignore[misc]
    item = create_model(
        "DescriptionDraft",
        __base__=_Draft,
        text=(str, Field(min_length=1)),
        claim_ids=(claim_ids, Field(min_length=1)),
        claim_text=(str, Field(min_length=1)),
    )
    return create_model(
        "DescriptionPoolDraft",
        __base__=_Draft,
        descriptions=(list[item], Field(min_length=pool_size, max_length=pool_size)),
        paths=(
            list[Annotated[str, Field(min_length=1)]],
            Field(min_length=PATHS, max_length=PATHS),
        ),
    )


def claim_span(text: str, quote: str) -> tuple[int, int] | None:
    """Where the quoted words sit in `text`, as `[start, end)`: literally, in any case."""
    words = quote.strip()
    if not words:
        return None
    found = re.search(re.escape(words), text, flags=re.IGNORECASE)
    return None if found is None else (found.start(), found.end())


def screen(verdict: str, *, unlicensed: Sequence[str], quoted: bool) -> Screen:
    """What becomes of one written candidate, in the order the laws put it."""
    if unlicensed:
        # Law 34: an unlicensed claim-shaped span never ships — not even as a draft.
        return "exception"
    if verdict not in PASSING:
        # Law 33: only a pass leaves draft.
        return "failed_lint"
    if not quoted:
        return "dropped"
    return "eligible"


def pick(
    written: Sequence[tuple[str, str]], *, limit: int, near_duplicate: float
) -> tuple[list[str], list[str]]:
    """`(selected, reserve)` refs: written order, never two at or over `near_duplicate`."""
    selected: list[tuple[str, str]] = []
    reserve: list[str] = []
    for ref, text in written:
        if len(selected) < limit and all(
            metrics.similarity(text, kept) < near_duplicate for _, kept in selected
        ):
            selected.append((ref, text))
        else:
            reserve.append(ref)
    return [ref for ref, _ in selected], reserve


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Built:
    group: DescriptionGroup
    rows: list[CreativeAsset]


class ClaimBoundDescriptionsNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="claim_bound_descriptions",
        stage="4.2",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.1.1",),
        task_class=TaskClass.COPYWRITE,
        input_model=CreativeBrief,
        output_model=ClaimBoundDescriptionsOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # Everything 4.2.2 reads is already pinned: the approved brief, the
        # ruleset's claims_index and asset_specs.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        slots = search_slots(brief, creative.input.account_structure.campaigns)
        await clear_earlier_attempts(ctx, NODE_ID)
        if not slots:
            return ClaimBoundDescriptionsOutput()

        # The run's start, not the wall clock, as in 4.2.1.
        now = ctx.run.started_at or datetime.now(UTC)
        ruleset = creative.linter.ruleset
        specs = description_specs(ruleset)
        licensed = licensed_or_fail(ruleset, now)
        built = [await describe(ctx, brief, slot, specs, licensed, now) for slot in slots]

        for item in built:
            ctx.db.add_all(item.rows)
        await ctx.db.flush()
        return ClaimBoundDescriptionsOutput(ad_groups=[item.group for item in built])


def description_specs(ruleset: RuleSet) -> dict[str, AssetSpec]:
    """`asset_specs.search.description` and `.path`, with what the prompt and selection need."""
    return required_specs(
        ruleset,
        SEARCH,
        {"description": ("max_chars", "max_count"), "path": ("max_chars",)},
    )


def licensed_or_fail(ruleset: RuleSet, now: datetime) -> list[ClaimRef]:
    """The pin's licensed claims, or a failure: a description with none cannot exist."""
    licensed = briefs.licensed_claims(ruleset, now)
    if not licensed:
        raise NodeContractError(
            f"ruleset {ruleset.ruleset_version} licenses no claim at the run's start, and law "
            "34 requires every description to carry one; register and sign a claim in "
            "Stage 03, then start a new creative run"
        )
    return licensed


async def describe(
    ctx: RunContext,
    brief: CreativeBrief,
    slot: Slot,
    specs: dict[str, AssetSpec],
    licensed: list[ClaimRef],
    now: datetime,
    *,
    node_id: str = NODE_ID,
    variant: Variant = "A",
    avoid: Sequence[str] = (),
) -> Built:
    """One ad group's descriptions and paths: A for 4.2.2, B for 4.2.4.

    Writes nothing — the rows come back for the caller to add once every ad
    group is built. `avoid` is variant A's copy, shown to B's writer.
    """
    creative = ctx.require_creative()
    copy = creative.constants.copy_
    ruleset = creative.linter.ruleset
    pool_size = copy.description_pool_size.value
    description, path = specs["description"], specs["path"]
    assert description.max_count is not None  # description_specs
    licensed_ids = [claim.claim_id for claim in licensed]

    draft: Any = await ctx.complete(
        draft_model(licensed_ids, pool_size=pool_size),
        system=SYSTEM.format(
            pool=pool_size, max_chars=description.max_chars, path_chars=path.max_chars
        )
        + (VARIANT_B_RULES if variant == "B" else ""),
        user=_user_prompt(brief, slot, licensed, variant=variant, avoid=avoid),
        task_class=TaskClass.COPYWRITE,
    )

    def lint(surface: str, ref: uuid.UUID, text: str) -> LintResult:
        return creative.linter.lint_candidate(
            LintTarget(
                ref=str(ref),
                surface=surface,
                campaign_type=SEARCH,
                market=slot.market,
                language=slot.language,
                text=text,
                generated_by_ai=True,
            ),
            now=now,
        )

    spans: list[str] = []
    written: list[dict[str, Any]] = []
    for item in draft.descriptions:
        asset_id = uuid.uuid4()
        result = lint(SURFACE, asset_id, item.text)
        unlicensed = exceptions.unlicensed_spans(result, text=item.text, ruleset=ruleset)
        span = claim_span(item.text, item.claim_text)
        fate = screen(result.verdict, unlicensed=unlicensed, quoted=span is not None)
        if fate == "exception":
            spans.extend(unlicensed)
            continue
        written.append(
            {
                "asset_id": asset_id,
                "text": item.text,
                "claim_ids": list(dict.fromkeys(uuid.UUID(str(c)) for c in item.claim_ids)),
                "claim_span": span,
                "lint": result,
                "fate": fate,
            }
        )

    selected, reserve = pick(
        [(str(e["asset_id"]), e["text"]) for e in written if e["fate"] == "eligible"],
        limit=description.max_count,
        near_duplicate=copy.near_duplicate_trigram.value,
    )
    minimum = description.min_count or 1
    if len(selected) < minimum:
        withheld = len(draft.descriptions) - len(written)
        failed = sum(1 for e in written if e["fate"] == "failed_lint")
        dropped = sum(1 for e in written if e["fate"] == "dropped")
        raise NodeContractError(
            f"{where(slot)} ({variant}): {len(selected)} description(s) could be selected from "
            f"{len(draft.descriptions)} written ({withheld} withheld for an unlicensed claim, "
            f"{failed} failed lint, {dropped} did not quote their claim); a Search ad needs "
            f"at least {minimum}"
        )

    status = {ref: CreativeAssetStatus.LINTED for ref in selected} | {
        ref: CreativeAssetStatus.RESERVE for ref in reserve
    }
    rows = [
        text_asset(
            ctx,
            asset_id=e["asset_id"],
            node_id=node_id,
            campaign_ref=slot.brief.campaign_ref,
            ad_group_ref=slot.brief.ad_group_ref,
            kind=CreativeAssetKind.DESCRIPTION,
            surface=SURFACE,
            variant=CreativeAssetVariant(variant),
            category=None,
            text=e["text"],
            fields={"claim_span": list(e["claim_span"]) if e["claim_span"] else None},
            claim_ids=e["claim_ids"],
            status=status.get(
                str(e["asset_id"]),
                CreativeAssetStatus.DRAFT
                if e["fate"] == "failed_lint"
                else CreativeAssetStatus.DROPPED,
            ),
            lint=e["lint"],
        )
        for e in written
    ]

    paths: list[str] = []
    for text in draft.paths:
        asset_id = uuid.uuid4()
        result = lint(PATH_SURFACE, asset_id, text)
        unlicensed = exceptions.unlicensed_spans(result, text=text, ruleset=ruleset)
        fate = screen(result.verdict, unlicensed=unlicensed, quoted=True)
        if fate == "exception":
            spans.extend(unlicensed)
            continue
        if fate == "eligible":
            paths.append(text)
        rows.append(
            text_asset(
                ctx,
                asset_id=asset_id,
                node_id=node_id,
                campaign_ref=slot.brief.campaign_ref,
                ad_group_ref=slot.brief.ad_group_ref,
                kind=CreativeAssetKind.PATH,
                surface=PATH_SURFACE,
                variant=CreativeAssetVariant(variant),
                category=None,
                text=text,
                fields={},
                claim_ids=[],
                status=CreativeAssetStatus.LINTED
                if fate == "eligible"
                else CreativeAssetStatus.DRAFT,
                lint=result,
            )
        )

    by_ref = {str(e["asset_id"]): e for e in written}

    def as_item(ref: str) -> dict[str, Any]:
        entry = by_ref[ref]
        return {
            "asset_id": entry["asset_id"],
            "text": entry["text"],
            "claim_ids": entry["claim_ids"],
            "claim_span": entry["claim_span"],
            "lint": lint_ref(entry["lint"]),
        }

    candidates = exceptions.candidates(spans)
    group = DescriptionGroup.model_validate(
        {
            "campaign_ref": slot.brief.campaign_ref,
            "ad_group_ref": slot.brief.ad_group_ref,
            "variant": variant,
            "descriptions": [as_item(ref) for ref in selected],
            # A passing path moves up when the one before it failed: Google
            # takes path2 only after path1.
            "paths": (*paths, *([None] * (PATHS - len(paths)))),
            "reserve": [as_item(ref) for ref in reserve],
            "exception_candidates": candidates,
        },
        context={"licensed_claim_ids": frozenset(licensed_ids)},
    )
    await ctx.progress(
        f"{where(slot)} ({variant}): {len(selected)} description(s) selected and "
        f"{len(reserve)} in reserve from {len(draft.descriptions)} written; "
        f"{len(paths)} of {PATHS} paths passed; "
        f"{sum(c.occurrences for c in candidates)} unlicensed claim span(s) withheld as "
        f"{len(candidates)} exception candidate(s)"
    )
    return Built(group=group, rows=rows)


def _user_prompt(
    brief: CreativeBrief,
    slot: Slot,
    licensed: Sequence[ClaimRef],
    *,
    variant: Variant = "A",
    avoid: Sequence[str] = (),
) -> str:
    sections: dict[str, Any] = {
        "AD GROUP": ad_group_section(slot, variant),
        "BRIEF": brief_section(brief),
        "KEYWORDS": list(slot.keywords),
        "PROOF POINTS": proof_points(licensed),
        "VOICE": list(brief.non_negotiables.voice_words),
        "NEVER": list(brief.non_negotiables.never_terms),
    }
    if avoid:
        sections["VARIANT A"] = list(avoid)
    return render_prompt(sections)


CLAIM_BOUND_DESCRIPTIONS = ClaimBoundDescriptionsNode()
