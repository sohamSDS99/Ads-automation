#!/usr/bin/env bash
# S3-P1's acceptance list, from the Stage 03 PRD §23.1:
#
#   - pytest guardrails/ and the constants suite green
#   - check_guardrails_purity.py exits 0 on the new code and non-zero when a
#     deliberate `import httpx` is added to a matcher
#   - two separate processes compiling the same fixture produce the same
#     RuleSet.hash
#   - two separate processes linting the same 100 targets produce
#     byte-identical findings
#   - a constant with `source` removed fails startup with that key's name
#   - the performance test passes
#   - mypy src/agent exits 0
#
# Nothing here needs the stack: guardrails/ reads nothing it was not handed,
# which is the property being proved. Nothing is asserted by inspection —
# each check exits non-zero on its own.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }

step "1. The guardrails and content-constants suites"
if (cd "$API" && uv run pytest tests/guardrails tests/test_content_constants.py -q); then
  ok "every matcher matches its hand-checked expectations"
else
  bad "the guardrails suite is red"
fi

step "2. guardrails/ is pure"
if (cd "$API" && uv run python scripts/check_guardrails_purity.py); then
  ok "no ORM, HTTP, LLM client, clock, random source or file read"
else
  bad "the purity guard failed on the shipped code"
fi

step "3. ...and the guard actually catches an impure import"
COPY=$(mktemp -d)
cp -R "$API/src" "$COPY/src"
printf 'import httpx\n' | cat - "$COPY/src/agent/guardrails/matchers/lexicon.py" \
  > "$COPY/lexicon.tmp" && mv "$COPY/lexicon.tmp" "$COPY/src/agent/guardrails/matchers/lexicon.py"
# Captured rather than piped: this command is SUPPOSED to exit non-zero, and
# under `set -o pipefail` that failure would poison a pipeline even when grep
# matched. The exit code is the acceptance criterion, so assert on it directly.
IMPURE_OUT=$(cd "$API" && uv run python scripts/check_guardrails_purity.py --src "$COPY/src" 2>&1)
IMPURE_CODE=$?
if [ "$IMPURE_CODE" -ne 0 ] && printf '%s' "$IMPURE_OUT" | grep -q 'imports .httpx.'; then
  ok "a deliberate httpx import in a matcher exits $IMPURE_CODE and names the line"
else
  bad "the purity guard did NOT catch an httpx import — it is proving nothing"
  printf '    exit %s\n%s\n' "$IMPURE_CODE" "$IMPURE_OUT"
fi
rm -rf "$COPY"

step "4. Two processes compiling the same fixture agree on the hash"
A=$(cd "$API" && PYTHONHASHSEED=0 uv run python -m tests.guardrails.fixture compile)
B=$(cd "$API" && PYTHONHASHSEED=12345 uv run python -m tests.guardrails.fixture compile)
if [ -n "$A" ] && [ "$A" = "$B" ]; then
  ok "identical RuleSet.hash under two different hash seeds: $A"
else
  bad "compilation is not reproducible across processes"
  printf '    seed 0:     %s\n    seed 12345: %s\n' "$A" "$B"
fi

step "5. Two processes linting 100 targets produce byte-identical findings"
C=$(cd "$API" && PYTHONHASHSEED=0 uv run python -m tests.guardrails.fixture lint)
D=$(cd "$API" && PYTHONHASHSEED=67890 uv run python -m tests.guardrails.fixture lint)
if [ -n "$C" ] && [ "$C" = "$D" ]; then
  ok "$(printf '%s' "$C" | wc -c | tr -d ' ') bytes of findings, identical under two hash seeds"
else
  bad "lint output differs between processes"
fi

step "6. A constant with no source fails startup, naming the key"
if (cd "$API" && uv run python - <<'PY'
import sys, pathlib, tempfile
sys.path.insert(0, "src")
import yaml
from agent.guidelines.constants import CONSTANTS_PATH, ContentConstantsError, load_content_constants

payload = yaml.safe_load(CONSTANTS_PATH.read_text())
del payload["image_policy"]["logo_match_score_min"]["source"]
with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory) / "content_constants.yaml"
    path.write_text(yaml.safe_dump(payload))
    try:
        load_content_constants(path)
    except ContentConstantsError as exc:
        assert "image_policy.logo_match_score_min.source" in str(exc), exc
        print(f"  refused, naming the key: {str(exc).splitlines()[-1].strip()}")
        raise SystemExit(0)
raise SystemExit("a constant with no source was accepted")
PY
); then
  ok "law 25 holds: an unattributable threshold cannot start up"
else
  bad "a constant with no source did NOT fail startup"
fi

step "7. 100 targets against a 400-rule set, under 1.5s"
if (cd "$API" && uv run pytest tests/guardrails/test_linter.py -q -k four_hundred); then
  ok "the performance budget holds, measured twice"
else
  bad "the performance test failed"
fi

step "8. mypy strict"
if (cd "$API" && uv run mypy); then
  ok "mypy src/agent exits 0"
else
  bad "mypy is red"
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS3-P1 acceptance: all 8 checks passed.\033[0m\n'
else
  printf '\033[31mS3-P1 acceptance: %d check(s) failed.\033[0m\n' "$failures"
fi
exit "$failures"
