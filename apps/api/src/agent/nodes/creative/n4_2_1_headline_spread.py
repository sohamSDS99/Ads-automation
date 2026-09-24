"""4.2.1 `headline_spread` — the model writes candidates; code selects (Stage 04 PRD §11 4.2.1).

For every Search ad group the approved brief covers:

1. **COPYWRITE writes the pool** — exactly `copy.headline_pool_size`
   headlines, each with a category, answering a schema in which `keyword_ref`
   is an enum of the ad group's keywords (2.4.2) and `claim_ids` an enum of the
   claims the pin licenses. The model cannot cite what code did not offer.
2. **Code checks each one** (`invalid_reason`): its braces are one keyword
   insertion Google performs and agree with its `dki` flag; a keyword headline
   carries its keyword; a proof headline stands on a licensed claim.
3. **Each is linted at creation** through `lint_adapter` against the run's
   current pin (law 33) — a DKI headline on its **default text**, the text
   Google counts against the limit (`measured_text`). Set rules (asset counts)
   belong to the assembled ad, so a candidate is linted alone.
4. **Code selects** — `select.headlines_v1` picks at most the spec's
   `max_count` (asset_specs.search.headline; missing is `spec_missing`, never a
   guess) to meet `copy.headline_quotas` with no two near-duplicates.

Every candidate becomes a `CreativeAsset`: one that failed lint stays `draft`
(law 33: only a pass leaves draft); one that passed but broke its own promise
is `dropped`; the selection is `linted`; everything else is a `reserve` for
4.2.3's repair round. Nothing is written until every ad group has been built,
so a failed attempt leaves no half an ad; an attempt that follows one that did
fail first clears what that attempt wrote (the executor commits a failed
attempt's session with its failure record).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.creative import brief as briefs
from agent.creative import combinatorics, select
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    Evidence,
    RunStage,
)
from agent.export.plan_contract import PlannedAdGroup, PlannedCampaign
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._text_assets import text_asset
from agent.schemas.creative_brief import AdGroupBrief, CreativeBrief
from agent.schemas.guardrails import AssetSpec, ClaimRef, LintResult, LintTarget, RuleSet
from agent.schemas.search_ads import (
    PASSING,
    DkiError,
    HeadlineCandidate,
    HeadlineGroup,
    HeadlineSpreadOutput,
    LintRef,
    QuotaLine,
    QuotaReport,
    dki_default,
    render_default,
)

NODE_ID = "4.2.1"
SEARCH = "search"
SURFACE = "rsa_headline"
#: What `LintTarget` gets when the plan names no market or language — the
#: same fallback as Stage 03's lint route (`routes_guidelines.py`).
UNKNOWN_MARKET = "*"
DEFAULT_LANGUAGE = "en"

Category = Literal["keyword", "benefit", "offer", "proof", "objection", "cta"]

SYSTEM = """You write the headline pool for one Google responsive search ad. Code picks the
ad's headlines from what you write, so write a real spread — different angles,
benefits, proofs and objections — not variations of one line.

Rules, all of them hard:
- Write exactly {pool} headlines. Each renders to at most {max_chars} characters:
  count them, spaces included.
- Give each a category: keyword, benefit, offer, proof, objection or cta. Write at
  least as many of each category as CATEGORY COUNTS says.
- A keyword headline contains its keyword_ref from KEYWORDS, word for word. Use
  each keyword in one headline at most.
- Keyword insertion: write {{KeyWord:default text}} and set dki true. The headline
  with the default text in place must fit {max_chars} characters. Every other
  headline sets dki false and contains no braces.
- A proof headline states one claim from PROOF POINTS, cites its claim_id, and
  says nothing that claim does not. No other headline asserts a fact about the
  product.
- Write no numbers, prices, percentages, dates or deadlines in an offer headline.
- Every cta headline asks for the same action.
- No exclamation marks. Plain, specific language in the brand's voice. Never use a
  word from NEVER.
"""


# ---------------------------------------------------------------------------
# the part that decides — pure, and tested on its own
# ---------------------------------------------------------------------------


def measured_text(text: str) -> str:
    """What the linter and the pair checks see: the DKI default, or the text as written."""
    try:
        return render_default(text)
    except DkiError:
        return text


def invalid_reason(
    text: str,
    category: str,
    *,
    keyword_ref: str | None,
    claim_ids: Sequence[uuid.UUID],
    dki: bool,
    keywords: Sequence[str],
    licensed: frozenset[uuid.UUID],
) -> str | None:
    """Why a candidate is not what it says it is, or None when it is."""
    try:
        has_insertion = dki_default(text) is not None
    except DkiError as exc:
        return f"malformed keyword insertion: {exc}"
    if dki and not has_insertion:
        return "dki is true but the text has no {KeyWord:default text}"
    if has_insertion and not dki:
        return "dki is false but the text inserts a keyword"
    unlicensed = [str(claim) for claim in claim_ids if claim not in licensed]
    if unlicensed:
        return f"claim(s) {', '.join(unlicensed)} are not licensed at the pin"
    if keyword_ref is not None and keyword_ref not in keywords:
        return f"keyword_ref {keyword_ref!r} is not one of this ad group's keywords"
    if category == "keyword":
        if keyword_ref is None:
            return "a keyword headline names no keyword"
        if not combinatorics.contains_phrase(measured_text(text), keyword_ref):
            return f"a keyword headline must contain its keyword {keyword_ref!r}"
    if category == "proof" and not claim_ids:
        return "a proof headline cites no licensed claim"
    return None


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(
    keywords: Sequence[str], licensed: Sequence[uuid.UUID], *, pool_size: int
) -> type[BaseModel]:
    """The pool schema, with every reference the model may make as an enum."""
    keyword_ref: Any = Literal[tuple(keywords)] | None if keywords else type(None)
    if licensed:
        claim_ids: tuple[Any, Any] = (
            list[Literal[tuple(str(claim) for claim in licensed)]],  # type: ignore[misc]
            ...,
        )
    else:
        claim_ids = (list[str], Field(max_length=0))
    item = create_model(
        "HeadlineDraft",
        __base__=_Draft,
        text=(str, Field(min_length=1)),
        category=(Category, ...),
        keyword_ref=(keyword_ref, ...),
        claim_ids=claim_ids,
        dki=(bool, ...),
    )
    return create_model(
        "HeadlinePoolDraft",
        __base__=_Draft,
        candidates=(list[item], Field(min_length=pool_size, max_length=pool_size)),
    )


def category_asks(quotas: Mapping[str, int], *, pool_size: int) -> dict[str, int]:
    """How many of each category to ask for: one over each quota when the pool has room."""
    padded = {category: count + 1 for category, count in quotas.items()}
    return padded if sum(padded.values()) <= pool_size else dict(quotas)


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Slot:
    brief: AdGroupBrief
    campaign: PlannedCampaign
    group: PlannedAdGroup

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


@dataclass(slots=True)
class _Built:
    group: HeadlineGroup
    rows: list[CreativeAsset]


class HeadlineSpreadNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="headline_spread",
        stage="4.2",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.1.1",),
        task_class=TaskClass.COPYWRITE,
        input_model=CreativeBrief,
        output_model=HeadlineSpreadOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # Everything 4.2.1 reads is already pinned: the approved brief (4.1.1's
        # output), the plan's keywords in `CreativeInput`, the ruleset's specs.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        slots = _search_slots(brief, creative.input.account_structure.campaigns)
        await _clear_earlier_attempts(ctx)
        if not slots:
            return HeadlineSpreadOutput()

        # The run's start, not the wall clock: a retry an hour later must be
        # linted against — and offered — exactly what the first attempt was.
        now = ctx.run.started_at or datetime.now(UTC)
        spec = _headline_spec(creative.linter.ruleset)
        licensed = briefs.licensed_claims(creative.linter.ruleset, now)
        built = [await self._spread(ctx, brief, slot, spec, licensed, now) for slot in slots]

        for item in built:
            ctx.db.add_all(item.rows)
        await ctx.db.flush()
        return HeadlineSpreadOutput(ad_groups=[item.group for item in built])

    async def _spread(
        self,
        ctx: RunContext,
        brief: CreativeBrief,
        slot: _Slot,
        spec: AssetSpec,
        licensed: list[ClaimRef],
        now: datetime,
    ) -> _Built:
        creative = ctx.require_creative()
        copy = creative.constants.copy_
        pool_size = copy.headline_pool_size.value
        quotas = dict(copy.headline_quotas.value)
        threshold = copy.near_duplicate_trigram.value
        assert spec.max_chars is not None and spec.max_count is not None  # _headline_spec

        licensed_ids = frozenset(claim.claim_id for claim in licensed)
        schema = draft_model(
            slot.keywords, [claim.claim_id for claim in licensed], pool_size=pool_size
        )
        draft: Any = await ctx.complete(
            schema,
            system=SYSTEM.format(pool=pool_size, max_chars=spec.max_chars),
            user=_user_prompt(brief, slot, licensed, category_asks(quotas, pool_size=pool_size)),
        )

        drafted: list[dict[str, Any]] = []
        for item in draft.candidates:
            claim_ids = [uuid.UUID(str(claim)) for claim in item.claim_ids]
            asset_id = uuid.uuid4()
            measured = measured_text(item.text)
            result = creative.linter.lint_candidate(
                LintTarget(
                    ref=str(asset_id),
                    surface=SURFACE,
                    campaign_type=SEARCH,
                    market=slot.market,
                    language=slot.language,
                    text=measured,
                    generated_by_ai=True,
                ),
                now=now,
            )
            drafted.append(
                {
                    "asset_id": asset_id,
                    "text": item.text,
                    "default_text": measured,
                    "category": item.category,
                    "keyword_ref": item.keyword_ref,
                    "claim_ids": claim_ids,
                    "dki": item.dki,
                    "lint": result,
                    "invalid": invalid_reason(
                        item.text,
                        item.category,
                        keyword_ref=item.keyword_ref,
                        claim_ids=claim_ids,
                        dki=item.dki,
                        keywords=slot.keywords,
                        licensed=licensed_ids,
                    ),
                }
            )

        eligible = [
            select.Candidate(
                ref=str(entry["asset_id"]), text=entry["default_text"], category=entry["category"]
            )
            for entry in drafted
            if entry["lint"].verdict in PASSING and entry["invalid"] is None
        ]
        try:
            chosen = select.headlines_v1(
                eligible, quotas=quotas, limit=spec.max_count, near_duplicate=threshold
            )
        except select.SelectionError as exc:
            raise NodeContractError(f"4.2.1 cannot select for {_where(slot)}: {exc}") from exc
        minimum = spec.min_count or 1
        if len(chosen.selected) < minimum:
            failed = sum(1 for entry in drafted if entry["lint"].verdict not in PASSING)
            invalid = sum(1 for entry in drafted if entry["invalid"] is not None)
            raise NodeContractError(
                f"{_where(slot)}: {len(chosen.selected)} headline(s) could be selected from "
                f"{len(drafted)} candidates ({failed} failed lint, {invalid} broke their own "
                f"category or keyword insertion); a Search ad needs at least {minimum}"
            )

        selected = set(chosen.selected)
        reserved = set(chosen.reserve)
        duplicates = {item.ref: item for item in chosen.near_duplicates}
        candidates: list[HeadlineCandidate] = []
        rows: list[CreativeAsset] = []
        for entry in drafted:
            ref = str(entry["asset_id"])
            linted: LintResult = entry["lint"]
            outcome, status, reason = _outcome(
                ref, linted, entry["invalid"], selected, reserved, duplicates
            )
            candidates.append(
                HeadlineCandidate(
                    asset_id=entry["asset_id"],
                    text=entry["text"],
                    default_text=entry["default_text"],
                    category=entry["category"],
                    keyword_ref=entry["keyword_ref"],
                    claim_ids=entry["claim_ids"],
                    dki=entry["dki"],
                    lint=LintRef(
                        verdict=linted.verdict,
                        ruleset_version=linted.ruleset_version,
                        rule_ids=list(dict.fromkeys(f.rule_id for f in linted.findings)),
                    ),
                    outcome=outcome,
                    reason=reason,
                )
            )
            rows.append(
                text_asset(
                    ctx,
                    asset_id=entry["asset_id"],
                    node_id=NODE_ID,
                    campaign_ref=slot.brief.campaign_ref,
                    ad_group_ref=slot.brief.ad_group_ref,
                    kind=CreativeAssetKind.HEADLINE,
                    surface=SURFACE,
                    variant=CreativeAssetVariant.A,
                    category=entry["category"],
                    text=entry["text"],
                    fields={
                        "dki": entry["dki"],
                        "default_text": entry["default_text"],
                        "keyword_ref": entry["keyword_ref"],
                    },
                    claim_ids=entry["claim_ids"],
                    status=status,
                    lint=linted,
                )
            )

        report = chosen.quota_report
        await ctx.progress(
            f"{_where(slot)}: {len(eligible)} of {len(drafted)} candidates passed lint and "
            f"their own checks; {len(chosen.selected)} selected, "
            f"quotas {'met' if report.met else 'short'}"
        )
        by_ref = {str(c.asset_id): c.asset_id for c in candidates}
        return _Built(
            group=HeadlineGroup(
                campaign_ref=slot.brief.campaign_ref,
                ad_group_ref=slot.brief.ad_group_ref,
                campaign_type=SEARCH,
                market=slot.market,
                language=slot.language,
                variant="A",
                candidates=candidates,
                selected=[by_ref[ref] for ref in chosen.selected],
                reserve=[by_ref[ref] for ref in chosen.reserve],
                quota_report=QuotaReport(
                    lines=[
                        QuotaLine(
                            category=line.category,
                            required=line.required,
                            selected=line.selected,
                            available=line.available,
                        )
                        for line in report.lines
                    ],
                    limit=report.limit,
                    selected=report.selected,
                    met=report.met,
                ),
                near_duplicate_trigram=threshold,
            ),
            rows=rows,
        )


def _outcome(
    ref: str,
    result: LintResult,
    invalid: str | None,
    selected: set[str],
    reserved: set[str],
    duplicates: Mapping[str, select.NearDuplicate],
) -> tuple[
    Literal["selected", "reserve", "dropped", "failed_lint"], CreativeAssetStatus, str | None
]:
    if result.verdict not in PASSING:
        return "failed_lint", CreativeAssetStatus.DRAFT, None
    if invalid is not None:
        return "dropped", CreativeAssetStatus.DROPPED, invalid
    if ref in selected:
        return "selected", CreativeAssetStatus.LINTED, None
    if ref in reserved:
        duplicate = duplicates.get(ref)
        reason = (
            f"near_duplicate of {duplicate.of} (copy.trigram_v1 {duplicate.similarity:.2f})"
            if duplicate is not None
            else None
        )
        return "reserve", CreativeAssetStatus.RESERVE, reason
    raise NodeContractError(
        f"candidate {ref} is neither selected nor a reserve"
    )  # pragma: no cover


def _search_slots(brief: CreativeBrief, campaigns: Sequence[PlannedCampaign]) -> list[_Slot]:
    """The brief's ad groups that sit in a Search campaign — an RSA exists nowhere else."""
    by_ref = {(campaign.campaign_ref or campaign.name): campaign for campaign in campaigns}
    slots: list[_Slot] = []
    for item in brief.ad_groups:
        campaign = by_ref.get(item.campaign_ref)
        if campaign is None:
            raise NodeContractError(
                f"the brief names campaign {item.campaign_ref!r}, which the pinned plan "
                f"does not have"
            )
        if campaign.type.strip().lower() != SEARCH:
            continue
        group = next((g for g in campaign.ad_groups if g.name == item.ad_group_ref), None)
        if group is None:
            raise NodeContractError(
                f"the brief names ad group {item.ad_group_ref!r} in {item.campaign_ref!r}, "
                f"which the pinned plan does not have"
            )
        slots.append(_Slot(brief=item, campaign=campaign, group=group))
    return slots


def _headline_spec(ruleset: RuleSet) -> AssetSpec:
    spec = ruleset.asset_specs.for_campaign(SEARCH).get("headline")
    missing = (
        "the whole spec"
        if spec is None
        else ", ".join(name for name in ("max_chars", "max_count") if getattr(spec, name) is None)
    )
    if spec is None or missing:
        raise NodeContractError(
            f"spec_missing: ruleset {ruleset.ruleset_version} has no asset_specs.search.headline "
            f"{missing}; Stage 04 never guesses a Google limit (§9.5)"
        )
    return spec


async def _clear_earlier_attempts(ctx: RunContext) -> None:
    """A failed attempt's rows were committed with its failure; this attempt replaces them."""
    await ctx.db.execute(
        sa.delete(CreativeAsset).where(
            CreativeAsset.creative_run_id == ctx.run.id,
            CreativeAsset.node_id == NODE_ID,
            CreativeAsset.frozen_at.is_(None),
        )
    )


def _where(slot: _Slot) -> str:
    return f"{slot.brief.campaign_ref} / {slot.brief.ad_group_ref}"


def _user_prompt(
    brief: CreativeBrief,
    slot: _Slot,
    licensed: Sequence[ClaimRef],
    asks: Mapping[str, int],
) -> str:
    sections: dict[str, Any] = {
        "AD GROUP": {
            "campaign": slot.brief.campaign_ref,
            "ad_group": slot.brief.ad_group_ref,
            "theme": slot.brief.theme,
            "primary_message": slot.brief.primary_message.text,
            "landing_url": str(slot.brief.landing_url),
            "language": slot.language,
        },
        "BRIEF": {
            "objective": brief.objective.text,
            "audience": [line.text for line in brief.audience],
            "angle": brief.angle.text,
            "offer": (
                f"an offer on {brief.offer.sku_or_set} exists; name it in words, never figures"
                if brief.offer is not None
                else "none"
            ),
        },
        "KEYWORDS": list(slot.keywords),
        "PROOF POINTS": [
            {"claim_id": str(claim.claim_id), "text": claim.normalized_text} for claim in licensed
        ],
        "CATEGORY COUNTS": dict(asks),
        "VOICE": list(brief.non_negotiables.voice_words),
        "NEVER": list(brief.non_negotiables.never_terms),
    }
    return "\n\n".join(
        f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=1)}"
        for name, value in sections.items()
    )


HEADLINE_SPREAD = HeadlineSpreadNode()
