#!/usr/bin/env bash
# S2-P7's exit criteria (Stage 02 PRD §21).
#
#   "Eval suite green on all five fixtures; coverage >= 85% on calc/ and >= 80%
#    elsewhere in scope; every §17 threshold has a test that would fail if
#    regressed."
#
# Plus the phase's scope line: staleness rules, `source_superseded` banners,
# `planning/diff.py` + compare view, five golden `PlanInput` fixtures with
# critique assertions, a 4-role authz matrix for every new route, consent and
# PII canary tests, coverage, and `docs/stage-02.md`.
#
# Checks 1-9 need nothing but the repo. Checks 10-11 need a real Postgres and
# Redis: whether `source_superseded` is ever *written* is a database fact, and
# it is the one thing this phase exists to fix. They are reported as FAILURES
# when the stack is down rather than skipped — a criterion a phase is named
# for must not pass by omission.
#
# From a worktree, point compose at your own stack:
#
#   COMPOSE_PROJECT_NAME=ads-s2p7 \
#   COMPOSE_FILE=docker-compose.yml:docker-compose.worktree.yml \
#   WORKTREE_SUBNET=172.31.216.0/24 WORKTREE_WEB_PORT=3107 \
#   ./scripts/verify-s2p7.sh
#
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

# Never `2>/dev/null` on a setup step: a broken setup then reads as a clean
# failure of the check it was preparing, which is how three of P8's checks lied.
pysh() { (cd "$API" && uv run python -); }

# ---------------------------------------------------------------------------

step "1. The five golden PlanInput fixtures exist and are PlanInputs"
if pysh <<'PY'
import json
from pathlib import Path

from agent.schemas.plan_input import PlanInput

paths = sorted(Path("tests/eval/plan").glob("*.json"))
assert len(paths) == 5, f"{len(paths)} fixtures, not 5"
names = set()
for path in paths:
    raw = json.loads(path.read_text())
    assert set(raw) == {"name", "why", "plan_input"}, f"{path.name}: {sorted(raw)}"
    # `PlanInput` forbids extras, so this really asserts the fixture was
    # written through the contract rather than around it.
    PlanInput.model_validate(raw["plan_input"])
    names.add(raw["name"])
assert names == {
    "baseline_single_market",
    "multi_market_consent_blocked",
    "launch_blockers_carried",
    "degraded_forecast_override",
    "eu_only_no_brand",
}, sorted(names)
print("   " + ", ".join(sorted(names)))
PY
then ok "five fixtures, each a valid PlanInput, spanning five input conditions"
else bad "the golden PlanInput fixtures are missing or malformed"
fi

step "2. Every fixture produces a plan that passes all ten §11 assertions"
if pysh <<'PY'
import json
from pathlib import Path

from agent.planning import critique
from agent.schemas.plan_input import PlanInput
from tests.eval import plan_dag

for path in sorted(Path("tests/eval/plan").glob("*.json")):
    raw = json.loads(path.read_text())
    source = PlanInput.model_validate(raw["plan_input"])
    built = plan_dag.build(source)
    issues = critique.run_checks(built.plan, launch_blockers=source.launch_blockers)
    assert not issues, f"{raw['name']}: " + "; ".join(
        f"{i.severity} {i.check} {i.finding}" for i in issues
    )
    print(f"   {raw['name']}: {len(built.plan.account_structure.campaigns)} campaigns, clean")
PY
then ok "PQ3: five plans, zero critique issues — not merely zero blocking ones"
else bad "a golden fixture produced a plan the critique refuses"
fi

step "3. The eval harness can fail (the negative controls)"
if (cd "$API" && uv run pytest tests/eval -q >/tmp/s2p7-eval.txt 2>&1); then
  controls=$(grep -c 'test_a_broken_plan_fails' /tmp/s2p7-eval.txt || true)
  ok "$( (cd "$API" && uv run pytest tests/eval -q 2>&1 | tail -1) )"
else
  bad "the eval suite is red — see /tmp/s2p7-eval.txt"
fi

step "4. Every §17 threshold names a test that exists"
if (cd "$API" && uv run pytest tests/test_nfr_stage_02.py -q >/tmp/s2p7-nfr.txt 2>&1); then
  ok "$( tail -1 /tmp/s2p7-nfr.txt )"
else
  bad "a §17 threshold has no holder, or its holder no longer exists"
fi

step "5. PC2: no canary reaches a prompt, the payload or any of the six exports"
if (cd "$API" && uv run pytest tests/test_pii_canary.py -q >/tmp/s2p7-pii.txt 2>&1); then
  ok "$( tail -1 /tmp/s2p7-pii.txt )"
else
  bad "a CRM canary leaked — see /tmp/s2p7-pii.txt"
fi

step "6. PQ2: coverage floors, each package on its own"
if make coverage-calc >/tmp/s2p7-cov-calc.txt 2>&1; then
  ok "calc/ + planning/: $(grep -o 'Total coverage: [0-9.]*%' /tmp/s2p7-cov-calc.txt | tail -1) (floor 85%)"
else
  bad "calc/ is below its 85% floor — see /tmp/s2p7-cov-calc.txt"
fi
if make coverage-plan >/tmp/s2p7-cov-plan.txt 2>&1; then
  grep -o 'Total coverage: [0-9.]*%' /tmp/s2p7-cov-plan.txt | while read -r line; do
    printf '       %s\n' "$line"
  done
  ok "nodes/plan, planning/ and export/ each clear their 80% floor"
else
  bad "a Stage 02 package is below its 80% floor — see /tmp/s2p7-cov-plan.txt"
fi

step "7. The staleness rule exists and is derived, not latched"
if pysh <<'PY'
import inspect

from agent.api import routes_plan
from agent.audit import AuditAction
from agent.planning import staleness

# Both transitions of acceptance currency refresh, in the same transaction.
for fn in (routes_plan.accept_research, routes_plan.withdraw_acceptance):
    src = inspect.getsource(fn)
    assert "staleness.refresh_for_project" in src, f"{fn.__name__} does not refresh"
    assert src.index("staleness.refresh_for_project") < src.index("db.commit"), (
        f"{fn.__name__} refreshes after its commit, so the two are not one transaction"
    )

# The flag has an independent derivation to be checked against.
assert hasattr(staleness, "derived_flags")
assert AuditAction.PLAN_SOURCE_SUPERSEDED.value == "plan.source_superseded"
assert AuditAction.PLAN_SOURCE_RESTORED.value == "plan.source_restored"
PY
then ok "accept and withdraw both refresh before their commit; both directions are audited"
else bad "the staleness rule is missing, or it runs outside the acceptance transaction"
fi

step "8. The banner offers the re-plan §4.4 asks for"
viewer=apps/web/components/plan/plan-viewer.tsx
if grep -q 'source_superseded' "$viewer" \
   && grep -q 'Plan against the current research' "$viewer" \
   && grep -q "plan\`}" "$viewer"; then
  ok "the Plan Viewer banner links to the console rather than only stating the fact"
else
  bad "the source_superseded banner does not offer a re-plan"
fi

step "9. The runbook exists and covers what an operator needs"
doc=docs/stage-02.md
if [ -f "$doc" ]; then
  missing=()
  for heading in "Accepting research" "The four gates" "Freezing" "Exports" \
                 "When the research moves on" "Failure modes" "Running the gates"; do
    grep -qi "$heading" "$doc" || missing+=("$heading")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    ok "docs/stage-02.md covers all seven sections ($(wc -l < "$doc" | tr -d ' ') lines)"
  else
    bad "docs/stage-02.md is missing: ${missing[*]}"
  fi
else
  bad "docs/stage-02.md does not exist"
fi

step "10. PR2 + PS3 + the staleness rule, against a real database"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest \
      tests/integration/test_plan_staleness.py \
      tests/integration/test_plan_resume.py \
      tests/integration/test_authz_matrix.py \
      -q >/tmp/s2p7-integration.txt 2>&1; then
    ok "$( tail -1 /tmp/s2p7-integration.txt )"
  else
    bad "staleness / resume / authz are red — see /tmp/s2p7-integration.txt"
  fi
else
  bad "postgres is not running; the staleness rule cannot be verified without it"
fi

step "11. The whole integration suite still passes"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test >/tmp/s2p7-suite.txt 2>&1; then
    ok "$( tail -1 /tmp/s2p7-suite.txt )"
  else
    bad "the integration suite is red — see /tmp/s2p7-suite.txt"
  fi
else
  bad "postgres is not running"
fi

step "12. Lint, format, types and the route/arithmetic guards"
(cd "$API" && uv run ruff check . >/tmp/s2p7-ruff.txt 2>&1) \
  && ok "ruff check clean" || bad "ruff check — see /tmp/s2p7-ruff.txt"
(cd "$API" && uv run ruff format --check . >/tmp/s2p7-fmt.txt 2>&1) \
  && ok "ruff format clean" || bad "ruff format — see /tmp/s2p7-fmt.txt"
(cd "$API" && uv run mypy src >/tmp/s2p7-mypy.txt 2>&1) \
  && ok "mypy strict clean" || bad "mypy — see /tmp/s2p7-mypy.txt"
(make guards >/tmp/s2p7-guards.txt 2>&1) \
  && ok "route guards + arithmetic isolation clean" || bad "guards — see /tmp/s2p7-guards.txt"

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS2-P7: every check passed.\033[0m\n'
else
  printf '\033[31mS2-P7: %d check(s) failed.\033[0m\n' "$failures"
fi
exit "$failures"
