#!/usr/bin/env bash
# S2-P5a's exit criteria.
#
# S2-P5 in Stage 02 PRD §21 is one phase covering nodes 2.5.1-2.5.3, node
# 2.6.1 synthesis, 2.6.2 critique, six export formats and the freeze. Most of
# that cannot be built yet: 2.5.3 reads 2.4.2, 2.2.4 and 2.3.1, and synthesis
# reads everything. §11's edge list makes 2.5.1 and 2.5.2 the exception —
# 2.5.1<-{2.1.1} and 2.5.2<-{2.1.1,2.1.4} — so they are buildable on top of
# S2-P2 alone and are this phase. Its criteria, derived from §11 and §13:
#
#   Both nodes run inside a real plan run and produce validated output; every
#   figure they carry resolves to a PlanCalc row; the consent scope comes from
#   gate 1.5.3 and not from the model; an EU market with no named consent
#   signal raises a blocking dependency; and no CRM record reaches a prompt.
#
# Checks 1-5 need nothing but the repo. Check 6 is the exit criterion itself
# and needs a real Postgres and Redis: a gate halting, a branch resuming and a
# citation checked against `derived` rows in the run's own session are all
# database facts. It is reported as a FAILURE when the stack is down rather
# than skipped, because the criterion the phase is named for should not pass
# by omission.
#
# Check 6 talks to whatever stack `docker compose` resolves to. The compose
# file pins `name: ads-research-agent`, so from a worktree point it at your own:
#
#   COMPOSE_PROJECT_NAME=ads-s2p5a \
#   COMPOSE_FILE=docker-compose.yml:/path/to/override.yml \
#   ./scripts/verify-s2p5a.sh
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

step "1. The 2.5 branch is in the plan DAG with PRD §11's edges"
if (cd "$API" && uv run python - <<'PY'
from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry

plan = get_dag(RunStage.PLAN)
# §11: 2.5.1<-{2.1.1}  2.5.2<-{2.1.1,2.1.4}
assert plan.depends_on("2.5.1") == ("2.1.1",), plan.depends_on("2.5.1")
assert plan.depends_on("2.5.2") == ("2.1.1", "2.1.4"), plan.depends_on("2.5.2")

# 2.5.1 is off both gates: it must be able to finish while sales deliberates.
assert "2.5.1" not in plan.descendants("2.1.3"), "2.5.1 waits on G1"
assert "2.5.1" not in plan.descendants("2.1.4"), "2.5.1 waits on G2"
# 2.5.2 is under G2, which is what §8.4's prose denies and §11's edges say.
assert "2.5.2" in plan.descendants("2.1.4"), "2.5.2 no longer reads the lead definition"

registry = get_registry()
# Neither node is a gate: §11 puts G1..G4 on 2.1.3, 2.1.4, 2.2.4 and 2.3.1.
assert not any(registry.spec(node).gate for node in ("2.5.1", "2.5.2"))
calc = {node: registry.spec(node).calc for node in ("2.5.1", "2.5.2")}
assert calc == {
    "2.5.1": ("measurement.reconciliation_v1",),
    "2.5.2": ("measurement.upload_window_v1",),
}, calc
print(f"   waves: {plan.waves()}")
PY
); then
  ok "2.5.1 hangs off 2.1.1 alone, 2.5.2 off 2.1.1 and 2.1.4, and neither is a gate"
else
  bad "the 2.5 edges do not match PRD §11"
fi

step "2. The two formulas exist, are declared beyond the PRD, and are constrained"
if (cd "$API" && uv run python - <<'PY'
from agent.calc.registry import CALC_KINDS, FORMULAS
from agent.planning.constants import load_planning_constants
from tests.test_calc_registry import BEYOND_THE_PRD, PRD_FORMULAS

# §9.2's nine are untouched, and the two additions are named with the §11
# output field that forced each one, rather than folded in silently.
assert PRD_FORMULAS <= set(FORMULAS)
assert set(FORMULAS) == PRD_FORMULAS | set(BEYOND_THE_PRD), sorted(FORMULAS)
for formula_id in BEYOND_THE_PRD:
    assert FORMULAS[formula_id].kind == "calc_measurement"
assert "calc_measurement" in CALC_KINDS

# Law 15: every measurement threshold carries a source and a review date.
constants = load_planning_constants()
for key in type(constants.measurement).model_fields:
    constant = constants.get(f"measurement.{key}")
    assert constant.source, f"measurement.{key} has no source"
    assert constant.reviewed_at, f"measurement.{key} has no reviewed_at"
print("   added:", ", ".join(sorted(BEYOND_THE_PRD)))
PY
); then
  ok "two formulas, one new evidence kind, and six sourced constants"
else
  bad "the measurement formulas or their constants are wrong"
fi

step "3. A tolerance is the widest named term, never a sum of allowances"
if (cd "$API" && uv run python - <<'PY'
import pandas as pd

from agent.calc import measurement
from agent.planning.constants import load_planning_constants

constants = load_planning_constants()
# 8% measured plus a 20% modelled allowance is 20%, not 28%. If that ever
# becomes addition, an EU account's tolerance compounds on every flag.
row = measurement.reconciliation_v1(
    pd.DataFrame(
        [{
            "metric": "conversions",
            "recorded_conversions": 200,
            "unreconciled_conversions": 16,
            "modelled": True,
        }]
    ),
    constants=constants,
).result["by_metric"][0]
assert row["observed_gap_pct"] == 8.0, row
assert row["tolerance_pct"] == 20.0, row
assert row["binding_driver"] == "modelled_conversions", row
print(f"   8% measured + 20% allowance -> {row['tolerance_pct']}%, bound by {row['binding_driver']}")
PY
); then
  ok "terms are selected, not summed, and the binding one is named"
else
  bad "the tolerance model compounds"
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
if (cd "$API" && uv run pytest tests/test_plan_nodes_2_5.py tests/test_calc_measurement.py \
      tests/test_planning_tracking.py tests/test_planning_constants.py \
      tests/test_calc_registry.py tests/test_registry.py tests/test_calc_isolation.py -q); then
  ok "the nodes, the formulas, the frame assembly and the guards are green"
else
  bad "a unit suite for this phase is red"
fi

step "7. The exit criterion itself, against a real database"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_plan_stage_2_5.py \
       tests/integration/test_plan_stage_2_1.py tests/integration/test_plan_calc.py -q; then
    ok "2.5.1 clears the halt, 2.5.2 runs once G2 is decided, every figure resolves \
to a PlanCalc row, consent comes from gate 1.5.3, and no CRM record reaches a 2.5 prompt"
  else
    bad "the Stage 2.5 integration suite is red"
  fi
else
  printf '  \033[31mFAIL\033[0m postgres is not running — run `make up` first.\n'
  printf '        This is the criterion the phase is named for. A gate halting, a\n'
  printf '        branch resuming and a citation checked against `derived` rows in\n'
  printf '        the run session are database facts, so there is no version of\n'
  printf '        this check that runs without one.\n'
  failures=$((failures + 1))
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS2-P5a: every exit criterion passed.\033[0m\n'
else
  printf '\033[31mS2-P5a: %s check(s) did not pass.\033[0m\n' "$failures"
fi
exit "$failures"
