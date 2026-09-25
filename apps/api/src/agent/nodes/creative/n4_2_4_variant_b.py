"""4.2.4 `variant_b` — a second RSA per ad group, from the brief's other angle (PRD §11 4.2.4).

For every Search ad group 4.2.3 judged, variant B is written through **the
same path A was**, not a second implementation of it:

1. 4.2.1's `spread` — the headline pool, its lint, its own checks and
   `select.headlines_v1` — led by the ad group's `angle_b` (decided up front in
   the G7-approved brief) instead of its primary message, and shown A's final
   copy to stay clear of;
2. 4.2.2's `describe` — claim-bound descriptions, the two paths and B's own
   exception candidates — the same way;
3. 4.2.3's `judge` — every pair B can serve, one repair round, pins only for
   `order_dependent` pairs.

Then B is measured against A: `distinctness_vs_a` is `copy.distinctness_v1`
between the copy each ad now carries, and the output schema holds it to
`copy.variant_min_distance`. **A B that paraphrases A fails validation**: the
node fails — the executor's bounded retry writes B afresh — rather than ship
an A/B test of one message.

`hypothesis` states the test in the brief's own approved words, B's angle
against A's primary message, and `primary_metric` is the ad group's KPI from
the frozen plan (the brief's `kpi`). Code states both; the model writes
neither, so neither can assert something no source gave.

Nothing is written until every ad group's B has been built, judged and
measured: a B that fails leaves no rows, and an attempt that follows a failed
one first clears what that attempt wrote.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from agent.creative import metrics
from agent.db.models import CreativeAsset, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._ad_groups import search_slots, where
from agent.nodes.creative._text_assets import clear_earlier_attempts
from agent.nodes.creative.n4_2_1_headline_spread import headline_spec, spread
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import (
    describe,
    description_specs,
    licensed_or_fail,
)
from agent.nodes.creative.n4_2_3_combination_coherence import (
    apply_state,
    flag_rules,
    judge,
    repair_rounds,
)
from agent.schemas.creative_brief import AdGroupBrief, CreativeBrief
from agent.schemas.search_ads import (
    ClaimBoundDescriptionsOutput,
    CombinationCoherenceOutput,
    DescriptionGroup,
    HeadlineGroup,
    HeadlineSpreadOutput,
    PairReport,
    Variant,
    VariantBGroup,
    VariantBOutput,
)

NODE_ID = "4.2.4"


# ---------------------------------------------------------------------------
# the part that decides — pure, and tested on its own
# ---------------------------------------------------------------------------


def hypothesis(group: AdGroupBrief) -> str:
    """The test B runs, in the brief's words: its angle against A's message, on the KPI."""
    return (
        f"Variant B, led by “{group.angle_b.text}”, beats variant A, led by "
        f"“{group.primary_message.text}”, on {group.kpi}."
    )


def ad_ref(campaign_ref: str, ad_group_ref: str, variant: Variant) -> str:
    return f"{campaign_ref}/{ad_group_ref}/{variant}"


def ad_texts(
    report: PairReport, headlines: HeadlineGroup, descriptions: DescriptionGroup
) -> list[str]:
    """What an ad carries, as text: its headlines (as Google counts them), then its descriptions."""
    heads = {candidate.asset_id: candidate.default_text for candidate in headlines.candidates}
    descs = {
        item.asset_id: item.text for item in (*descriptions.descriptions, *descriptions.reserve)
    }
    missing = [str(a) for a in report.headlines if a not in heads] + [
        str(a) for a in report.descriptions if a not in descs
    ]
    if missing:
        raise NodeContractError(
            f"the {report.variant} ad for {report.campaign_ref} / {report.ad_group_ref} carries "
            f"asset(s) its own copy does not have: {', '.join(missing)}"
        )
    return [heads[a] for a in report.headlines] + [descs[a] for a in report.descriptions]


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------


class VariantBNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="variant_b",
        stage="4.2",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.2.3",),
        task_class=TaskClass.COPYWRITE,
        input_model=CombinationCoherenceOutput,
        output_model=VariantBOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # A's copy and ads are the outputs of 4.2.1–4.2.3, all upstream of 4.2.3.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        copy = creative.constants.copy_
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        slots = search_slots(brief, creative.input.account_structure.campaigns)
        await clear_earlier_attempts(ctx, NODE_ID)
        if not slots:
            return VariantBOutput()

        # The run's start, not the wall clock: B is linted against — and
        # offered — exactly what A was.
        now = ctx.run.started_at or datetime.now(UTC)
        ruleset = creative.linter.ruleset
        licensed = licensed_or_fail(ruleset, now)
        context = {"licensed_claim_ids": frozenset(claim.claim_id for claim in licensed)}
        headlines_a = {
            (g.campaign_ref, g.ad_group_ref): g
            for g in HeadlineSpreadOutput.model_validate(ctx.output_of("4.2.1")).ad_groups
            if g.variant == "A"
        }
        descriptions_a = {
            (g.campaign_ref, g.ad_group_ref): g
            for g in ClaimBoundDescriptionsOutput.model_validate(
                ctx.output_of("4.2.2"), context=context
            ).ad_groups
            if g.variant == "A"
        }
        ads_a = {
            (ad.campaign_ref, ad.ad_group_ref): ad
            for ad in CombinationCoherenceOutput.model_validate(ctx.output_of("4.2.3")).ads
            if ad.variant == "A"
        }
        headline = headline_spec(ruleset)
        specs = description_specs(ruleset)
        rounds = repair_rounds(copy)
        threshold = copy.variant_min_distance.value

        groups: list[VariantBGroup] = []
        rows: list[CreativeAsset] = []
        for slot in slots:
            key = (slot.brief.campaign_ref, slot.brief.ad_group_ref)
            if key not in ads_a or key not in headlines_a or key not in descriptions_a:
                raise NodeContractError(
                    f"{where(slot)}: 4.2.1–4.2.3 left no variant A ad to write B against"
                )
            a_texts = ad_texts(ads_a[key], headlines_a[key], descriptions_a[key])

            heads = await spread(
                ctx,
                brief,
                slot,
                headline,
                licensed,
                now,
                node_id=NODE_ID,
                variant="B",
                avoid=a_texts,
            )
            descs = await describe(
                ctx,
                brief,
                slot,
                specs,
                licensed,
                now,
                node_id=NODE_ID,
                variant="B",
                avoid=a_texts,
            )
            judged = await judge(
                ctx,
                heads.group,
                descs.group,
                node_id=NODE_ID,
                rounds=rounds,
                quotas=dict(copy.headline_quotas.value),
                rules=flag_rules(copy, heads.group),
            )
            distinct = metrics.distinctness(
                a_texts, ad_texts(judged.report, heads.group, descs.group)
            )
            stated = hypothesis(slot.brief)
            try:
                group = VariantBGroup.model_validate(
                    {
                        "campaign_ref": slot.brief.campaign_ref,
                        "ad_group_ref": slot.brief.ad_group_ref,
                        "headlines": heads.group,
                        "descriptions": descs.group,
                        "ad_b": {
                            "ad_ref": ad_ref(*key, "B"),
                            "campaign_ref": slot.brief.campaign_ref,
                            "ad_group_ref": slot.brief.ad_group_ref,
                            "variant": "B",
                            "angle": slot.brief.angle_b.text,
                            "hypothesis": stated,
                            "headlines": judged.report.headlines,
                            "descriptions": judged.report.descriptions,
                            "paths": descs.group.paths,
                            "final_url": slot.brief.landing_url,
                            "pair_report": judged.report,
                            "distinctness_vs_a": distinct,
                        },
                        "distinctness_vs_a": distinct,
                        "variant_min_distance": threshold,
                        "hypothesis": stated,
                        "primary_metric": slot.brief.kpi,
                    },
                    context=context,
                )
            except ValidationError as exc:
                reasons = "; ".join(str(error["msg"]) for error in exc.errors())
                raise NodeContractError(f"{where(slot)}: variant B is invalid — {reasons}") from exc

            batch = [*heads.rows, *descs.rows]
            apply_state(batch, judged.state)
            rows.extend(batch)
            groups.append(group)
            await ctx.progress(
                f"{where(slot)} (B): copy.distinctness_v1 {distinct:.2f} from A "
                f"(at least {threshold:.2f}); primary metric {slot.brief.kpi}"
            )

        ctx.db.add_all(rows)
        await ctx.db.flush()
        return VariantBOutput(ad_groups=groups)


VARIANT_B = VariantBNode()
