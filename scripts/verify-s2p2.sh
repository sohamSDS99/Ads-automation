#!/usr/bin/env bash
# S2-P2's exit criteria, from Stage 02 PRD §21:
#
#   "A partial run of 2.1 produces validated outputs, halts on G1 and G2, an
#    `approver` resumes each, an `operator` gets 403, every number resolves to
#    a `PlanCalc` row."
#
# Plus the four deliverables that sentence does not mention by name: the
# registry keyed by (stage, node_id), the `NodeSpec` extensions, the read-only
# connector assertion, and `calc_evidence_ids` validation.
#
# Checks 1-6 need nothing but the repo. Check 7 is the exit criterion itself
# and needs a real Postgres and Redis — a gate halting, an approver resuming
# it and a unique constraint deduping a calculation are all database facts,
# and SQLite would not tell the truth about any of them. It is reported as a
# FAILURE when the stack is down rather than skipped quietly, because the one
# criterion the phase is named for should not pass by omission.
#
# Nothing here is asserted by inspection: each check exits non-zero on its own.
#
# Check 7 talks to whatever stack `docker compose` resolves to. The compose
# file pins `name: ads-research-agent`, so from a worktree — where that stack
# belongs to a different branch — point it at your own:
#
#   COMPOSE_PROJECT_NAME=ads-s2p2 \
#   COMPOSE_FILE=docker-compose.yml:/path/to/override.yml \
#   ./scripts/verify-s2p2.sh
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

step "1. The plan DAG is stage 2.1, with PRD §11's edges"
if (cd "$API" && uv run python - <<'PY'
import sys

from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry

plan = get_dag(RunStage.PLAN)
research = get_dag(RunStage.RESEARCH)

# §11's edge list for stage 2.1:
#   2.1.1<-{}  2.1.2<-{2.1.1}  2.1.3<-{2.1.1,2.1.2}  2.1.4<-{2.1.1}
expected = {
    "2.1.1": (),
    "2.1.2": ("2.1.1",),
    "2.1.3": ("2.1.1", "2.1.2"),
    "2.1.4": ("2.1.1",),
}
# The 2.1 sub-graph only. Later phases add nodes to the same DAG, and a
# whole-graph assertion here would fail on every one of them for a reason
# that has nothing to do with S2-P2.
actual = {node: plan.depends_on(node) for node in plan.node_ids if node.startswith("2.1.")}
assert actual == expected, f"the 2.1 sub-graph is {actual}"
assert set(plan.node_ids) & set(research.node_ids) == set(), "the two DAGs overlap"

# §5.3: G1 and G2 are parallel. 2.1.4 must not sit downstream of 2.1.3.
assert "2.1.4" not in plan.descendants("2.1.3"), "G2 waits on G1"
assert "2.1.3" not in plan.descendants("2.1.4"), "G1 waits on G2"

# The placeholders S2-P0 shipped are gone.
assert "2.0.1" not in get_registry(), "the S2-P0 placeholder nodes still register"
print(f"   waves: {plan.waves()}")
sys.exit(0)
PY
); then
  ok "2.1.1 -> {2.1.2, 2.1.4} -> 2.1.3, and G1/G2 do not block each other"
else
  bad "the plan DAG does not match PRD §11"
fi

step "2. NodeSpec carries \`calc\` and \`gate_key\`, and the keys are G1 and G2"
if (cd "$API" && uv run python - <<'PY'
from pydantic import ValidationError

from agent.db.models import RunStage
from agent.orchestrator.registry import get_registry

registry = get_registry()
keyed = {spec.id: spec.gate_key for spec in registry.specs() if spec.gate_key}
# G1 and G2 are stage 2.1's; G3 and G4 arrive with S2-P3 and S2-P4, so this
# asserts the two this phase owns rather than the whole eventual set.
assert {key: value for key, value in keyed.items() if value in {"G1", "G2"}} == {
    "2.1.3": "G1",
    "2.1.4": "G2",
}, keyed

calc = {
    spec.id: spec.calc
    for spec in registry.for_stage(RunStage.PLAN).specs()
    if spec.id.startswith("2.1.")
}
assert calc == {
    "2.1.1": ("economics.max_cpa_v1",),
    "2.1.2": ("economics.max_cpa_v1", "economics.payback_v1"),
    "2.1.3": ("economics.max_cpa_v1",),
    "2.1.4": ("economics.max_cpa_v1",),
}, calc

# A typo in the allow-list fails at import, not mid-run.
from agent.llm.router import TaskClass  # noqa: E402
from agent.nodes.base import NodeSpec  # noqa: E402
from pydantic import BaseModel  # noqa: E402

try:
    NodeSpec(
        id="2.9",
        name="n",
        stage="2",
        run_stage=RunStage.PLAN,
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=BaseModel,
        calc=("economics.max_cpa_v9",),
    )
except ValidationError as exc:
    assert "unregistered formula" in str(exc), str(exc)
else:
    raise SystemExit("an unregistered formula id was accepted")
print("   allow-lists:", calc)
PY
); then
  ok "the extensions exist, the keys are right, and a bad formula id is refused at import"
else
  bad "NodeSpec.calc / gate_key is wrong"
fi

step "3. A plan run may not reach a connector that can write (law 12, PS1)"
if (cd "$API" && uv run python - <<'PY'
from agent.connectors import MutationForbidden, assert_read_only, is_read_only

# The two a Stage 2.1 node actually pulls through.
for name in ("google_ads", "csv_ingest"):
    assert is_read_only(name), f"{name} is not marked ReadOnlyConnector"

# And one that is not marked, to prove the assertion is not vacuous.
try:
    assert_read_only("dataforseo", why="node 2.1.1")
except MutationForbidden as exc:
    assert "dataforseo" in str(exc)
    print("   refused:", str(exc).split(" and ")[0])
else:
    raise SystemExit("an unmarked connector was allowed on a plan run")
PY
); then
  ok "google_ads and csv_ingest are read-only; an unmarked connector raises"
else
  bad "the read-only assertion does not hold"
fi

step "4. Arithmetic isolation, calc citations, and no direct formula imports"
if (cd "$API" && uv run python scripts/check_calc_isolation.py); then
  ok "nodes/plan/ computes nothing and cites everything"
else
  bad "the isolation guard found a violation"
fi

step "5. Route guards, types and lint"
if (cd "$API" && uv run python scripts/check_route_guards.py && uv run mypy src/agent >/dev/null \
    && uv run ruff check src tests >/dev/null && uv run ruff format --check src tests >/dev/null); then
  ok "every route is guarded; mypy and ruff are clean"
else
  bad "a guard, mypy or ruff is unhappy"
fi

step "6. The unit suites for what this phase added"
if (cd "$API" && uv run pytest tests/test_plan_nodes_2_1.py tests/test_planning_crm.py \
      tests/test_registry.py tests/test_calc_isolation.py tests/test_node_contract.py -q); then
  ok "the nodes, the CRM assembly, the registry and the guard are green"
else
  bad "a unit suite for this phase is red"
fi

step "7. The exit criterion itself, against a real database"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_plan_stage_2_1.py \
       tests/integration/test_plan_handshake.py -q; then
    ok "2.1 runs, halts on G1 and G2, an approver resumes each, an operator gets 403, \
and every number resolves to a PlanCalc row"
  else
    bad "the Stage 2.1 integration suite is red"
  fi
else
  printf '  \033[31mFAIL\033[0m postgres is not running — run `make up` first.\n'
  printf '        This is the criterion the phase is named for. A halted gate, an\n'
  printf '        approver resuming it and the PlanCalc dedupe are all database\n'
  printf '        facts, so there is no version of this check that runs without one.\n'
  failures=$((failures + 1))
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS2-P2: every exit criterion passed.\033[0m\n'
else
  printf '\033[31mS2-P2: %s check(s) did not pass.\033[0m\n' "$failures"
fi
exit "$failures"
