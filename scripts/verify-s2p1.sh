#!/usr/bin/env bash
# S2-P1's exit criteria, from PRD §21:
#
#   "pytest calc/ green with hand-verified expected values; a missing `source`
#    on any constant fails startup with the key named; `PlanCalc` dedupes on
#    (plan_run_id, formula_id, inputs_hash)"
#
# Everything here runs without the stack except the last check, which needs a
# real Postgres to prove a unique constraint — it is skipped, loudly, when the
# stack is down. Nothing is asserted by inspection: each check exits non-zero on
# its own.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

step "1. calc/ and planning/ unit suites (hand-checked expected values)"
if (cd "$API" && uv run pytest tests/test_calc_*.py tests/test_planning_constants.py -q); then
  ok "every formula matches its hand-checked fixture"
else
  bad "the calc unit suite is red"
fi

step "2. PQ2 coverage floor: >= 85% on agent.calc and agent.planning"
if (cd "$API" && uv run pytest tests -q --ignore=tests/integration \
      --cov=agent.calc --cov=agent.planning \
      --cov-report=term-missing:skip-covered --cov-fail-under=85 >/dev/null); then
  ok "coverage is at or above 85%"
else
  bad "coverage on calc/ or planning/ is below 85%"
fi

step "3. A constant with no source fails startup, naming the key"
if (cd "$API" && uv run python - <<'PY'
import sys, tempfile, pathlib
sys.path.insert(0, "src")
from agent.planning.constants import ConstantsError, load_planning_constants

broken = """
version: "test"
learning:
  tcpa_min_conv_30d: {value: 30, reviewed_at: 2026-09-21}
"""
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
    handle.write(broken)
    path = pathlib.Path(handle.name)
try:
    load_planning_constants(path)
except ConstantsError as exc:
    message = str(exc)
    assert "learning.tcpa_min_conv_30d.source" in message, message
    print("   startup refused and named the key:")
    print("  ", message.splitlines()[1].strip())
    sys.exit(0)
sys.exit("a constant with no source was accepted")
PY
); then
  ok "the failure names learning.tcpa_min_conv_30d.source"
else
  bad "a sourceless constant did not fail startup with its key named"
fi

step "4. The arithmetic-isolation guard"
if (cd "$API" && uv run python scripts/check_calc_isolation.py); then
  ok "calc/ is pure and nodes/plan/ holds no arithmetic"
else
  bad "the isolation guard found a violation"
fi

step "5. PlanCalc dedupes on (plan_run_id, formula_id, inputs_hash)"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_plan_calc.py -q; then
    ok "one row and one evidence id for a repeated calculation; the constraint rejects a duplicate"
  else
    bad "the PlanCalc dedupe test is red"
  fi
else
  printf '  \033[33mSKIP\033[0m postgres is not running — run `make up` first.\n'
  printf '        This is the one criterion that needs a real database: the dedupe is a\n'
  printf '        unique constraint, and SQLite would not enforce it the same way.\n'
  failures=$((failures + 1))
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS2-P1: every exit criterion passed.\033[0m\n'
else
  printf '\033[31mS2-P1: %s check(s) did not pass.\033[0m\n' "$failures"
fi
exit "$failures"
