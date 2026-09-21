#!/usr/bin/env bash
# S2-P4's exit criteria, from Stage 02 PRD §21:
#
#   "A run produces a full campaign -> ad group -> keyword tree; every name
#    passes `validator_regex`; brand terms appear only in the brand campaign;
#    no keyword appears twice."
#
# Plus the deliverables that sentence does not name: gate G4, which completes
# law 16's four, the naming validator and the collision check against the live
# account, and the structure floors 2.4.3 judges the built tree against.
#
# Checks 1-6 need nothing but the repo. Check 7 is the exit criterion itself,
# and it needs a real Postgres and Redis: "a run produces" is a claim about a
# run, and the only honest way to check it is to execute the whole DAG through
# all four gates and read what came out. It is reported as a FAILURE when the
# stack is down rather than skipped, because the criterion the phase is named
# for must not pass by omission.
#
# Nothing here is asserted by inspection: each check exits non-zero on its own.
#
# Check 7 talks to whatever stack `docker compose` resolves to. The compose
# file pins `name: ads-research-agent`, so from a worktree — where that stack
# belongs to a different branch — point it at your own:
#
#   COMPOSE_PROJECT_NAME=ads-s2p4 \
#   COMPOSE_FILE=docker-compose.yml:/path/to/override.yml \
#   ./scripts/verify-s2p4.sh
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

step "1. Stages 2.3 and 2.4 are in the plan DAG with PRD §11's edges"
if (cd "$API" && uv run python - <<'PY'
import sys

from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag

# §11's edge list, for the six nodes this phase added. Two transitive
# dependencies are declared beyond it and argued in each node's spec:
# 2.3.2 reads 2.2.2's cluster assignment, and 2.4.1 and 2.4.2 read the
# approved split and 2.2.2's bid-strategy verdict.
EXPECTED = {
    "2.3.1": {"2.2.4", "2.1.3"},
    "2.3.2": {"2.3.1", "2.3.3", "2.2.2"},
    "2.3.3": {"2.3.1"},
    "2.4.1": {"2.3.1", "2.2.4"},
    "2.4.2": {"2.3.1", "2.3.3", "2.4.1", "2.2.4", "2.2.2"},
    "2.4.3": {"2.4.2", "2.2.4", "2.2.2"},
}

dag = get_dag(RunStage.PLAN)
bad = []
for node_id, parents in EXPECTED.items():
    if node_id not in dag.node_ids:
        bad.append(f"{node_id} is not in the plan DAG")
        continue
    found = set(dag.depends_on(node_id))
    if found != parents:
        bad.append(f"{node_id} depends on {sorted(found)}, expected {sorted(parents)}")
if bad:
    print("\n".join(bad))
    sys.exit(1)
print(f"plan DAG holds {len(dag.node_ids)} nodes in {len(dag.waves())} waves")
PY
); then
  ok "2.3.1-2.3.3 and 2.4.1-2.4.3 are registered with their declared edges"
else
  bad "the plan DAG does not match PRD §11 for stages 2.3 and 2.4"
fi

step "2. Gate G4 completes law 16's four, and routes to an approver"
if (cd "$API" && uv run python - <<'PY'
import sys

from agent.db.models import ApprovalRequiredRole, RunStage
from agent.orchestrator.registry import discover

plan_gates = {
    spec.id: spec
    for spec in discover().specs()
    if spec.gate and spec.run_stage is RunStage.PLAN
}
keys = {node_id: spec.gate_key for node_id, spec in plan_gates.items()}
if keys != {"2.1.3": "G1", "2.1.4": "G2", "2.2.4": "G3", "2.3.1": "G4"}:
    print(f"plan gates are {keys}, expected exactly G1-G4")
    sys.exit(1)
if any(spec.required_role is not ApprovalRequiredRole.APPROVER for spec in plan_gates.values()):
    print("a plan gate does not route to an approver")
    sys.exit(1)
print("four plan gates: G1 targets, G2 lead definition, G3 budget, G4 channel slate")
PY
); then
  ok "G4 is on 2.3.1, and the plan carries four gates and no more"
else
  bad "the plan gate set is not exactly G1-G4 routed to approvers"
fi

step "3. Every figure stages 2.3 and 2.4 publish comes from a registered formula"
if (cd "$API" && uv run python scripts/check_calc_isolation.py); then
  ok "no arithmetic in nodes/plan/, every numeric output cites calc_evidence_ids"
else
  bad "law 14 is broken somewhere in nodes/plan/"
fi

step "4. The two formulas this phase added are registered and hand-checked"
if (cd "$API" && uv run python - <<'PY'
import sys

from agent.calc.registry import FORMULAS

missing = {"allocation.share_v1", "structure.overlap_v1"} - set(FORMULAS)
if missing:
    print(f"not registered: {sorted(missing)}")
    sys.exit(1)
print(f"{len(FORMULAS)} formulas registered, including the two S2-P4 added")
PY
) && (cd "$API" && uv run pytest tests/test_calc_allocation.py tests/test_calc_structure.py \
        tests/test_calc_registry.py -q); then
  ok "allocation.share_v1 and structure.overlap_v1 are registered and green"
else
  bad "a new formula is missing or its hand-checked suite is red"
fi

step "5. The naming validator enforces the convention it was compiled from"
if (cd "$API" && uv run python - <<'PY'
import re
import sys

from agent.planning import naming

PATTERNS = {"campaign": "{market} | {channel} | {brand_split}"}
TOKENS = {"market": ["US", "DE"], "channel": ["Search"], "brand_split": ["Brand", "NonBrand"]}
validator = re.compile(naming.compile_validator(PATTERNS, TOKENS))

must_pass = ["US | Search | Brand", "DE | Search | NonBrand"]
must_fail = ["FR | Search | Brand", "US | Shopping | Brand", "US | Search", "US"]
bad = [name for name in must_pass if not validator.match(name)]
bad += [name for name in must_fail if validator.match(name)]
if bad:
    print(f"the compiled regex is wrong about: {bad}")
    sys.exit(1)

try:
    naming.compile_validator({"campaign": "{market} | {mystery}"}, TOKENS)
except naming.NamingError:
    pass
else:
    print("an unknown token compiled instead of being refused")
    sys.exit(1)
print("compiled validator accepts the convention and refuses everything else")
PY
); then
  ok "the regex is compiled from the patterns, and an unknown token is refused"
else
  bad "the naming validator does not enforce its own convention"
fi

step "6. The unit suites for what this phase added"
if (cd "$API" && uv run pytest tests/test_plan_nodes_2_3.py tests/test_plan_nodes_2_4.py \
      tests/test_planning_naming.py tests/test_planning_structure.py \
      tests/test_registry.py tests/test_dag.py tests/test_calc_isolation.py -q); then
  ok "the nodes, the naming layer, the frames, the registry and the guards are green"
else
  bad "a unit suite for this phase is red"
fi

step "7. The exit criterion itself, against a real database"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_plan_stage_2_3_2_4.py \
       tests/integration/test_plan_stage_2_2.py -q; then
    ok "a real run produces a campaign -> ad group -> keyword tree, every name passes \
validator_regex, brand terms are only in the brand campaign, and no keyword appears twice"
  else
    bad "the stage 2.3/2.4 integration suite is red"
  fi
else
  printf '  \033[31mFAIL\033[0m postgres is not running — run `make up` first.\n'
  printf '        This is the criterion the phase is named for. "A run produces" is a\n'
  printf '        claim about a run, and the only honest way to check it is to execute\n'
  printf '        the whole DAG through four gates and read what came out.\n'
  failures=$((failures + 1))
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS2-P4: every exit criterion passed.\033[0m\n'
else
  printf '\033[31mS2-P4: %s check(s) did not pass.\033[0m\n' "$failures"
fi
exit "$failures"
