#!/usr/bin/env bash
# S3-P2's acceptance list, from the Stage 03 PRD §21:
#
#   A partial run produces a voice profile quoting real best-performing copy, a
#   compiling lexicon, and halts on G5 and G6; an `approver` resumes each, an
#   `operator` gets `403`; the brand-book canary string appears in zero prompts
#   and zero payload fields; every lexicon entry compiles to a matcher.
#
# Steps 1-6 need nothing but the repo. Step 7 needs the stack, because "halts on
# G5 and G6, an approver resumes each and an operator gets 403" is a claim about
# the executor, the approvals table and the authorization layer together, and
# nothing smaller than a real run can prove it.
#
# Three of these steps are **mutation checks**: they break the protection on
# purpose and assert the test goes red. A canary test that would pass with the
# redaction deleted is not evidence of anything, and the only way to know is to
# delete it and look.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; }

step "1. The phase's own suites"
if (cd "$API" && uv run pytest \
      tests/test_redaction.py \
      tests/test_brand_book.py \
      tests/test_conditional_gate.py \
      tests/test_guideline_nodes_3_1.py \
      tests/test_guideline_nodes_3_5.py \
      tests/test_signoff_persistence.py -q); then
  ok "redaction, brand_book, the conditional gate, both node groups and G6's write"
else
  bad "a unit suite for this phase is red"
fi

step "2. The guideline DAG is the four real nodes, and S3-P0's placeholder is gone"
DAG_OUT=$(cd "$API" && uv run python -c "
from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry
ids = sorted(get_dag(RunStage.GUIDELINE).node_ids)
assert ids == ['3.1.1', '3.1.2', '3.1.3', '3.5.1'], ids
gates = {s.id: s.gate_key for s in get_registry().specs() if s.gate and s.run_stage is RunStage.GUIDELINE}
assert gates == {'3.1.3': 'G5', '3.5.1': 'G6'}, gates
root = get_registry().spec('3.5.1')
assert root.depends_on == (), root.depends_on
assert root.gate_conditional is True
print(' '.join(ids))
" 2>&1)
if [ $? -eq 0 ]; then
  ok "nodes $DAG_OUT, gates G5+G6, and 3.5.1 is a conditional root"
else
  bad "the guideline DAG is not what §11 describes"
  printf '%s\n' "$DAG_OUT"
fi

step "3. Every lexicon entry compiles to a matcher"
LEX_OUT=$(cd "$API" && uv run python -c "
from agent.nodes.content.stage_3_1 import LexiconEntry, lexicon_rules
entries = [
    LexiconEntry(term='cheap', surface_forms=['cheap', 'cheapest'], locale='en'),
    LexiconEntry(term='Safety Data Sheet', surface_forms=['SDS'], locale='en'),
    LexiconEntry(term='guaranteed', surface_forms=['guarantee', 'guaranteed'], locale='en'),
]
for entry in entries:
    for mode in ('forbid', 'require'):
        matcher = lexicon_rules.matcher_for(entry, mode=mode)
        assert matcher.kind == 'term_set', matcher
print(len(entries) * 2)
" 2>&1)
if [ $? -eq 0 ]; then
  ok "$LEX_OUT matchers built and prepared through the guardrails registry"
else
  bad "a lexicon entry did not compile"
  printf '%s\n' "$LEX_OUT"
fi

step "4. ...and an entry that cannot compile is refused rather than published"
# Captured, not piped: this is SUPPOSED to raise, and under `set -o pipefail` a
# grep on its output would report the deliberate failure as a pipeline failure.
UNCOMPILABLE=$(cd "$API" && uv run python -c "
from agent.nodes.content.stage_3_1 import LexiconEntry, lexicon_rules
lexicon_rules.matcher_for(LexiconEntry(term='   ', surface_forms=['', ' ']), mode='forbid')
" 2>&1)
UNCOMPILABLE_CODE=$?
if [ "$UNCOMPILABLE_CODE" -ne 0 ] && printf '%s' "$UNCOMPILABLE" | grep -q 'no usable surface form'; then
  ok "an empty entry exits $UNCOMPILABLE_CODE and says why"
else
  bad "an unenforceable lexicon entry compiled anyway — §11's invariant is not held"
  printf '    exit %s\n%s\n' "$UNCOMPILABLE_CODE" "$UNCOMPILABLE"
fi

step "5. Redaction holds, and the test that says so would fail without it"
if (cd "$API" && uv run pytest tests/test_redaction.py -q); then
  ok "emails, telephone numbers and postal addresses are stripped; dates and prices are not"
else
  bad "the redaction suite is red"
fi
# The mutation check. With `redact_pii` reduced to the identity function, the
# node-level law-30 test must go red. If it stays green it was never testing
# redaction.
MUTANT=$(mktemp -d)
cp -R "$API/src" "$MUTANT/src"
cat > "$MUTANT/src/agent/evidence/redact.py" <<'PY'
from __future__ import annotations
from typing import Any
def redact_pii(text: str) -> str:
    return text
def redact_payload(value: Any) -> Any:
    return value
PY
# `-o pythonpath=` and NOT the PYTHONPATH environment variable. This suite sets
# `pythonpath = ["src"]` in pyproject, and pytest *prepends* that to sys.path —
# so an env var pointing at the mutant is shadowed by the real package and the
# check silently measures nothing. That is how this step failed the first time
# it ran: the harness was wrong, not the code under it.
MUTANT_OUT=$(cd "$API" && uv run --no-sync python -m pytest \
  tests/test_guideline_nodes_3_1.py -q -k personal_data \
  -o pythonpath="$MUTANT/src" -p no:cacheprovider 2>&1)
MUTANT_CODE=$?
if [ "$MUTANT_CODE" -ne 0 ]; then
  ok "with redaction removed, the law-30 test goes red (exit $MUTANT_CODE) — it is really testing it"
else
  bad "the law-30 test PASSES with redaction deleted, so it proves nothing"
  printf '%s\n' "$MUTANT_OUT"
fi
rm -rf "$MUTANT"

step "6. The verbatim-quote rule really refuses an invented example"
INVENTED=$(cd "$API" && uv run pytest tests/test_guideline_nodes_3_1.py -q \
  -k "invented_do_example or dont_example_must_also_be_real" 2>&1)
if [ $? -eq 0 ]; then
  ok "3.1.1 fails the node on copy the brand never published"
else
  bad "an invented example survived §11's quoting rule"
  printf '%s\n' "$INVENTED"
fi

step "7. The gates, the resume, the refusal and the canary — against a real run"
# Prefer this worktree's own wrapper when it exists. A bare `docker compose`
# here resolves to the base file's hardcoded `name: ads-research-agent`, which
# is a *different* worktree's running stack — and compose seeing a different
# config decides the network must be recreated and tears it down mid-command.
if [ -x ./dc.sh ]; then COMPOSE=(./dc.sh); else COMPOSE=(docker compose); fi
if "${COMPOSE[@]}" ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if "${COMPOSE[@]}" --profile test run --rm test pytest \
       tests/integration/test_s3p2_brand_rules.py \
       tests/integration/test_guideline_entry.py -q; then
    ok "halts on G5+G6, approver resumes, operator 403s, canary in zero prompts"
  else
    bad "the S3-P2 integration suite is red"
  fi
else
  skip "the stack is not running — 'make up' first, then re-run for the full list"
fi

step "8. Types, format and the three build guards"
(cd "$API" && uv run mypy) && ok "mypy strict" || bad "mypy"
(cd "$API" && uv run ruff check . >/dev/null && uv run ruff format --check . >/dev/null) \
  && ok "ruff check and ruff format --check" || bad "ruff"
for guard in check_route_guards check_calc_isolation check_guardrails_purity; do
  if (cd "$API" && uv run python "scripts/$guard.py" >/dev/null 2>&1); then
    ok "$guard"
  else
    bad "$guard"
  fi
done

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS3-P2 acceptance: every check passed.\033[0m\n'
else
  printf '\033[31mS3-P2 acceptance: %s check(s) failed.\033[0m\n' "$failures"
fi
exit "$failures"
