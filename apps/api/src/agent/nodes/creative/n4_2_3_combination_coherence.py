"""4.2.3 `combination_coherence` — every pair the ad can serve, one repair round (PRD §11 4.2.3).

Google shows a responsive search ad as any of its headlines beside any of its
descriptions, so every pair it could serve is checked, per ad group:

1. **Deterministic flags** — `combinatorics.pair_flags_v1` over every HH, HD
   and DD pair of 4.2.1's selection and 4.2.2's descriptions. Blocking.
2. **CLASSIFY labels** — every pair labelled `reads_well`, `redundant`,
   `contradictory` or `order_dependent`, in batches of `CLASSIFY_BATCH` (50)
   pairs per call. The schema has one required enum per pair, so every pair is
   labelled exactly once.
3. **Exactly one repair round** (`copy.pair_repair_rounds`) — the flagged pairs
   and the `redundant` / `contradictory` ones go to `combinatorics.repair_v1`,
   which swaps from the reserves 4.2.1 and 4.2.2 kept. The pairs the swapped-in
   assets form are flagged and labelled once so the report is complete; nothing
   is repaired a second time. What one round could not end is `unresolved`.
4. **Pins only for `order_dependent` pairs** (`combinatorics.pins_v1`), in the
   order they read — never to force a message.

4.2.2's output is read through its schema, `ClaimBoundDescriptionsOutput`,
validated against the claims the pin licenses at the run's start — the same
set 4.2.2 wrote against — so no description reaches a pair without one (law 34).

Nothing is written until every ad has been judged. The asset rows are then set
to an absolute state computed from 4.2.1's and 4.2.2's outputs — which assets
the ad carries, which are reserves, which are pinned — so an attempt that
follows a failed one (whose writes the executor committed) overwrites rather
than compounds them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, create_model

from agent.creative import brief as briefs
from agent.creative import combinatorics
from agent.creative.combinatorics import Asset, FlagRules, Kind
from agent.creative.constants import CopyConstants
from agent.db.models import CreativeAsset, CreativeAssetStatus, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._text_assets import generated_lineage
from agent.schemas.search_ads import (
    BAD_LABELS,
    ClaimBoundDescriptionsOutput,
    CombinationCoherenceOutput,
    DescriptionGroup,
    DescriptionItem,
    HeadlineCandidate,
    HeadlineGroup,
    HeadlineSpreadOutput,
    Pair,
    PairLabel,
    PairReport,
    Pin,
    Swap,
)

NODE_ID = "4.2.3"
#: §11 4.2.3: "CLASSIFY labels (batched 50 pairs/call)".
CLASSIFY_BATCH = 50

SYSTEM = """You label pairs of copy from one Google responsive search ad. Google may show
any headline beside any other headline and any description, so each pair below
may appear together in one ad. For every pair, choose exactly one label:

- reads_well: the two read naturally together, in either order.
- redundant: they say the same thing, so showing both wastes the ad's space.
- contradictory: they conflict — different offers, different claims, or asks for
  different actions.
- order_dependent: they read well only in the order given, A then B.

Label every pair. Judge only what is written in A and B.
"""

PairKey = tuple[str, str]


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True, slots=True)
class Judged:
    report: PairReport
    #: asset id -> (status, pin position, lineage) — the absolute state to write.
    state: dict[uuid.UUID, tuple[CreativeAssetStatus, str | None, dict[str, Any] | None]]


class CombinationCoherenceNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="combination_coherence",
        stage="4.2",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.2.1", "4.2.2"),
        task_class=TaskClass.CLASSIFY,
        input_model=HeadlineSpreadOutput,
        output_model=CombinationCoherenceOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # Both inputs are the outputs of the two nodes this one depends on.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        copy = creative.constants.copy_
        rounds = repair_rounds(copy)
        headlines = HeadlineSpreadOutput.model_validate(ctx.output_of("4.2.1"))
        if not headlines.ad_groups:
            # No Search ad group in scope: no responsive search ad, no pair to
            # judge, and nothing 4.2.2 could be asked for.
            return CombinationCoherenceOutput()
        descriptions = _descriptions(ctx)
        by_group = {
            (group.campaign_ref, group.ad_group_ref, group.variant): group
            for group in descriptions.ad_groups
        }

        judged: list[Judged] = []
        for group in headlines.ad_groups:
            match = by_group.get((group.campaign_ref, group.ad_group_ref, group.variant))
            if match is None:
                raise NodeContractError(
                    f"4.2.2 wrote no descriptions for {group.campaign_ref} / "
                    f"{group.ad_group_ref} ({group.variant}); an ad cannot be judged "
                    f"without the descriptions Google would show with its headlines"
                )
            judged.append(
                await judge(
                    ctx,
                    group,
                    match,
                    rounds=rounds,
                    quotas=dict(copy.headline_quotas.value),
                    rules=flag_rules(copy, group),
                )
            )

        await _apply(ctx, judged)
        return CombinationCoherenceOutput(ads=[item.report for item in judged])


def repair_rounds(copy: CopyConstants) -> int:
    """`copy.pair_repair_rounds`, held to the one round §11 4.2.3 allows."""
    rounds = copy.pair_repair_rounds.value
    if rounds not in (0, 1):
        raise NodeContractError(
            f"copy.pair_repair_rounds is {rounds}; §11 4.2.3 allows one repair round, "
            "and a second would re-ask a model without a bound on its cost"
        )
    return rounds


def flag_rules(copy: CopyConstants, group: HeadlineGroup) -> FlagRules:
    """The deterministic flags' parameters for one ad: the threshold, and its pool's CTA verbs."""
    return FlagRules(
        near_duplicate=copy.near_duplicate_trigram.value,
        cta_verbs=combinatorics.cta_verbs(_headline(candidate) for candidate in group.candidates),
    )


async def judge(
    ctx: RunContext,
    group: HeadlineGroup,
    descriptions: DescriptionGroup,
    *,
    node_id: str = NODE_ID,
    rounds: int,
    quotas: Mapping[str, int],
    rules: FlagRules,
) -> Judged:
    """One ad's pairs, flags, labels, one repair round and pins: A for 4.2.3, B for 4.2.4.

    Writes nothing: `Judged.state` is the absolute state for the caller to set.
    `node_id` is the node that made the swaps, recorded in their lineage.
    """
    candidates = {candidate.asset_id: candidate for candidate in group.candidates}
    heads = [_headline(candidates[asset_id]) for asset_id in group.selected]
    head_reserve = [_headline(candidates[asset_id]) for asset_id in group.reserve]
    descs = [_description(item) for item in descriptions.descriptions]
    desc_reserve = [_description(item) for item in descriptions.reserve]

    pairs = combinatorics.enumerate_pairs(heads, descs)
    labels = await label_pairs(ctx, pairs)
    bad: dict[PairKey, tuple[str, ...]] = {}
    for a, b, kind in pairs:
        raised = combinatorics.flags(a, b, kind, rules)
        label = labels[(a.ref, b.ref)]
        reasons = (*raised, *((label,) if label in BAD_LABELS else ()))
        if reasons:
            bad[(a.ref, b.ref)] = reasons

    swaps: tuple[combinatorics.Swap, ...] = ()
    final_heads, final_descs = tuple(heads), tuple(descs)
    repaired = 0
    if bad and rounds:
        repair = combinatorics.repair_v1(
            heads, descs, head_reserve, desc_reserve, bad, quotas=quotas, rules=rules
        )
        swaps, final_heads, final_descs, repaired = (
            repair.swaps,
            repair.headlines,
            repair.descriptions,
            1,
        )

    final = combinatorics.enumerate_pairs(final_heads, final_descs)
    # The pairs a swapped-in asset forms are labelled once, so the report
    # covers every pair the ad can serve. They are not repaired: one round.
    labels.update(await label_pairs(ctx, [p for p in final if (p[0].ref, p[1].ref) not in labels]))
    report_pairs: list[Pair] = []
    order_dependent: list[tuple[Asset, Asset, Kind]] = []
    for a, b, kind in final:
        raised = combinatorics.flags(a, b, kind, rules)
        label = labels[(a.ref, b.ref)]
        report_pairs.append(
            Pair.model_validate(
                {"a": a.ref, "b": b.ref, "kind": kind, "flags": list(raised), "label": label}
            )
        )
        if label == "order_dependent" and not raised:
            order_dependent.append((a, b, kind))
    pins = combinatorics.pins_v1(order_dependent)

    unresolved = [
        (pair.a, pair.b) for pair in report_pairs if pair.flags or pair.label in BAD_LABELS
    ]
    report = PairReport(
        campaign_ref=group.campaign_ref,
        ad_group_ref=group.ad_group_ref,
        variant=group.variant,
        headlines=[uuid.UUID(asset.ref) for asset in final_heads],
        descriptions=[uuid.UUID(asset.ref) for asset in final_descs],
        pairs=report_pairs,
        swaps=[
            Swap(out=uuid.UUID(s.out), in_from_reserve=uuid.UUID(s.into), why=s.why) for s in swaps
        ],
        pins=[
            Pin(asset_id=uuid.UUID(pin.asset_ref), position=pin.position, why=pin.why)
            for pin in pins
        ],
        repair_rounds=repaired,
        unresolved=unresolved,
    )
    await ctx.progress(
        f"{group.campaign_ref} / {group.ad_group_ref} ({group.variant}): {len(pairs)} pairs, "
        f"{len(bad)} to "
        f"repair, {len(swaps)} swapped from reserve, {len(unresolved)} unresolved, "
        f"{len(pins)} pinned"
    )

    state: dict[uuid.UUID, tuple[CreativeAssetStatus, str | None, dict[str, Any] | None]] = {}
    carried = {asset.ref for asset in (*final_heads, *final_descs)}
    swapped_in = {s.into: s.out for s in swaps}
    pinned = {pin.asset_ref: pin.position for pin in pins}
    for asset in (*heads, *head_reserve, *descs, *desc_reserve):
        status = CreativeAssetStatus.LINTED if asset.ref in carried else CreativeAssetStatus.RESERVE
        lineage = (
            {"origin": "reserve_swap", "parent_id": swapped_in[asset.ref], "node_id": node_id}
            if asset.ref in swapped_in
            else None
        )
        state[uuid.UUID(asset.ref)] = (status, pinned.get(asset.ref), lineage)
    return Judged(report=report, state=state)


async def label_pairs(
    ctx: RunContext, pairs: Sequence[tuple[Asset, Asset, Kind]]
) -> dict[PairKey, PairLabel]:
    """CLASSIFY every pair, `CLASSIFY_BATCH` per call, one required enum each."""
    labels: dict[PairKey, PairLabel] = {}
    for start in range(0, len(pairs), CLASSIFY_BATCH):
        batch = pairs[start : start + CLASSIFY_BATCH]
        keys = [f"p{index:02d}" for index in range(len(batch))]
        schema = create_model(
            "PairLabelsDraft",
            __base__=_Draft,
            **{key: (PairLabel, ...) for key in keys},  # type: ignore[call-overload]
        )
        shown = {
            key: {"kind": kind, "a": a.text, "b": b.text}
            for key, (a, b, kind) in zip(keys, batch, strict=True)
        }
        answer = await ctx.complete(
            schema,
            system=SYSTEM,
            user="PAIRS:\n" + json.dumps(shown, ensure_ascii=False, indent=1),
            # Explicit: 4.2.4, a COPYWRITE node, labels B's pairs through here too.
            task_class=TaskClass.CLASSIFY,
        )
        for key, (a, b, _kind) in zip(keys, batch, strict=True):
            labels[(a.ref, b.ref)] = getattr(answer, key)
    return labels


def _descriptions(ctx: RunContext) -> ClaimBoundDescriptionsOutput:
    raw = ctx.output_of("4.2.2")
    creative = ctx.require_creative()
    now = ctx.run.started_at or datetime.now(UTC)
    licensed = frozenset(
        claim.claim_id for claim in briefs.licensed_claims(creative.linter.ruleset, now)
    )
    return ClaimBoundDescriptionsOutput.model_validate(
        raw, context={"licensed_claim_ids": licensed}
    )


def _headline(candidate: HeadlineCandidate) -> Asset:
    return Asset(
        ref=str(candidate.asset_id),
        role="headline",
        text=candidate.default_text,
        category=candidate.category,
        keyword_ref=candidate.keyword_ref,
        claim_ids=tuple(str(claim) for claim in candidate.claim_ids),
    )


def _description(item: DescriptionItem) -> Asset:
    return Asset(
        ref=str(item.asset_id),
        role="description",
        text=item.text,
        claim_ids=tuple(str(claim) for claim in item.claim_ids),
        claim_span=item.claim_span,
    )


def apply_state(
    rows: Iterable[CreativeAsset],
    state: Mapping[uuid.UUID, tuple[CreativeAssetStatus, str | None, dict[str, Any] | None]],
) -> None:
    """Set each judged row to its computed state — absolute, so a retry overwrites."""
    for row in rows:
        if row.id not in state:
            continue
        status, position, lineage = state[row.id]
        row.status = status
        row.pin_position = position
        row.lineage = lineage if lineage is not None else generated_lineage(row.node_id)


async def _apply(ctx: RunContext, judged: Sequence[Judged]) -> None:
    """4.2.1's and 4.2.2's rows, already written, set to what 4.2.3 judged."""
    state = {asset_id: entry for item in judged for asset_id, entry in item.state.items()}
    if not state:
        return
    rows = (
        (
            await ctx.db.execute(
                sa.select(CreativeAsset).where(
                    CreativeAsset.creative_run_id == ctx.run.id,
                    CreativeAsset.id.in_(list(state)),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(str(asset_id) for asset_id in set(state) - {row.id for row in rows})
    if missing:
        raise NodeContractError(
            f"4.2.1/4.2.2 name asset(s) this run has no row for: {', '.join(missing)}"
        )
    apply_state(rows, state)
    await ctx.db.flush()


COMBINATION_COHERENCE = CombinationCoherenceNode()
