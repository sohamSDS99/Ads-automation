#!/usr/bin/env bash
# S3-P3's acceptance list, from the Stage 03 PRD §21:
#
#   A run halts on H1 for the named legal owner only; `admin` signing returns
#   `403`; an `approver` who is not the legal owner returns `403`; a stale
#   `set_hash` returns `409` and writes nothing; a reused re-auth token returns
#   `401`; an approved claim licenses its surface forms in the linter and an
#   expired one does not; a from-price mismatch against live offer data
#   produces a blocking finding.
#
# Steps 1-5 need nothing but the repo. Steps 6-7 need the stack, because every
# claim about 403/409/401 is a claim about the routes, the database and the
# authorization layer together, and nothing smaller than a real request proves
# it.
#
# Two steps are **mutation checks**: they break a protection on purpose and
# assert the suite goes red. A test that would still pass with the guard
# deleted is not evidence, and the only way to know is to delete it and look.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }

step "1. The phase's own unit suites"
if (cd "$API" && uv run pytest \
      tests/test_claim_signature_hash.py \
      tests/test_claims_index.py \
      tests/test_guideline_nodes_3_2.py \
      tests/test_registry.py -q); then
  ok "set hash, the claims index, the 3.2 nodes and the person-task spec rules"
else
  bad "a unit suite for this phase is red"
fi

step "2. The guideline DAG gained stage 3.2, and H1 is a person-task not a gate"
DAG_OUT=$(cd "$API" && uv run python -c "
from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry

ids = sorted(get_dag(RunStage.GUIDELINE).node_ids)
for wanted in ('3.2.1', '3.2.2', '3.2.3', '3.2.4'):
    assert wanted in ids, (wanted, ids)

reg = get_registry()
h1 = reg.spec('3.2.3')
assert h1.human_task_key == 'H1', h1.human_task_key
assert h1.gate is False and h1.gate_key is None, 'H1 must not be a gate'
assert h1.required_role is None, 'a person-task routes to an identity, never a role'
assert set(h1.depends_on) == {'3.2.2', '3.5.1'}, h1.depends_on

# A gate and a person-task in one spec is refused at import.
from pydantic import ValidationError
from agent.nodes.base import NodeSpec
from agent.db.models import ApprovalRequiredRole
from agent.llm.router import TaskClass
from pydantic import BaseModel
try:
    NodeSpec(id='9.9', name='x', stage='9', task_class=TaskClass.CLASSIFY,
             input_model=BaseModel, output_model=BaseModel, gate=True, gate_key='G9',
             required_role=ApprovalRequiredRole.APPROVER, human_task_key='H9')
except ValidationError:
    pass
else:
    raise AssertionError('a node was allowed to be both a gate and a person-task')
print(' '.join(ids))
" 2>&1)
if [ $? -eq 0 ]; then
  ok "stage 3.2 present; 3.2.3 is H1, hangs off 3.2.2 and 3.5.1, and cannot also be a gate"
else
  bad "the DAG or the person-task spec is wrong: $DAG_OUT"
fi

step "3. admin holds neither CLAIM_SIGN nor ATTEST_SUBMIT (law 23)"
if (cd "$API" && uv run python -c "
from agent.auth.rbac import Permission, ROLE_PERMISSIONS
from agent.db.models import UserRole
assert Permission.CLAIM_SIGN not in ROLE_PERMISSIONS[UserRole.ADMIN]
assert Permission.ATTEST_SUBMIT not in ROLE_PERMISSIONS[UserRole.ADMIN]
assert Permission.CLAIM_SIGN in ROLE_PERMISSIONS[UserRole.APPROVER]
"); then
  ok "admin cannot sign and cannot attest; approver can"
else
  bad "the non-delegable permissions are reachable by admin"
fi

step "4. An approved claim licenses; expired, voided and unsigned all block"
if (cd "$API" && uv run pytest tests/test_claims_index.py -q \
      -k "licenses or blocking or un_licenses"); then
  ok "the licence loop holds in all four states"
else
  bad "the licence loop is broken"
fi

step "5. A from-price mismatch against live offer data is blocking"
if (cd "$API" && uv run pytest tests/guardrails/test_offers.py -q -k "from_price" \
      && uv run pytest tests/test_guideline_nodes_3_2.py -q -k "OfferIntegrity"); then
  ok "the matcher blocks a stale floor and 3.2.4 reports it as a live violation"
else
  bad "offer integrity does not block a stale from-price"
fi

step "6. MUTATION — deleting the identity check must turn the 403s green-to-red"
GUARD='if matrix.legal_owner_id != me.user.id:'
TARGET="$API/src/agent/api/routes_claims.py"
if grep -q "$GUARD" "$TARGET"; then
  cp "$TARGET" /tmp/routes_claims.bak
  # Make the identity check unreachable: every approver becomes the legal owner.
  python3 - "$TARGET" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
p.write_text(s.replace("if matrix.legal_owner_id != me.user.id:", "if False:", 1))
PY
  if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
    if docker compose run --rm test pytest tests/integration/test_claim_signature.py -q \
         -k "not_the_named_legal_owner" >/dev/null 2>&1; then
      bad "the identity check was removed and the suite still passed"
    else
      ok "removing the identity check turns the suite red, so the test is load-bearing"
    fi
  else
    skip "stack not running — start it with 'make up' to run the mutation check"
  fi
  cp /tmp/routes_claims.bak "$TARGET"
else
  bad "the identity check has moved; this mutation no longer targets anything"
fi

step "7. The signature suite against a real stack"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest \
       tests/integration/test_claim_signature.py \
       tests/integration/test_human_task_h1.py -q; then
    ok "403 for admin, 403 for the wrong approver, 409 on a stale hash, 401 on a reused token, and H1 halts for the named owner"
  else
    bad "the signature or person-task suite is red against the stack"
  fi
else
  skip "stack not running — start it with 'make up'"
fi

step "8. Gates: route guards, guardrails purity, types"
if (cd "$API" && uv run python scripts/check_route_guards.py \
      && uv run python scripts/check_guardrails_purity.py \
      && uv run mypy); then
  ok "every new route declares a permission, guardrails stayed pure, types clean"
else
  bad "a repo gate is red"
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS3-P3 acceptance: PASS\033[0m\n'
  exit 0
fi
printf '\033[31mS3-P3 acceptance: %d step(s) FAILED\033[0m\n' "$failures"
exit 1
