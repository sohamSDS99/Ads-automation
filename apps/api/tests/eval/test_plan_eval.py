"""The plan eval: five golden `PlanInput` fixtures × §11's ten assertions (PQ3).

Stage 01's eval (`test_eval.py`) holds one *node output* per node and checks it
against the node's contract. This one holds one *input* per condition and
checks what the whole deterministic half of Stage 02 makes of it — the
assembler and the critique, both real. The two are complementary: that one
catches a prompt that drifted from its schema, this one catches a plan that is
schema-valid and wrong.

**Four axes, and then the negative controls.**

1. *Schema* — the fixture is a `PlanInput`, and the node outputs built from it
   validate against the registry's declared models.
2. *Assembly* — `plan_synthesis.assemble` produces a `CampaignPlan` that
   validates against §12.
3. *Critique* — `critique.run_checks` returns **nothing**. Not "no blocking
   issues": nothing. A warning in a golden fixture is a fixture nobody will
   look at twice, and §11's ten are all things a good plan does not do.
4. *Derivation* — the plan reflects **this** input. Markets, consent scope,
   launch blockers, degraded sources and the override reason each have to
   survive the crossing, and each is a §18 row where they historically did not.

**The negative controls are the point, again.** Ten mutations, one per §11
assertion, each of which must produce a blocking issue *from the check it is
aimed at*. Without them this file would pass if `run_checks` returned `[]`
unconditionally — which is precisely how the first version of Stage 01's eval
harness passed, and the reason its README says so.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent.export.plan_contract import CampaignPlan
from agent.orchestrator.registry import get_registry
from agent.planning import critique
from agent.planning.critique import CHECK_NAMES
from agent.schemas.plan_input import PlanInput
from tests.eval import plan_dag

FIXTURES = Path(__file__).parent / "plan"

#: A negative control: something that makes one node output wrong.
Mutation = Callable[[dict[str, Any]], None]

#: PRD §21 asks for five. Pinned so deleting one is a failing test rather than
#: a quietly smaller suite.
EXPECTED_CASES = 5


@dataclass(frozen=True, slots=True)
class Case:
    path: Path
    name: str
    why: str
    source: PlanInput

    def __str__(self) -> str:  # pragma: no cover — pytest id
        return self.name


def load_cases() -> list[Case]:
    cases = []
    for path in sorted(FIXTURES.glob("*.json")):
        raw = json.loads(path.read_text())
        cases.append(
            Case(
                path=path,
                name=raw["name"],
                why=raw["why"],
                source=PlanInput.model_validate(raw["plan_input"]),
            )
        )
    return cases


CASES = load_cases()
BY_NAME = {case.name: case for case in CASES}


def built(name: str, **kwargs: Any) -> plan_dag.Built:
    return plan_dag.build(BY_NAME[name].source, **kwargs)


def checks(plan: CampaignPlan, source: PlanInput) -> list[critique.Issue]:
    return critique.run_checks(plan, launch_blockers=source.launch_blockers)


def blocking_checks(issues: list[critique.Issue]) -> set[str]:
    return {issue.check for issue in issues if issue.severity == "blocking"}


# ---------------------------------------------------------------------------
# the suite exists and is the size it claims
# ---------------------------------------------------------------------------


def test_the_suite_has_five_cases() -> None:
    assert len(CASES) == EXPECTED_CASES


def test_every_case_says_what_it_is_for() -> None:
    for case in CASES:
        assert len(case.why.split()) >= 12, f"{case.name} has no useful `why`"


def test_the_cases_span_the_conditions_that_change_a_plan() -> None:
    """Five fixtures of the same shape would measure one path five times."""
    assert {case.name for case in CASES} == {
        "baseline_single_market",
        "multi_market_consent_blocked",
        "launch_blockers_carried",
        "degraded_forecast_override",
        "eu_only_no_brand",
    }


def test_a_fixture_is_exactly_a_plan_input() -> None:
    """No extra keys, no missing ones. `PlanInput` forbids extras, so this is
    really asserting that the fixture was written through the contract and not
    around it."""
    for case in CASES:
        raw = json.loads(case.path.read_text())
        assert set(raw) == {"name", "why", "plan_input"}
        assert PlanInput.model_validate(raw["plan_input"]) == case.source


# ---------------------------------------------------------------------------
# axis 1 — schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_every_node_output_validates_against_its_contract(case: Case) -> None:
    """The outputs the harness builds are shapes a node could really produce."""
    registry = get_registry()
    result = plan_dag.build(case.source)
    for node_id, payload in result.outputs.items():
        model = registry.spec(node_id).output_model
        assert model is not None
        model.model_validate(payload)


# ---------------------------------------------------------------------------
# axis 2 — assembly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_plan_round_trips_through_the_contract(case: Case) -> None:
    """Validate, dump to JSON, validate again.

    JSON and not python: `Number.value` is a `Decimal` that pydantic serialises
    as a **string**, and a field that survives in memory and dies in JSONB is a
    class of defect this project has shipped once already.
    """
    plan = plan_dag.build(case.source).plan
    again = CampaignPlan.model_validate(json.loads(plan.model_dump_json()))
    assert again.model_dump(mode="json") == plan.model_dump(mode="json")


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_plan_is_deterministic(case: Case) -> None:
    """PT3. Same input, same constants, byte-identical output."""
    first = plan_dag.build(case.source).plan.model_dump_json()
    second = plan_dag.build(case.source).plan.model_dump_json()
    assert first == second


# ---------------------------------------------------------------------------
# axis 3 — the ten assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_plan_passes_every_critique_assertion(case: Case) -> None:
    """PQ3, in one line. Not "no blocking issues" — nothing at all."""
    result = plan_dag.build(case.source)
    issues = checks(result.plan, case.source)
    assert issues == [], "\n".join(
        f"{issue.severity} {issue.check}: {issue.finding}" for issue in issues
    )


def test_the_ten_names_are_the_ten_checks() -> None:
    """A check added without a name — or a name with no check — changes what
    "all ten passed" means, silently."""
    assert len(CHECK_NAMES) == 10
    assert len(set(CHECK_NAMES)) == 10


# ---------------------------------------------------------------------------
# axis 4 — the plan is a function of *this* input
# ---------------------------------------------------------------------------


def test_every_market_in_the_plan_came_from_the_input() -> None:
    for case in CASES:
        result = plan_dag.build(case.source)
        declared = {market.country.upper() for market in case.source.markets}
        planned = {
            campaign.market.strip().upper()
            for campaign in result.plan.account_structure.campaigns
            if campaign.market
        }
        assert planned <= declared, f"{case.name} planned a market nobody asked for"


def test_a_refused_market_reaches_no_audience_surface() -> None:
    """PC1, end to end, on the fixture built for it."""
    case = BY_NAME["multi_market_consent_blocked"]
    result = plan_dag.build(case.source)
    plan = result.plan

    assert result.consent.markets_blocked == ("DE",)
    assert "DE" in plan.measurement_plan.consent_markets_blocked
    assert "DE" not in plan.measurement_plan.consent_markets_allowed

    audience = {
        entry.market.upper()
        for entry in plan.channel_slate.slate
        if critique._is_audience_channel(entry.campaign_type)
    }
    assert "DE" not in audience
    assert {"US", "FR"} <= audience

    # ...and it still gets a search campaign. Consent governs audience lists,
    # not keywords, and a fixture that refused the market outright would be
    # asserting a rule the PRD does not have.
    search = {campaign.market.upper() for campaign in plan.account_structure.campaigns}
    assert "DE" in search


def test_every_launch_blocker_survives_the_crossing() -> None:
    case = BY_NAME["launch_blockers_carried"]
    result = plan_dag.build(case.source)
    carried = {item.task.strip().lower() for item in result.plan.open_dependencies}
    assert len(case.source.launch_blockers) == 3
    for blocker in case.source.launch_blockers:
        assert blocker.statement.strip().lower() in carried
    assert {item.source for item in result.plan.open_dependencies} >= {"research"}


def test_a_degraded_source_and_an_override_are_both_carried() -> None:
    """§18's two "the plan must say so" rows, on one fixture."""
    case = BY_NAME["degraded_forecast_override"]
    result = plan_dag.build(case.source)
    assert result.plan.media_plan.degraded_sources == ["google_ads_forecast"]
    assert result.plan.source.degraded_sources == ["google_ads_forecast"]
    assert result.plan.source.launch_readiness == "no_go"
    assert result.plan.source.override_reason == case.source.override_reason
    assert result.plan.source.override_reason


def test_an_eu_scope_names_its_consent_mechanism() -> None:
    """§13: EU markets in scope means the mechanism is named, not assumed."""
    case = BY_NAME["eu_only_no_brand"]
    signal = plan_dag.build(case.source).plan.measurement_plan.consent_signal
    assert signal.get("required") is True
    assert signal.get("mechanism")
    assert set(signal.get("markets") or []) == {"DE", "NL"}

    # ...and a non-EU scope says it is not required rather than going quiet.
    other = plan_dag.build(BY_NAME["baseline_single_market"].source)
    assert other.plan.measurement_plan.consent_signal.get("required") is False


def test_a_plan_with_no_brand_has_no_isolation_section() -> None:
    """Assertion 5 passes by returning early, not by finding nothing."""
    result = plan_dag.build(BY_NAME["eu_only_no_brand"].source)
    assert result.plan.channel_slate.brand_isolation is None
    assert critique.check_brand_isolation(result.plan) == []


def test_every_figure_in_every_plan_resolves_to_a_calculation() -> None:
    """PT1, over all five. `Number` exists to make this checkable."""
    for case in CASES:
        plan = plan_dag.build(case.source).plan
        numbers = plan.numbers()
        assert numbers, f"{case.name} states no traceable figure at all"
        for number in numbers:
            assert number.calc_evidence_id is not None, f"{case.name}: {number.label}"


def test_every_claim_in_every_plan_is_cited() -> None:
    """PT2."""
    for case in CASES:
        plan = plan_dag.build(case.source).plan
        for claim in (*plan.assumptions, *plan.risks):
            assert claim.evidence_ids, f"{case.name}: {claim.statement}"


# ---------------------------------------------------------------------------
# the negative controls — ten mutations, ten assertions
# ---------------------------------------------------------------------------


def _first_campaign(outputs: dict[str, Any]) -> dict[str, Any]:
    campaigns = outputs["2.4.2"]["campaigns"]
    return campaigns[0]


def _nonbrand(outputs: dict[str, Any]) -> dict[str, Any]:
    for campaign in outputs["2.4.2"]["campaigns"]:
        if "Nonbrand" in campaign["name"]:
            return campaign
    raise AssertionError("the fixture has no non-brand campaign")


def _brand(outputs: dict[str, Any]) -> dict[str, Any]:
    for campaign in outputs["2.4.2"]["campaigns"]:
        if "Brand" in campaign["name"] and "Nonbrand" not in campaign["name"]:
            return campaign
    raise AssertionError("the fixture has no brand campaign")


def _break_allocation(outputs: dict[str, Any]) -> None:
    outputs["2.2.4"]["allocation"][0]["usd"] *= 2


def _break_objectives(outputs: dict[str, Any]) -> None:
    outputs["2.1.3"]["objectives"] = []


def _break_ceiling(outputs: dict[str, Any]) -> None:
    row = outputs["2.1.3"]["objectives"][0]
    row["target_value"] = row["ceiling_value"] + 1


def _break_keyword_hygiene(outputs: dict[str, Any]) -> None:
    """The same term in two ad groups — the one §18 calls out by name."""
    source = _nonbrand(outputs)["ad_groups"][0]["keywords"][0]
    _brand(outputs)["ad_groups"][0]["keywords"].append(dict(source))


def _break_landing_urls(outputs: dict[str, Any]) -> None:
    _first_campaign(outputs)["ad_groups"][0]["landing_url"] = ""


def _break_brand_isolation(outputs: dict[str, Any]) -> None:
    term = outputs["2.3.3"]["brand_terms"][0]["term"]
    group = _nonbrand(outputs)["ad_groups"][0]
    group["keywords"].append(
        {"term": term, "match_type": "exact", "forecast_cpc_usd": 1.2, "search_volume": 400}
    )
    _nonbrand(outputs)["negatives"] = []


def _break_learning_capacity(outputs: dict[str, Any]) -> None:
    outputs["2.2.4"]["learning_warnings"] = [
        {
            "campaign_ref": _first_campaign(outputs)["campaign_ref"],
            "forecast_conv_30d": 4.0,
            "threshold": 15.0,
            "verdict": "below",
            "remedy": None,
        }
    ]


def _break_naming(outputs: dict[str, Any]) -> None:
    _first_campaign(outputs)["name"] = "whatever the model felt like"


def _break_consent(outputs: dict[str, Any]) -> None:
    """An audience channel in a market gate 1.5.3 refused."""
    outputs["2.3.1"]["slate"].append(
        {
            "campaign_type": "demand_gen",
            "market": "DE",
            "campaign_refs": [],
            "launch_wave": 2,
            "rationale": "Somebody wanted the reach.",
            "prerequisites": [],
            "est_share_of_budget_pct": 0.0,
            "est_monthly_usd": 0.0,
        }
    )


#: `(check, fixture, mutation, build kwargs)`. One row per way an assertion can
#: be made to fire. `test_every_assertion_has_a_negative_control` checks the
#: set against `CHECK_NAMES` rather than against a count, so a check added to
#: §11 without a control here fails this file.
CONTROLS: tuple[tuple[str, str, Mutation | None, dict[str, Any]], ...] = (
    ("1_allocation_sums", "baseline_single_market", _break_allocation, {}),
    ("2_campaigns_complete", "baseline_single_market", _break_objectives, {}),
    ("3_targets_within_ceilings", "baseline_single_market", _break_ceiling, {}),
    ("4_keyword_hygiene", "baseline_single_market", _break_keyword_hygiene, {}),
    ("4_keyword_hygiene", "baseline_single_market", _break_landing_urls, {}),
    ("5_brand_isolation", "baseline_single_market", _break_brand_isolation, {}),
    ("6_learning_capacity", "baseline_single_market", _break_learning_capacity, {}),
    # Not a node mutation: an uncited claim is something the *narrative* model
    # returned, and `assemble` takes the claims as an argument.
    ("7_traceability", "baseline_single_market", None, {"cite": False}),
    ("8_consent", "multi_market_consent_blocked", _break_consent, {}),
    ("9_naming", "baseline_single_market", _break_naming, {}),
)


@pytest.mark.parametrize(
    ("check", "fixture", "mutation", "kwargs"),
    CONTROLS,
    ids=[f"{row[0]}-{(row[2].__name__ if row[2] else 'uncited')}" for row in CONTROLS],
)
def test_a_broken_plan_fails_the_assertion_aimed_at_it(
    check: str, fixture: str, mutation: Mutation | None, kwargs: dict[str, Any]
) -> None:
    case = BY_NAME[fixture]
    broken = plan_dag.build(case.source, mutate=mutation, **kwargs)
    found = blocking_checks(checks(broken.plan, case.source))
    assert check in found, f"{check} did not fire; got {sorted(found) or 'nothing'}"


def test_dropping_a_blocker_from_the_plan_fails_assertion_ten() -> None:
    """Assertion 10's control, which cannot be a node mutation.

    `_dependencies` builds `open_dependencies` from the launch blockers
    directly, so no node output can lose one. What *can* lose one is the plan
    being checked against research it was not built from — which is exactly the
    §4.4 staleness case, and is why the check takes the blockers as an argument
    rather than reading them off the plan.
    """
    case = BY_NAME["baseline_single_market"]
    other = BY_NAME["launch_blockers_carried"]
    plan = plan_dag.build(case.source).plan
    issues = critique.run_checks(plan, launch_blockers=other.source.launch_blockers)
    assert "10_launch_blockers" in blocking_checks(issues)


def test_every_assertion_has_a_negative_control() -> None:
    """The control that guards the controls."""
    covered = {row[0] for row in CONTROLS} | {"10_launch_blockers"}
    assert covered == set(CHECK_NAMES), f"uncontrolled: {sorted(set(CHECK_NAMES) - covered)}"
