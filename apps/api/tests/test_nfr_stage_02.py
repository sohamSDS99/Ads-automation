"""Stage 02 §17, one row at a time — and the index that says where each lives.

S2-P7's exit criterion is "every §17 threshold has a test that would fail if
regressed". That is two claims, and only the second is interesting: it is easy
to write a test *about* a threshold that cannot fail. So this file does two
things.

**`NFRS` is the index.** Twenty rows, one per §17 id, each naming the test that
holds it — most of them elsewhere, because the right place for PS2 is the
freeze suite and the right place for PQ1 is the export suite. `test_the_index
_covers_every_threshold` checks the table against §17's own list, and
`test_every_named_holder_exists` imports each named module and looks the
function up, so a renamed or deleted test fails here rather than silently
leaving a threshold uncovered.

**The rest of the file is the thresholds that had no test at all.** The audit
that opened S2-P7 found seven: PF1, PF2, PF4, PT2, PR2, PQ2 and PQ3. PQ3 is the
plan eval; PQ2 is a `make` gate; the other five are below. Two of them found
real defects while being written, which is the argument for the exercise:

* **PF4's hard cap was not enforced.** `max_plan_cost_usd` lived in `config.py`
  and was read by nothing, so a plan run was billed against research's $15
  ceiling — nearly twice what §17 states.
* **PT2 is enforced by the contract, not by the critique.** `Claim.evidence_ids`
  carries `min_length=1`, so an uncited claim cannot exist in a validated plan
  and the critique's branch for one is unreachable. The regression test
  therefore asserts on `Claim`, which is where the rule actually lives.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent.config import Settings
from agent.db.models import RunStage
from agent.export.contract import Claim
from agent.export.plan_contract import Number
from agent.orchestrator.dag import get_dag

# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

#: `{id: (what it requires, "module::function" that would fail if regressed)}`.
NFRS: dict[str, tuple[str, str]] = {
    "PF1": (
        "A full 20-node plan run finishes inside 20 minutes of machine time.",
        "tests.test_nfr_stage_02::test_pf1_the_plan_dag_stays_wide_enough_to_finish_in_time",
    ),
    "PF2": (
        "A gate approval resumes the branch within 5 seconds.",
        "tests.test_nfr_stage_02::test_pf2_a_decided_gate_enqueues_the_run_in_the_same_request",
    ),
    "PF3": (
        "A budget what-if recalculation round trip is under 2 seconds.",
        "tests.test_calc_allocation::test_a_what_if_over_forty_campaigns_by_four_markets_is_still_one_pass",
    ),
    "PF4": (
        "A plan run costs at most $6 at default routing, hard-capped at $8.",
        "tests.test_nfr_stage_02::test_pf4_a_plan_run_is_capped_by_its_own_ceiling",
    ),
    "PT1": (
        "Every Number resolves to a PlanCalc row.",
        "tests.test_nfr_stage_02::test_pt1_a_figure_cannot_exist_without_its_calculation",
    ),
    "PT2": (
        "Every Claim carries at least one resolvable evidence id.",
        "tests.test_nfr_stage_02::test_pt2_a_claim_cannot_exist_without_a_citation",
    ),
    "PT3": (
        "Identical input and constants produce byte-identical node outputs.",
        "tests.eval.test_plan_eval::test_the_plan_is_deterministic",
    ),
    "PT4": (
        "Arithmetic outside calc/ fails a CI grep.",
        "tests.test_calc_isolation::test_the_guard_exits_zero_on_the_real_tree",
    ),
    "PS1": (
        "A plan run makes zero Google Ads mutate calls.",
        "tests.test_google_ads_forecast::test_the_connector_stays_read_only",
    ),
    "PS2": (
        "An UPDATE to a frozen plan's payload, markdown or version is refused.",
        "tests.integration.test_plan_freeze::test_the_database_refuses_to_rewrite_a_frozen_plans_payload",
    ),
    "PS3": (
        "Every new route carries require(Permission); the 4-role matrix is green.",
        "tests.integration.test_authz_matrix::test_the_matrix_covers_every_guarded_route_in_the_application",
    ),
    "PS4": (
        "Acceptance, start, each gate decision, each override and the freeze "
        "each write an AuditLog row in the same transaction as the change.",
        "tests.integration.test_plan_freeze::test_the_freeze_writes_an_audit_row_in_the_same_transaction",
    ),
    "PC1": (
        "No market the consent gate refused appears in an audience plan.",
        "tests.eval.test_plan_eval::test_a_refused_market_reaches_no_audience_surface",
    ),
    "PC2": (
        "No raw CRM row reaches a prompt or the plan payload.",
        "tests.test_pii_canary::test_no_canary_reaches_the_plan_payload_or_any_export",
    ),
    "PR1": (
        "A degraded connector never stops the run and is carried into the plan.",
        "tests.eval.test_plan_eval::test_a_degraded_source_and_an_override_are_both_carried",
    ),
    "PR2": (
        "Zero completed nodes re-execute after an API or worker crash mid-run.",
        "tests.integration.test_plan_resume::test_a_plan_run_killed_mid_dag_re_executes_nothing_it_finished",
    ),
    "PR3": (
        "An unsupported research_schema_version fails at trigger time with 422.",
        "tests.integration.test_plan_handshake::test_e2_an_unsupported_research_schema_names_both_versions",
    ),
    "PQ1": (
        "EDITOR_CSV imports with zero errors and matching entity counts.",
        "tests.test_plan_exports::test_the_imported_counts_equal_the_plans_own_counts",
    ),
    "PQ2": (
        "Coverage is at least 85% on calc/ and 80% on nodes/plan, planning, export.",
        "tests.test_nfr_stage_02::test_pq2_the_coverage_gate_names_the_packages_and_the_floors",
    ),
    "PQ3": (
        "Five golden PlanInput fixtures pass every §11 critique assertion.",
        "tests.eval.test_plan_eval::test_the_plan_passes_every_critique_assertion",
    ),
}

#: §17's own list, in its own order. Written out rather than derived from
#: `NFRS`, because a table that defines its own completeness criterion measures
#: nothing.
SECTION_17 = (
    "PF1", "PF2", "PF3", "PF4",
    "PT1", "PT2", "PT3", "PT4",
    "PS1", "PS2", "PS3", "PS4",
    "PC1", "PC2",
    "PR1", "PR2", "PR3",
    "PQ1", "PQ2", "PQ3",
)  # fmt: skip


def test_the_index_covers_every_threshold() -> None:
    assert tuple(NFRS) == SECTION_17


def test_every_threshold_says_what_it_requires() -> None:
    for key, (requirement, _) in NFRS.items():
        assert len(requirement.split()) >= 6, f"{key} has no useful requirement"


@pytest.mark.parametrize("key", SECTION_17)
def test_every_named_holder_exists(key: str) -> None:
    """The index points at a real test, or it points at nothing.

    Import the module and look the function up. A holder that was renamed in a
    refactor takes its threshold's coverage with it, quietly, and this is the
    only thing that notices.
    """
    _, holder = NFRS[key]
    module_name, _, function_name = holder.partition("::")
    module = importlib.import_module(module_name)
    assert hasattr(module, function_name), f"{key}: {holder} does not exist"
    assert callable(getattr(module, function_name))


# ---------------------------------------------------------------------------
# PF1 — a full plan run finishes in 20 minutes
# ---------------------------------------------------------------------------

#: §11's wave count for the 20-node plan DAG. Nothing in the code enforces a
#: per-node wall clock, so what actually decides PF1 is how much of the DAG
#: runs in parallel — 12 waves at a minute or two each is 20 minutes, and 20
#: waves is not.
PLAN_WAVES = 12
PLAN_NODES = 20

#: 20 minutes over the critical path. Not a timeout the code enforces; the
#: budget a reader can hold each node to when this test fails.
PF1_BUDGET_S_PER_WAVE = 20 * 60 / PLAN_WAVES


def test_pf1_the_plan_dag_stays_wide_enough_to_finish_in_time() -> None:
    """PF1 is a wall-clock number, and wall clock here is the critical path.

    A node added on the end of the chain costs a whole wave; the same node
    added beside an existing one costs nothing. So the regression this guards
    is the one that actually happens: somebody adds a dependency for
    convenience and the run gets a minute longer, twelve times over. If this
    fails, either widen the DAG or re-justify PF1 with a measurement.
    """
    dag = get_dag(RunStage.PLAN)
    waves = dag.waves()
    assert sum(len(wave) for wave in waves) == PLAN_NODES
    assert len(waves) == PLAN_WAVES, (
        f"the plan DAG is {len(waves)} waves deep, not {PLAN_WAVES}. At "
        f"{PF1_BUDGET_S_PER_WAVE:.0f}s a wave that is "
        f"{len(waves) * PF1_BUDGET_S_PER_WAVE / 60:.0f} minutes against PF1's 20."
    )
    # ...and the four gates sit on the critical path by design (§11), which is
    # why PF1 excludes the human wait. If a gate ever stopped being a wave
    # boundary the number would flatter itself.
    assert waves[0] == ("2.1.1",)
    assert waves[-1] == ("2.6.2",)


def test_pf1_the_research_dag_is_untouched_by_the_plan_one() -> None:
    """Two stages, two DAGs, one registry. A plan node leaking into the
    research DAG would make every research run longer and is the cheapest way
    to break PF1's Stage 01 equivalent."""
    research = {node for wave in get_dag(RunStage.RESEARCH).waves() for node in wave}
    assert all(not node.startswith("2.") for node in research)


# ---------------------------------------------------------------------------
# PF2 — a gate approval resumes the branch in 5 seconds
# ---------------------------------------------------------------------------


def test_pf2_a_decided_gate_enqueues_the_run_in_the_same_request() -> None:
    """Five seconds is a budget only if nothing is polling.

    `decide_approval` resumes inline — it calls `_resume`, which enqueues the
    run before the response is written. There is no interval to tune and no
    cron to wait for, which is *why* PF2 is achievable, and this test is what
    notices if resumption is ever moved onto a poller. Asserted against the
    source rather than by timing a request: a stopwatch on a laptop measures
    the laptop.
    """
    from agent.api import routes_approvals

    source = routes_approvals.__doc__ or ""
    assert "resumes" in source

    import inspect

    decide = inspect.getsource(routes_approvals.decide_approval)
    resume = inspect.getsource(routes_approvals._resume)
    assert "_resume(" in decide, "deciding a gate no longer resumes the run inline"
    assert "enqueue_run" in resume, "the resume path no longer enqueues the run itself"
    # A poller would have to sleep, and a sleep in this path is the regression.
    assert "sleep" not in resume


# ---------------------------------------------------------------------------
# PF4 — a plan run is capped at $8
# ---------------------------------------------------------------------------


def test_pf4_a_plan_run_is_capped_by_its_own_ceiling() -> None:
    """The setting §17 names, and the code that reads it.

    Both halves, because the first half passed for the whole of S2-P0 to S2-P6
    while the second did not exist: `max_plan_cost_usd` was declared in
    `config.py` and consulted nowhere, so a plan run was billed against
    research's $15.
    """
    import inspect

    from agent.orchestrator import executor

    settings = Settings()
    assert settings.max_plan_cost_usd == Decimal("8.00")
    assert settings.max_plan_cost_usd < settings.max_run_cost_usd

    source = inspect.getsource(executor.RunExecutor._budget_cap)
    assert "max_plan_cost_usd" in source, "the plan ceiling is configured and never read"
    assert "RunStage.PLAN" in source, "the ceiling does not depend on the stage"
    assert "stage=run.stage" in inspect.getsource(executor.RunExecutor)


# ---------------------------------------------------------------------------
# PT1 / PT2 — traceability is enforced by the contract
# ---------------------------------------------------------------------------


def test_pt1_a_figure_cannot_exist_without_its_calculation() -> None:
    """PT1 is a type, not a review step.

    `Number.calc_evidence_id` is required and non-optional, so 100% of the
    `Number` objects in a validated plan resolve to a `PlanCalc` row by
    construction. The runtime half is `CalcIndex.number`, which returns `None`
    — omitting the figure — rather than stating one it cannot trace.
    """
    with pytest.raises(ValidationError):
        Number(value=Decimal("1"), unit="usd")  # type: ignore[call-arg]

    field = Number.model_fields["calc_evidence_id"]
    assert field.is_required()
    assert field.annotation is not None and "None" not in str(field.annotation)


def test_pt1_an_untraceable_figure_is_omitted_rather_than_stated() -> None:
    """The other half: what happens when there is no row to cite."""
    from agent.planning.plan_synthesis import CalcIndex

    dropped: list[str] = []
    empty = CalcIndex(())
    assert (
        empty.number(
            42,
            unit="usd",
            node_id="2.2.4",
            formula_id="allocation.split_v1",
            label="monthly envelope",
            drop=dropped.append,
        )
        is None
    )
    assert dropped and "allocation.split_v1" in dropped[0]


def test_pt2_a_claim_cannot_exist_without_a_citation() -> None:
    """PT2, at the layer that enforces it.

    The critique also refuses an uncited claim, but that branch is unreachable
    through a validated plan — which makes `Claim` the thing to hold. An
    optional citation list would make "uncited" the path of least resistance
    for the model and for every future caller, which is the comment on the
    field itself.
    """
    with pytest.raises(ValidationError):
        Claim(statement="Something true.", evidence_ids=[], confidence="high")


# ---------------------------------------------------------------------------
# PQ2 — the coverage gate
# ---------------------------------------------------------------------------

#: What §17 PQ2 names, and what `make coverage-plan` must therefore measure.
PQ2_PACKAGES = ("agent.calc", "agent.planning", "agent.nodes.plan", "agent.export")
PQ2_CALC_FLOOR = 85
PQ2_OTHER_FLOOR = 80


def test_pq2_the_coverage_gate_names_the_packages_and_the_floors() -> None:
    """A floor nobody runs is not a floor.

    Coverage cannot be asserted from inside the suite it measures, so what is
    checkable here is that the `make` target exists, measures the packages §17
    names, and fails below the number §17 gives. The measurement itself is
    `make coverage-calc` and `make coverage-plan`, both of which S2-P7's
    `verify-s2p7.sh` runs.
    """
    from pathlib import Path

    makefile = Path(__file__).resolve().parents[3] / "Makefile"
    text = makefile.read_text()
    assert "coverage-calc:" in text
    assert "coverage-plan:" in text

    calc = text.split("coverage-calc:")[1].split("\n\n")[0]
    assert "--cov=agent.calc" in calc
    assert f"--cov-fail-under={PQ2_CALC_FLOOR}" in calc

    plan = text.split("coverage-plan:")[1].split("\n\n")[0]
    for package in ("agent.nodes.plan", "agent.planning", "agent.export"):
        assert package in plan, f"PQ2 does not measure {package}"
    assert f"--cov-fail-under={PQ2_OTHER_FLOOR}" in plan
    # Each on its own: a blended figure lets a well-covered package carry a
    # bare one, which is not what §17 asks for.
    assert "for pkg in" in plan, "PQ2's three floors were measured as one"
