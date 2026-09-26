"""4.6.2 `editorial_lint_and_exceptions` — every asset's lint, and what only H3 can license.

Stage 04 PRD §11 4.6.2 and Law 34: a full `LintResult` per target, and
`exceptions[]{kind ∈ {new_claim, disclaimer, image_right}, subject,
asset_ids[], occurrences, evidence_ids[], proposed{…}, fallback_asset_ids[]}`,
capped at `max_exceptions_per_run` and ranked by occurrences — **only items not
already cleared**: "the agent flags only the exceptions".

**The lint.** Every non-dropped asset's `LintResult` at the run's pin. Law 33
linted each target at creation, against this same pin, with every per-target
rule the RuleSet carries — and no repin can precede this node, because H3 (the
only one) comes after it. So the result is read and verified to be *at the
pin*, not recomputed from a second definition of each node's lint lines (a
price row does not even keep the description line it was linted with). An
asset with no result at the pin is `unlinted`, never a pass (law 31). 4.6.4
re-lints everything once H3 has fixed the final pin.

**The exceptions** come from what upstream nodes already found, never from a
new detector:

* `new_claim` — every clause a node withheld (`exception_candidates` of 4.2.2,
  4.2.4, 4.2.5, 4.3.1, 4.3.2, 4.3.3) and every draft whose own lint names an
  unlicensed claim (4.2.1 stores such a headline as a draft). One claim is one
  exception however often it was seen; its licence is scoped to the markets
  and languages it was seen in; its type is the family of the pin's detector
  that finds it. A clause the Stage 03 register already holds is not *new* —
  that row's decision is Stage 03's — so it is reported, not raised;
* `image_right` — a third-party reference 4.4.1 refused for want of one (the
  shape `media/references.image_right_proposal` defines), and every VISION
  flag G8 or G8b showed on an asset still in the package (§13: "VISION flags
  … become image_right exceptions in 4.6.2, never silent passes");
* `disclaimer` — a blocking finding of a disclosure rule on an asset still in
  the package: the text and placement it requires.

`fallback_asset_ids` is precomputed: for a withheld clause, the carried copy
already filling its slot; for an asset that is being carried, a reserve of the
same slot; nothing for a draft, which was never carried. What exceeds the cap,
is already cleared in this run, or is in the register goes to `not_raised[]`
with its reason. No model call, no spend; the rows are rewritten on a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.creative import edits, exceptions
from agent.creative.clearance import CARRIED
from agent.creative.exceptions import ExceptionKind, Raise, Sighting, fold
from agent.db.models import (
    ClaimRecord,
    CreativeAsset,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    Evidence,
    RunStage,
)
from agent.export.plan_contract import PlannedCampaign
from agent.guardrails.normalize import normalized_text
from agent.llm.router import TaskClass
from agent.media import references
from agent.nodes.base import NodeSpec, RunContext
from agent.nodes.creative._ad_groups import DEFAULT_LANGUAGE, UNKNOWN_MARKET
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_qa import (
    EditorialLint,
    ExceptionItem,
    NotRaised,
    TargetLint,
)
from agent.schemas.guardrails import DisclosureMatcher, LintResult, RuleSet

NODE_ID = "4.6.2"
THIRD_PARTY_REFUSAL = "third_party_without_image_right"


# ---------------------------------------------------------------------------
# what 4.6.2 reads of each upstream output — only that, so a field another
# phase adds elsewhere in those outputs cannot break this node
# ---------------------------------------------------------------------------


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Candidate(_Loose):
    span: str
    occurrences: int = 1


class _Group(_Loose):
    campaign_ref: str
    ad_group_ref: str | None = None
    variant: str | None = None
    market: str | None = None
    language: str | None = None
    exception_candidates: list[_Candidate] = Field(default_factory=list)


class _VariantBGroup(_Loose):
    descriptions: _Group | None = None


class _Ref(_Loose):
    campaign_ref: str


class _Outputs(_Loose):
    ad_groups: list[_Group] = Field(default_factory=list)
    asset_groups: list[_Group] = Field(default_factory=list)
    campaigns: list[_Group] = Field(default_factory=list)
    exception_candidates: list[_Candidate] = Field(default_factory=list)
    promotions: list[_Ref] = Field(default_factory=list)
    prices: list[_Ref] = Field(default_factory=list)


class _VariantB(_Loose):
    ad_groups: list[_VariantBGroup] = Field(default_factory=list)


class _Advisory(_Loose):
    flags: list[str] = Field(default_factory=list)


class _ReviewItem(_Loose):
    asset_id: uuid.UUID
    vision_advisory: _Advisory = Field(default_factory=_Advisory)


class _Review(_Loose):
    items: list[_ReviewItem] = Field(default_factory=list)


class _Concepts(_Loose):
    reference_refusals: dict[str, str | None] = Field(default_factory=dict)


class EditorialLintNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="editorial_lint_and_exceptions",
        stage="4.6",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.6.1", "4.5.2"),
        task_class=TaskClass.VISION,
        input_model=CreativeInput,
        output_model=EditorialLint,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        ruleset = creative.linter.ruleset
        pin = creative.linter.pin
        cap = int(creative.constants.exceptions.max_exceptions_per_run.value)
        campaigns = {
            (campaign.campaign_ref or campaign.name): campaign
            for campaign in creative.input.account_structure.campaigns
        }
        assets = await _assets(ctx)
        by_id = {asset.id: asset for asset in assets}

        targets = [_target(asset, pin) for asset in assets]

        sightings = [
            *_withheld(ctx.outputs, campaigns, assets),
            *_drafts(assets, campaigns, ruleset, pin),
        ]
        not_raised: list[NotRaised] = []
        claims, in_register = await _new_claims(ctx, sightings, ruleset)
        not_raised.extend(in_register)
        cleared = await _cleared(ctx)
        rights, already = await _image_rights(ctx, by_id, assets, creative.input, cleared)
        not_raised.extend(already)
        disclaimers, already = _disclaimers(assets, ruleset, pin, cleared)
        not_raised.extend(already)

        kept, over = exceptions.rank([*claims, *rights, *disclaimers], cap=cap)
        not_raised.extend(
            NotRaised(
                kind=item.kind,
                subject=item.subject,
                occurrences=item.occurrences,
                reason="over_cap",
                detail=f"beyond the {cap} exceptions H3 is asked about in one run",
            )
            for item in over
        )
        rows = await _write(ctx, kept)
        return EditorialLint(
            ruleset_version=pin,
            targets=targets,
            exceptions=[
                ExceptionItem(
                    exception_id=row.id,
                    kind=item.kind,
                    subject=item.subject,
                    asset_ids=list(item.asset_ids),
                    occurrences=item.occurrences,
                    evidence_ids=list(item.evidence_ids),
                    proposed=dict(item.proposed),
                    fallback_asset_ids=list(item.fallback_asset_ids),
                )
                for item, row in zip(kept, rows, strict=True)
            ],
            not_raised=not_raised,
            cap=cap,
        )


# ---------------------------------------------------------------------------
# the lint
# ---------------------------------------------------------------------------


async def _assets(ctx: RunContext) -> list[CreativeAsset]:
    return list(
        (
            await ctx.db.execute(
                sa.select(CreativeAsset)
                .where(
                    CreativeAsset.creative_run_id == ctx.run.id,
                    CreativeAsset.status != CreativeAssetStatus.DROPPED,
                )
                .order_by(CreativeAsset.node_id, CreativeAsset.created_at, CreativeAsset.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


def _lint_at(asset: CreativeAsset, pin: str) -> LintResult | None:
    if asset.lint is None or asset.ruleset_version != pin:
        return None
    try:
        result = LintResult.model_validate(asset.lint)
    except ValidationError:
        return None
    return result if result.ruleset_version == pin else None


def _target(asset: CreativeAsset, pin: str) -> TargetLint:
    result = _lint_at(asset, pin)
    return TargetLint(
        asset_id=asset.id,
        kind=asset.kind.value,
        surface=asset.surface,
        status=asset.status.value,
        verdict=result.verdict if result is not None else "unlinted",
        lint=result,
    )


# ---------------------------------------------------------------------------
# new claims
# ---------------------------------------------------------------------------


def locale(
    campaigns: Mapping[str, PlannedCampaign],
    campaign_ref: str,
    ad_group_ref: str | None = None,
    *,
    market: str | None = None,
    language: str | None = None,
) -> tuple[str, str]:
    """The market and language copy for this campaign was written for — the same
    fallbacks `_ad_groups.Slot` gives the lint targets it was linted as."""
    campaign = campaigns.get(campaign_ref)
    group_market = None
    if campaign is not None and ad_group_ref is not None:
        group = next((g for g in campaign.ad_groups if g.name == ad_group_ref), None)
        group_market = group.market if group is not None else None
    found_market = (
        market or group_market or (campaign.market if campaign is not None else None) or ""
    ).strip() or UNKNOWN_MARKET
    found_language = (
        language or (campaign.language if campaign is not None else None) or ""
    ).strip() or DEFAULT_LANGUAGE
    return found_market, found_language


def _carried(
    assets: Sequence[CreativeAsset],
    node_id: str,
    campaign_refs: Iterable[str] | None = None,
    ad_group_ref: str | None = None,
    variant: str | None = None,
) -> tuple[uuid.UUID, ...]:
    """What already fills the slot a withheld clause was written for."""
    refs = set(campaign_refs) if campaign_refs is not None else None
    return tuple(
        asset.id
        for asset in assets
        if asset.node_id == node_id
        and asset.status in CARRIED
        and (refs is None or asset.campaign_ref in refs)
        and (ad_group_ref is None or asset.ad_group_ref == ad_group_ref)
        and (variant is None or asset.variant == variant)
    )


def _withheld(
    outputs: Mapping[str, Mapping[str, Any]],
    campaigns: Mapping[str, PlannedCampaign],
    assets: Sequence[CreativeAsset],
) -> list[Sighting]:
    """Every clause an upstream node withheld (Law 34), where it was aimed."""
    groups: list[tuple[str, _Group]] = []
    for node_id in ("4.2.2", "4.2.5", "4.3.1", "4.3.3"):
        parsed = _Outputs.model_validate(outputs.get(node_id) or {})
        groups.extend(
            (node_id, group)
            for group in (*parsed.ad_groups, *parsed.asset_groups, *parsed.campaigns)
        )
    variant_b = _VariantB.model_validate(outputs.get("4.2.4") or {})
    groups.extend(
        ("4.2.4", group.descriptions)
        for group in variant_b.ad_groups
        if group.descriptions is not None
    )

    found: list[Sighting] = []
    for node_id, group in groups:
        market, language = locale(
            campaigns,
            group.campaign_ref,
            group.ad_group_ref,
            market=group.market,
            language=group.language,
        )
        fallback = _carried(
            assets, node_id, (group.campaign_ref,), group.ad_group_ref, group.variant
        )
        found.extend(
            Sighting(
                clause=candidate.span,
                markets=(market,),
                languages=(language,),
                occurrences=candidate.occurrences,
                fallback=fallback,
            )
            for candidate in group.exception_candidates
        )

    # 4.3.2 collects across its campaigns, so its clauses were aimed at all of them.
    offers = _Outputs.model_validate(outputs.get("4.3.2") or {})
    if offers.exception_candidates:
        refs = list(
            dict.fromkeys(ref.campaign_ref for ref in (*offers.promotions, *offers.prices))
        ) or list(campaigns)
        locales = [locale(campaigns, ref) for ref in refs]
        markets = tuple(sorted({m for m, _ in locales})) or (UNKNOWN_MARKET,)
        languages = tuple(sorted({lang for _, lang in locales})) or (DEFAULT_LANGUAGE,)
        fallback = _carried(assets, "4.3.2")
        found.extend(
            Sighting(
                clause=candidate.span,
                markets=markets,
                languages=languages,
                occurrences=candidate.occurrences,
                fallback=fallback,
            )
            for candidate in offers.exception_candidates
        )
    return found


def _drafts(
    assets: Sequence[CreativeAsset],
    campaigns: Mapping[str, PlannedCampaign],
    ruleset: RuleSet,
    pin: str,
) -> list[Sighting]:
    """Drafts whose own lint at the pin names an unlicensed claim (4.2.1's headlines).

    Only a finding on the row's own text (`target_ref == asset id`) has offsets
    into `text`; a multi-line asset's lines are `{id}:{n}`, and those nodes
    withhold a claim instead of drafting it.
    """
    found: list[Sighting] = []
    for asset in assets:
        if asset.status is not CreativeAssetStatus.DRAFT or not asset.text:
            continue
        result = _lint_at(asset, pin)
        if result is None:
            continue
        own = result.model_copy(
            update={"findings": tuple(f for f in result.findings if f.target_ref == str(asset.id))}
        )
        clauses = exceptions.unlicensed_spans(
            own, text=edits.linted_text(asset.surface, asset.text), ruleset=ruleset
        )
        market, language = locale(campaigns, asset.campaign_ref, asset.ad_group_ref)
        found.extend(
            Sighting(clause=clause, markets=(market,), languages=(language,), asset_id=asset.id)
            for clause in clauses
        )
    return found


async def _new_claims(
    ctx: RunContext, sightings: Sequence[Sighting], ruleset: RuleSet
) -> tuple[list[Raise], list[NotRaised]]:
    raised = exceptions.claims(sightings, ruleset)
    if not raised:
        return [], []
    keys = {
        item.subject: normalized_text(item.subject, locale=_first(item.proposed.get("languages")))
        for item in raised
    }
    held = dict(
        (
            await ctx.db.execute(
                sa.select(ClaimRecord.normalized_text, ClaimRecord.status).where(
                    ClaimRecord.project_id == ctx.run.project_id,
                    ClaimRecord.superseded_by.is_(None),
                    ClaimRecord.normalized_text.in_(set(keys.values())),
                )
            )
        )
        .tuples()
        .all()
    )
    new: list[Raise] = []
    reported: list[NotRaised] = []
    for item in raised:
        status = held.get(keys[item.subject])
        if status is None:
            new.append(item)
            continue
        reported.append(
            NotRaised(
                kind="new_claim",
                subject=item.subject,
                occurrences=item.occurrences,
                reason="in_register",
                detail=(
                    f"the Stage 03 claims register already holds this claim ({status.value}); "
                    "its decision is the register's, not H3's"
                ),
            )
        )
    return new, reported


def _first(values: Any) -> str:
    return str(values[0]) if values else DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# image rights and disclaimers
# ---------------------------------------------------------------------------


async def _cleared(ctx: RunContext) -> set[tuple[str, str]]:
    """`(kind, folded subject)` of every exception already cleared in this run —
    clearances are package-scoped (§8.6), so only this run's count."""
    rows = (
        await ctx.db.execute(
            sa.select(CreativeException.kind, CreativeException.subject_text).where(
                CreativeException.creative_run_id == ctx.run.id,
                CreativeException.status == CreativeExceptionStatus.CLEARED,
                CreativeException.decided_by.is_not(None),
            )
        )
    ).tuples()
    return {(kind.value, fold(subject or "")) for kind, subject in rows}


async def _image_rights(
    ctx: RunContext,
    by_id: Mapping[uuid.UUID, CreativeAsset],
    assets: Sequence[CreativeAsset],
    creative_input: CreativeInput,
    cleared: set[tuple[str, str]],
) -> tuple[list[Raise], list[NotRaised]]:
    raised: list[Raise] = []
    reported: list[NotRaised] = []

    # A third-party reference 4.4.1 would not send without one.
    cleared_refs = await references.cleared_image_rights(ctx.db, ctx.run.id)
    statements = {ref.reference_id: ref.rights_statement for ref in creative_input.references}
    refusals = _Concepts.model_validate(ctx.outputs.get("4.4.1") or {}).reference_refusals
    for key, reason in sorted(refusals.items()):
        if reason != THIRD_PARTY_REFUSAL:
            continue
        reference_id = uuid.UUID(key)
        subject = f"reference {reference_id}"
        if reference_id in cleared_refs:
            reported.append(_already("image_right", subject, 1))
            continue
        raised.append(
            Raise(
                kind="image_right",
                subject=subject,
                occurrences=1,
                proposed=references.image_right_proposal(
                    reference_id,
                    basis=statements.get(reference_id)
                    or "third-party reference, no licence on file",
                ),
            )
        )

    # Every VISION flag G8 or G8b showed on an asset still in the package.
    flagged: dict[str, tuple[str, list[uuid.UUID]]] = {}
    for node_id in ("4.4.5", "4.4.7"):
        for item in _Review.model_validate(ctx.outputs.get(node_id) or {}).items:
            if item.asset_id not in by_id:
                continue
            for flag in item.vision_advisory.flags:
                shown = " ".join(flag.split())
                if not shown:
                    continue
                spelled, ids = flagged.setdefault(fold(shown), (shown, []))
                if item.asset_id not in ids:
                    ids.append(item.asset_id)
    for key, (subject, ids) in flagged.items():
        if ("image_right", key) in cleared:
            reported.append(_already("image_right", subject, len(ids)))
            continue
        raised.append(
            Raise(
                kind="image_right",
                subject=subject,
                occurrences=len(ids),
                asset_ids=tuple(ids),
                fallback_asset_ids=_reserves([by_id[i] for i in ids], assets),
                proposed={"basis": "VISION flag at G8/G8b", "flag": subject},
            )
        )
    return raised, reported


def _disclaimers(
    assets: Sequence[CreativeAsset],
    ruleset: RuleSet,
    pin: str,
    cleared: set[tuple[str, str]],
) -> tuple[list[Raise], list[NotRaised]]:
    """A blocking disclosure finding: the text and placement the rule requires."""
    rules = {
        rule.rule_id: rule.matcher
        for rule in ruleset.rules
        if rule.category == "disclosure" and isinstance(rule.matcher, DisclosureMatcher)
    }
    needed: dict[tuple[str, str], list[CreativeAsset]] = {}
    for asset in assets:
        result = _lint_at(asset, pin)
        if result is None:
            continue
        for finding in result.findings:
            matcher = rules.get(finding.rule_id)
            if matcher is None or finding.severity != "blocking":
                continue
            tied = needed.setdefault((matcher.required_text, matcher.placement), [])
            if asset not in tied:
                tied.append(asset)
    raised: list[Raise] = []
    reported: list[NotRaised] = []
    for (text, placement), tied in needed.items():
        if ("disclaimer", fold(text)) in cleared:
            reported.append(_already("disclaimer", text, len(tied)))
            continue
        raised.append(
            Raise(
                kind="disclaimer",
                subject=text,
                occurrences=len(tied),
                asset_ids=tuple(a.id for a in tied),
                fallback_asset_ids=_reserves(tied, assets),
                proposed={"text": text, "placement": placement},
            )
        )
    return raised, reported


def _reserves(
    tied: Sequence[CreativeAsset], assets: Sequence[CreativeAsset]
) -> tuple[uuid.UUID, ...]:
    """One reserve of its own slot for each tied asset that is being carried."""
    used: set[uuid.UUID] = set()
    for asset in tied:
        if asset.status not in CARRIED:
            continue
        reserve = next(
            (
                a
                for a in assets
                if a.status is CreativeAssetStatus.RESERVE
                and a.id not in used
                and edits.same_slot(a, asset)
            ),
            None,
        )
        if reserve is not None:
            used.add(reserve.id)
    return tuple(a.id for a in assets if a.id in used)


def _already(kind: ExceptionKind, subject: str, occurrences: int) -> NotRaised:
    return NotRaised(
        kind=kind,
        subject=subject,
        occurrences=max(occurrences, 1),
        reason="already_cleared",
        detail="the legal owner already cleared this in this run",
    )


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


async def _write(ctx: RunContext, kept: Sequence[Raise]) -> list[CreativeException]:
    """This run's exception rows, rewritten: an earlier attempt's open rows go
    first, so a retry raises each exception once. Decided rows are history."""
    await ctx.db.execute(
        sa.delete(CreativeException).where(
            CreativeException.creative_run_id == ctx.run.id,
            CreativeException.status == CreativeExceptionStatus.OPEN,
        )
    )
    rows = [
        CreativeException(
            workspace_id=ctx.run.workspace_id,
            project_id=ctx.run.project_id,
            creative_run_id=ctx.run.id,
            kind=CreativeExceptionKind(item.kind),
            subject_text=item.subject,
            asset_ids=list(item.asset_ids),
            occurrences=item.occurrences,
            proposed=dict(item.proposed),
            evidence_ids=list(item.evidence_ids),
            fallback_asset_ids=list(item.fallback_asset_ids),
        )
        for item in kept
    ]
    ctx.db.add_all(rows)
    await ctx.db.flush()
    return rows


EDITORIAL_LINT_AND_EXCEPTIONS = EditorialLintNode()
