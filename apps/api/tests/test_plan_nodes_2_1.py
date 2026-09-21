"""Stage 2.1 — what the nodes do with what they were given.

The property under test throughout is law 14: **no figure in a plan node's
output was produced by the model.** Every number here is traced back to the
`economics.max_cpa_v1` result the node cited, by value, so a node that started
scaling, rounding or averaging one would fail rather than merely look odd.

The scripted answers deliberately contain **no figures at all** — the model is
asked for a selector, a label or a rank and nothing else. That is the strongest
form of the test available: a node that got a number from the model could not,
because there was none to get.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.nodes import gather
from agent.nodes.base import collect_calc_evidence_ids, derived_ids
from agent.nodes.plan import stage_2_1
from agent.orchestrator.plan_calc import CalcNotPermitted
from agent.planning import crm
from tests import plan_support as support


@pytest.fixture
def stub_collect(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace `gather.collect` with a prepared `Gathered`.

    The real one reads Postgres and may pull a connector. What these tests are
    about is what a node does with what it got, so the read is stubbed here
    and `tests/integration/test_plan_stage_2_1.py` proves the read itself.
    """

    def install(found: gather.Gathered) -> None:
        async def collect(ctx: Any, *needs: gather.Need) -> gather.Gathered:
            return found

        monkeypatch.setattr(gather, "collect", collect)

    return install


# ---------------------------------------------------------------------------
# fixtures — round numbers, so every expectation below is checkable by hand
# ---------------------------------------------------------------------------

#: One segment: four wins averaging $20,000, one loss. 80% of every dollar is
#: gross profit, the CAC ratio is 3 and the safety margin is 15%.
#:
#:   gross profit  = 20,000 x 0.80             = 16,000
#:   max CPA (won) = 16,000 / 3                =  5,333.33  (5333.3333…)
#:   close rate    = 4 won of 5                =     80%
#:   max CPL       = 5,333.3333… x 0.80        =  4,266.67  (4266.6666…)
#:   target CPL    = 4,266.6666… x 0.85        =  3,626.67
#:
#: Rounding happens once, at the boundary — `calc/registry.money()` — never
#: between steps. Chaining the *rounded* 5,333.33 would give 4,266.66 and
#: 3,626.66, and the cent of difference is the whole reason PT3 asks for
#: byte-identical output from identical inputs.
WON = [
    {"account_name": f"Acme {index}", "industry": "Chemicals", "deal_value": value}
    for index, value in enumerate((26_000, 22_000, 18_000, 14_000))
]
LOST = [{"account_name": "Lost Co", "industry": "Chemicals", "close_reason": "price"}]

EXPECTED_MAX_CPA_WON = 5333.33
EXPECTED_MAX_CPL = 4266.67
EXPECTED_TARGET_CPL = 3626.67


def report() -> dict[str, Any]:
    return support.research_report(
        business_context={
            "products": [{"name": "SDS Manager", "gross_margin_pct": 80, "evidence_ids": []}],
            "segments": [
                {"label": "Plant operators", "share_of_revenue_pct": 60, "evidence_ids": []}
            ],
            "exclusions": [
                {
                    "persona": "Sole traders",
                    "disqualifier": "no compliance obligation",
                    "evidence_ids": [],
                }
            ],
        },
        demand_map={"total_keywords": 120},
    )


def gathered(*, actions: int = 1) -> gather.Gathered:
    rows = [support.evidence(crm.CRM_WON, row) for row in WON]
    rows.extend(support.evidence(crm.CRM_LOST, row) for row in LOST)
    rows.extend(
        support.evidence(
            stage_2_1.CONVERSION_ACTION,
            {
                "name": f"Demo request {index}",
                "conversion_action_id": f"{index}",
                "status": "ENABLED",
            },
        )
        for index in range(actions)
    )
    return gather.Gathered(evidence=rows)


def source(**overrides: Any) -> Any:
    return support.plan_input(report(), **overrides)


# The model is asked for a *selector*, never a value. Each scripted answer
# below therefore contains no figure the node could copy by accident.
TAXONOMY_ANSWER: dict[str, Any] = {
    "actions": [
        {
            "name": "Qualified lead",
            "ads_action_id": "0",
            "category": "qualified_lead",
            "counting": "one_per_click",
            "value_model": "fixed",
            "value_basis": "target_cpl",
            "primary": True,
            "include_in_conversions": True,
            "rationale": "Sales works every one of these.",
            "evidence_ids": [],
        },
        {
            "name": "Newsletter signup",
            "ads_action_id": None,
            "category": "signup",
            "counting": "every",
            "value_model": "none",
            "value_basis": "none",
            "primary": False,
            "include_in_conversions": False,
            "rationale": "Useful as a secondary signal only.",
            "evidence_ids": [],
        },
    ],
    "deprecate": [
        {"action": "Pageview", "reason": "Counts everybody who lands.", "evidence_ids": []}
    ],
    "ranking": ["Qualified lead", "Newsletter signup"],
}

METHOD_NOTES_ANSWER = {
    "method_notes": "The ceiling is gross profit over the target CAC ratio, times the close rate.",
    "caveats": ["One segment carries the whole account."],
}

TARGETS_ANSWER: dict[str, Any] = {
    "objectives": [
        {
            "campaign_ref": "nonbrand-us-lead-gen",
            "objective": "lead_gen",
            "primary_kpi": "cpl",
            "segment_ref": "Chemicals",
            "basis": "The whole book of business is chemicals.",
            "ramp": [{"month": 2, "phase": "steady"}, {"month": 1, "phase": "learning"}],
            "confidence": "medium",
            "evidence_ids": [],
        },
        {
            "campaign_ref": "brand-defense",
            "objective": "brand_defense",
            "primary_kpi": "cpa",
            "segment_ref": None,
            "basis": "Competitors bid on our name.",
            "ramp": [],
            "confidence": "high",
            "evidence_ids": [],
        },
    ],
    "north_star": {
        "metric": "cpl",
        "period": "monthly",
        "segment_ref": None,
        "rationale": "One number the account is judged on.",
    },
}

LEAD_ANSWER: dict[str, Any] = {
    "qualified_lead": {
        "required_signals": ["has a compliance obligation", "50+ employees"],
        "disqualifiers": ["sole trader"],
        "scoring": [
            {"signal": "has a compliance obligation", "weight": 5, "source_field": "industry"},
            {"signal": "50+ employees", "weight": 3, "source_field": "employee_count"},
        ],
        "threshold": 5,
    },
    "sla_response_hours": 4,
    "routing": [{"segment": "Chemicals", "owner": "EMEA desk"}],
    "observed_rejection_reasons": ["price"],
    "notes": "Sales rejects sole traders on sight.",
}


async def run_node(
    node: Any, *, stub_collect: Any, outputs: dict[str, Any] | None = None, **kwargs: Any
):
    """Drive one node the way the executor does: gather, then reason."""
    found = kwargs.pop("gathered", None) or gathered()
    stub_collect(found)
    harness = support.harness(
        node.spec.id,
        answers=kwargs.pop("answers"),
        gathered=found,
        source=kwargs.pop("source", None) or source(),
        permitted=node.spec.calc,
        outputs=outputs or {},
        **kwargs,
    )
    evidence = await node.gather(harness.ctx)
    output = await node.reason(harness.ctx, evidence)
    return harness, evidence, output


# ---------------------------------------------------------------------------
# 2.1.1 conversion_taxonomy
# ---------------------------------------------------------------------------


async def test_2_1_1_assigns_the_computed_value_the_model_selected(stub_collect: Any) -> None:
    _, evidence, output = await run_node(
        stage_2_1.conversion_taxonomy,
        stub_collect=stub_collect,
        answers={"TaxonomyDraft": TAXONOMY_ANSWER},
    )
    qualified, newsletter = output.actions
    assert qualified.name == "Qualified lead"
    assert qualified.value_basis == "target_cpl"
    assert qualified.assigned_value_usd == EXPECTED_TARGET_CPL
    assert qualified.lead_to_won_rate_pct == 80.0
    # `value_basis: none` means no value, not zero — a zero would read as "this
    # conversion is worth nothing", which is a different claim.
    assert newsletter.value_basis == "none"
    assert newsletter.assigned_value_usd is None


async def test_2_1_1_ranks_by_the_models_ordering(stub_collect: Any) -> None:
    _, _, output = await run_node(
        stage_2_1.conversion_taxonomy,
        stub_collect=stub_collect,
        answers={"TaxonomyDraft": TAXONOMY_ANSWER},
    )
    assert [action.rank for action in output.actions] == [1, 2]
    assert output.actions[0].primary is True


async def test_2_1_1_ranks_an_action_the_model_left_out_of_the_ordering(
    stub_collect: Any,
) -> None:
    """A dropped name must not become rank 0 and sort to the front."""
    answer = {**TAXONOMY_ANSWER, "ranking": ["Newsletter signup"]}
    _, _, output = await run_node(
        stage_2_1.conversion_taxonomy,
        stub_collect=stub_collect,
        answers={"TaxonomyDraft": answer},
    )
    ranks = {action.name: action.rank for action in output.actions}
    assert ranks["Newsletter signup"] == 1
    assert ranks["Qualified lead"] == 1, "an unranked action falls to the end of the list"
    assert min(ranks.values()) >= 1


async def test_2_1_1_cites_only_derived_rows_it_returned(stub_collect: Any) -> None:
    """The exact check the executor makes, made here so a node owns its own proof."""
    _, evidence, output = await run_node(
        stage_2_1.conversion_taxonomy,
        stub_collect=stub_collect,
        answers={"TaxonomyDraft": TAXONOMY_ANSWER},
    )
    cited = collect_calc_evidence_ids(output.model_dump(mode="json"))
    assert cited, "a node carrying numbers must cite a calculation"
    assert cited <= derived_ids(evidence)


# ---------------------------------------------------------------------------
# 2.1.2 unit_economics_ceiling
# ---------------------------------------------------------------------------


async def test_2_1_2_reports_the_computed_ceiling_verbatim(stub_collect: Any) -> None:
    _, _, output = await run_node(
        stage_2_1.unit_economics_ceiling,
        stub_collect=stub_collect,
        answers={"MethodNotes": METHOD_NOTES_ANSWER},
        outputs={"2.1.1": {}},
    )
    row = output.by_segment[0]
    assert row.segment == "Chemicals"
    assert row.deals == 4
    assert row.acv_usd == 20_000.0
    assert row.gross_margin_pct == 80.0
    assert row.lead_to_won_pct == 80.0
    assert row.max_cpa_won_usd == EXPECTED_MAX_CPA_WON
    assert row.max_cpl_usd == EXPECTED_MAX_CPL
    assert row.target_cpl_usd == EXPECTED_TARGET_CPL
    assert row.close_rate_basis == "segment"
    assert output.method_notes.startswith("The ceiling is gross profit")


async def test_2_1_2_has_no_payback_without_a_contract_term(stub_collect: Any) -> None:
    _, _, output = await run_node(
        stage_2_1.unit_economics_ceiling,
        stub_collect=stub_collect,
        answers={"MethodNotes": METHOD_NOTES_ANSWER},
        outputs={"2.1.1": {}},
    )
    assert output.by_segment[0].payback_months is None
    assert any(gap.startswith(crm.CONTRACT_TERM_KEY) for gap in output.open_gaps)


async def test_2_1_2_adds_payback_when_the_project_configured_a_term(
    stub_collect: Any,
) -> None:
    """Two calculations, two citations — and the payback CAC is the ceiling."""
    found = gathered()
    stub_collect(found)
    harness = support.harness(
        "2.1.2",
        answers={"MethodNotes": METHOD_NOTES_ANSWER},
        gathered=found,
        source=source(product_context={crm.CONTRACT_TERM_KEY: 12}),
        permitted=stage_2_1.unit_economics_ceiling.spec.calc,
        outputs={"2.1.1": {}},
    )
    harness.ctx.project.product_context = {crm.CONTRACT_TERM_KEY: 12}
    evidence = await stage_2_1.unit_economics_ceiling.gather(harness.ctx)
    output = await stage_2_1.unit_economics_ceiling.reason(harness.ctx, evidence)

    assert len(output.calc_evidence_ids) == 2
    row = output.by_segment[0]
    # 20,000 x 0.80 / 12 = 1,333.33 monthly gross profit; 5,333.33 / 1,333.33 = 4.
    assert row.payback_months == pytest.approx(4.0, abs=0.01)
    assert row.ltv_to_cac == pytest.approx(3.0, abs=0.01)
    assert harness.writer.calls == [
        "2.1.2:economics.max_cpa_v1",
        "2.1.2:economics.payback_v1",
    ]


async def test_2_1_2_asks_the_model_for_prose_and_nothing_else(stub_collect: Any) -> None:
    harness, _, _ = await run_node(
        stage_2_1.unit_economics_ceiling,
        stub_collect=stub_collect,
        answers={"MethodNotes": METHOD_NOTES_ANSWER},
        outputs={"2.1.1": {}},
    )
    assert [name for name, _ in harness.llm.prompts] == ["MethodNotes"]
    fields = set(stage_2_1.MethodNotes.model_fields)
    assert fields == {"method_notes", "caveats"}


# ---------------------------------------------------------------------------
# 2.1.3 campaign_targets — gate G1
# ---------------------------------------------------------------------------


async def test_2_1_3_selects_the_target_and_the_ceiling_for_the_chosen_kpi(
    stub_collect: Any,
) -> None:
    _, _, output = await run_node(
        stage_2_1.campaign_targets,
        stub_collect=stub_collect,
        answers={"CampaignTargetsDraft": TARGETS_ANSWER},
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    lead_gen, brand = output.objectives
    assert lead_gen.primary_kpi == "cpl"
    assert lead_gen.target_value == EXPECTED_TARGET_CPL
    assert lead_gen.ceiling_value == EXPECTED_MAX_CPL
    assert lead_gen.target_source == "computed"
    # A `cpa` campaign is bounded by the per-customer ceiling, not the per-lead one.
    assert brand.primary_kpi == "cpa"
    assert brand.ceiling_value == EXPECTED_MAX_CPA_WON


async def test_2_1_3_never_sets_a_target_above_its_ceiling(stub_collect: Any) -> None:
    """Critique assertion 3 of PRD §11, proved at the node that could break it."""
    _, _, output = await run_node(
        stage_2_1.campaign_targets,
        stub_collect=stub_collect,
        answers={"CampaignTargetsDraft": TARGETS_ANSWER},
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    for objective in output.objectives:
        assert objective.target_value is not None
        assert objective.ceiling_value is not None
        assert objective.target_value <= objective.ceiling_value


async def test_2_1_3_ramp_months_are_selected_never_interpolated(stub_collect: Any) -> None:
    _, _, output = await run_node(
        stage_2_1.campaign_targets,
        stub_collect=stub_collect,
        answers={"CampaignTargetsDraft": TARGETS_ANSWER},
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    ramp = output.objectives[0].ramp
    assert [stage.month for stage in ramp] == [1, 2], (
        "months are ordered, whatever order they came in"
    )
    assert ramp[0].phase == "learning"
    assert ramp[0].target == EXPECTED_MAX_CPL
    assert ramp[1].phase == "steady"
    assert ramp[1].target == EXPECTED_TARGET_CPL
    assert {stage.target for stage in ramp} <= {EXPECTED_MAX_CPL, EXPECTED_TARGET_CPL}


async def test_2_1_3_falls_back_to_the_blended_ceiling_for_an_unknown_segment(
    stub_collect: Any,
) -> None:
    """A model naming a segment that does not exist must not produce a null target."""
    answer = {
        **TARGETS_ANSWER,
        "objectives": [{**TARGETS_ANSWER["objectives"][0], "segment_ref": "Aerospace"}],
    }
    _, _, output = await run_node(
        stage_2_1.campaign_targets,
        stub_collect=stub_collect,
        answers={"CampaignTargetsDraft": answer},
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    assert output.objectives[0].target_value == EXPECTED_TARGET_CPL


async def test_2_1_3_is_gate_g1_and_2_1_4_is_gate_g2() -> None:
    assert stage_2_1.campaign_targets.spec.gate_key == "G1"
    assert stage_2_1.lead_definition.spec.gate_key == "G2"
    assert stage_2_1.campaign_targets.spec.gate is True
    assert stage_2_1.lead_definition.spec.gate is True


async def test_g2_does_not_wait_on_g1() -> None:
    """§5.3: G1 and G2 run in parallel. 2.1.4 must not depend on 2.1.2 or 2.1.3."""
    assert stage_2_1.lead_definition.spec.depends_on == ("2.1.1",)
    assert "2.1.3" not in stage_2_1.lead_definition.spec.depends_on


# ---------------------------------------------------------------------------
# 2.1.4 lead_definition — gate G2
# ---------------------------------------------------------------------------


async def test_2_1_4_measures_the_rate_it_can_and_names_the_one_it_cannot(
    stub_collect: Any,
) -> None:
    _, _, output = await run_node(
        stage_2_1.lead_definition,
        stub_collect=stub_collect,
        answers={"LeadDefinitionDraft": LEAD_ANSWER},
        outputs={"2.1.1": {}},
    )
    assert output.observed_sql_to_won_pct == 80.0
    assert output.expected_mql_to_sql_pct is None
    assert stage_2_1.MQL_GAP in output.open_gaps


async def test_2_1_4_keeps_the_models_ranks_and_policy(stub_collect: Any) -> None:
    _, _, output = await run_node(
        stage_2_1.lead_definition,
        stub_collect=stub_collect,
        answers={"LeadDefinitionDraft": LEAD_ANSWER},
        outputs={"2.1.1": {}},
    )
    assert output.qualified_lead is not None
    assert [signal.weight for signal in output.qualified_lead.scoring] == [5, 3]
    assert output.qualified_lead.threshold == 5
    assert output.sla_response_hours == 4
    assert output.observed_rejection_reasons == ["price"]


async def test_2_1_4_shows_the_model_the_close_reasons_but_returns_no_counts(
    stub_collect: Any,
) -> None:
    harness, _, output = await run_node(
        stage_2_1.lead_definition,
        stub_collect=stub_collect,
        answers={"LeadDefinitionDraft": LEAD_ANSWER},
        outputs={"2.1.1": {}},
    )
    prompt = harness.llm.user_prompt("LeadDefinitionDraft")
    assert "recurring close reasons on lost deals" in prompt
    assert '"deals": 1' in prompt
    assert all(isinstance(reason, str) for reason in output.observed_rejection_reasons)


# ---------------------------------------------------------------------------
# what every node refuses to do
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node",
    [
        stage_2_1.conversion_taxonomy,
        stage_2_1.unit_economics_ceiling,
        stage_2_1.campaign_targets,
        stage_2_1.lead_definition,
    ],
    ids=lambda node: node.spec.id,
)
async def test_no_ceiling_stops_the_node_and_names_the_field(node: Any, stub_collect: Any) -> None:
    """No margin in the research report: nothing is guessed and nothing is spent."""
    bare = support.research_report(
        business_context={"products": [{"name": "SDS Manager", "evidence_ids": []}]}
    )
    found = gathered()
    stub_collect(found)
    harness = support.harness(
        node.spec.id,
        answers={},
        gathered=found,
        source=support.plan_input(bare),
        permitted=node.spec.calc,
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    evidence = await node.gather(harness.ctx)
    with pytest.raises(stage_2_1.InsufficientPlanInput, match="gross_margin_pct"):
        await node.reason(harness.ctx, evidence)
    assert harness.llm.prompts == [], (
        "the model must not be called for a shape that cannot validate"
    )
    assert evidence, "the Stage 01 evidence is still returned, so the console shows what was read"


@pytest.mark.parametrize(
    "node",
    [
        stage_2_1.conversion_taxonomy,
        stage_2_1.unit_economics_ceiling,
        stage_2_1.campaign_targets,
        stage_2_1.lead_definition,
    ],
    ids=lambda node: node.spec.id,
)
async def test_a_node_cannot_call_a_formula_it_did_not_declare(
    node: Any, stub_collect: Any
) -> None:
    """The allow-list, exercised through the node rather than around it."""
    found = gathered()
    stub_collect(found)
    harness = support.harness(
        node.spec.id,
        answers={},
        gathered=found,
        source=source(),
        permitted=(),
        outputs={"2.1.1": {}, "2.1.2": {}},
    )
    with pytest.raises(CalcNotPermitted, match="economics.max_cpa_v1"):
        await node.gather(harness.ctx)


async def test_every_stage_2_1_node_declares_only_formulas_it_uses() -> None:
    """A spec listing a formula the node never calls is an allow-list nobody reads."""
    used = {
        "2.1.1": {stage_2_1.MAX_CPA},
        "2.1.2": {stage_2_1.MAX_CPA, stage_2_1.PAYBACK},
        "2.1.3": {stage_2_1.MAX_CPA},
        "2.1.4": {stage_2_1.MAX_CPA},
    }
    for node in (
        stage_2_1.conversion_taxonomy,
        stage_2_1.unit_economics_ceiling,
        stage_2_1.campaign_targets,
        stage_2_1.lead_definition,
    ):
        assert set(node.spec.calc) == used[node.spec.id]


async def test_a_plan_node_on_a_research_run_says_so(stub_collect: Any) -> None:
    """`ctx.require_plan()` names the node rather than raising AttributeError."""
    from agent.nodes.base import NodeContractError

    found = gathered()
    stub_collect(found)
    harness = support.harness(
        "2.1.1",
        answers={},
        gathered=found,
        source=source(),
        permitted=(stage_2_1.MAX_CPA,),
    )
    harness.ctx.plan = None
    with pytest.raises(NodeContractError, match="2.1.1"):
        await stage_2_1.conversion_taxonomy.gather(harness.ctx)


async def test_the_prompt_carries_the_plan_source_and_the_constants_version(
    stub_collect: Any,
) -> None:
    harness, _, _ = await run_node(
        stage_2_1.conversion_taxonomy,
        stub_collect=stub_collect,
        answers={"TaxonomyDraft": TAXONOMY_ANSWER},
    )
    prompt = harness.llm.user_prompt("TaxonomyDraft")
    assert str(support.RESEARCH_RUN_ID) in prompt
    assert support.CONSTANTS.version in prompt


async def test_degraded_sources_reach_the_prompt(stub_collect: Any) -> None:
    """§4.3 rule 4 — a forecast on degraded data must not read as confident."""
    found = gathered()
    stub_collect(found)
    harness = support.harness(
        "2.1.1",
        answers={"TaxonomyDraft": TAXONOMY_ANSWER},
        gathered=found,
        source=support.plan_input(
            support.research_report(
                business_context={
                    "products": [
                        {"name": "SDS Manager", "gross_margin_pct": 80, "evidence_ids": []}
                    ]
                },
                degraded_sources=["google_ads_forecast"],
            )
        ),
        permitted=(stage_2_1.MAX_CPA,),
    )
    evidence = await stage_2_1.conversion_taxonomy.gather(harness.ctx)
    await stage_2_1.conversion_taxonomy.reason(harness.ctx, evidence)
    assert "google_ads_forecast" in harness.llm.user_prompt("TaxonomyDraft")


async def test_all_four_nodes_share_one_calculation_within_a_run() -> None:
    """The dedupe that lets four nodes cite one `PlanCalc` row.

    Each node gets its own runner — that is how the allow-list is scoped — but
    they share a writer, exactly as they share one plan run. Four calls, one
    row, one citation.
    """
    writer = support.StubWriter()
    frame = crm.segments_frame(
        won=WON,
        lost=LOST,
        business_context=support.plan_input(report()).business_context,
        product_context={},
    ).frame
    ids: set[uuid.UUID] = set()
    for node_id in ("2.1.1", "2.1.2", "2.1.3", "2.1.4"):
        from agent.orchestrator.plan_calc import PlanCalcRunner

        runner = PlanCalcRunner(
            node_id=node_id,
            permitted=(stage_2_1.MAX_CPA,),
            writer=writer,  # type: ignore[arg-type]
            constants=support.CONSTANTS,
            session=writer,  # type: ignore[arg-type]
        )
        ids.add((await runner.run(stage_2_1.MAX_CPA, frame)).id)
    assert len(ids) == 1
    assert len(writer.rows) == 1
