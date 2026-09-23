#!/usr/bin/env bash
# S3-P5's acceptance list, from the Stage 03 PRD §21:
#
#   An unbound run emits specs for every campaign type and a plan-bound run
#   emits only the slate's; an image over the coverage threshold produces a
#   blocking finding with the ratio and the OCR text in evidence; the same
#   image linted twice produces the same metrics; with OCR unavailable the
#   verdict is `indeterminate`, never `pass`.
#
# Steps 1-7 need nothing but the repo and a host `tesseract`. Step 8 needs the
# stack. Step 9 needs Docker, because "the worker image carries the OCR engine"
# is a claim about an image and nothing smaller than building it proves so.
#
# Step 6 is a **mutation check**: it breaks law 31 on purpose and asserts the
# suite goes red. A test that would still pass with the guard deleted is not
# evidence, and the only way to know is to delete it and look.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
API=apps/api
failures=0
skipped=0

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skipped=$((skipped + 1)); }

step "1. The phase's own unit suites"
if (cd "$API" && uv run pytest \
      tests/test_imaging_precheck.py \
      tests/test_guideline_nodes_3_4.py \
      tests/test_image_lint_route.py \
      tests/guardrails/test_image_and_disclosure.py \
      tests/test_content_constants.py -q); then
  ok "the measurement, the three nodes, the verdict rule and the image rules"
else
  bad "a unit suite for this phase is red"
fi

step "2. The guideline DAG gained stage 3.4, with §11's edges and no halts"
if (cd "$API" && uv run python -c "
from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry

ids = sorted(get_dag(RunStage.GUIDELINE).node_ids)
for wanted in ('3.4.1', '3.4.2', '3.4.3'):
    assert wanted in ids, (wanted, ids)

reg = get_registry()
assert reg.spec('3.4.1').depends_on == ('3.3.1',), reg.spec('3.4.1').depends_on
assert reg.spec('3.4.2').depends_on == ('3.4.1',), reg.spec('3.4.2').depends_on
assert set(reg.spec('3.4.3').depends_on) == {'3.4.1', '3.1.3'}, reg.spec('3.4.3').depends_on

# Law 28: two gates and two person-tasks, and none of them is in 3.4.
for node_id in ('3.4.1', '3.4.2', '3.4.3'):
    spec = reg.spec(node_id)
    assert spec.gate is False and spec.gate_key is None, node_id
    assert spec.human_task_key is None, node_id
print('3.4.1, 3.4.2, 3.4.3 wired; edges match §11; no gate, no person-task')
"); then
  ok "the three nodes are in the DAG with the PRD's edges"
else
  bad "the DAG does not match §11"
fi

step "3. Unbound emits every campaign type; plan-bound emits only the slate's"
if (cd "$API" && uv run python -c "
import asyncio, sys
sys.path.insert(0, 'tests')
from agent.export.plan_contract import ChannelSlate, SlateEntry
from agent.guidelines.constants import get_content_constants
from agent.nodes.content.stage_3_4 import asset_spec_sheet
from agent.schemas.guideline_input import GuidelineBindings, GuidelineInput
from tests.guideline_support import PROJECT_ID, RUN_ID, harness

def run(slate):
    bench = harness('3.4.1')
    bench.ctx.scratch['guideline_input'] = GuidelineInput(
        project_id=PROJECT_ID, guideline_run_id=RUN_ID, bindings=GuidelineBindings(),
        channel_slate=slate, constants_version=get_content_constants().version)
    return asyncio.run(asset_spec_sheet.reason(bench.ctx, []))

known = set(get_content_constants().asset_specs)
unbound = run(None)
assert unbound.scope == 'unscoped', unbound.scope
assert set(unbound.campaign_types) == known, (unbound.campaign_types, known)

bound = run(ChannelSlate(slate=[SlateEntry(campaign_type='search', market='GB')]))
assert bound.scope == 'scoped', bound.scope
assert bound.campaign_types == ['search'], bound.campaign_types
assert len(bound.specs) < len(unbound.specs), 'binding a plan must NARROW the sheet'
print(f'unbound: {len(unbound.specs)} specs over {sorted(known)}; bound: {len(bound.specs)} over [search]')
"); then
  ok "§11's scoping rule holds in both directions"
else
  bad "the scoping rule is wrong"
fi

step "4. The same image measured in two PROCESSES produces identical metrics"
# Two processes, not two calls: Python randomises string hashing per process,
# so a same-process check passes even when a result leaks out of a set.
if (cd "$API" && uv run python -c "
import json, os, subprocess, sys

SNIPPET = '''
import io, json, sys
from PIL import Image, ImageDraw, ImageFont
from agent.imaging import precheck
img = Image.new(\"RGB\", (1200, 628), (18, 32, 64))
d = ImageDraw.Draw(img)
d.rectangle([40, 40, 300, 140], fill=(240, 120, 20))
d.text((60, 220), \"Safety Data Sheets for every site\", font=ImageFont.load_default(size=56), fill=(255, 255, 255))
buf = io.BytesIO(); img.save(buf, format=\"PNG\")
m = precheck.measure(buf.getvalue()).model_dump(mode=\"json\")
m.pop(\"measured_ms\")
print(json.dumps(m, sort_keys=True))
'''

out = []
for seed in ('0', '1', '12345'):
    env = {**os.environ, 'PYTHONHASHSEED': seed}
    r = subprocess.run([sys.executable, '-c', SNIPPET], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(r.stderr[-800:]); raise SystemExit(1)
    out.append(r.stdout.strip())
assert len(set(out)) == 1, 'two processes disagreed about the same image'
status = json.loads(out[0])['status']
print(f'3 processes, identical metrics (status={status})')
"); then
  ok "the measurement is reproducible across processes"
else
  bad "the measurement is not deterministic"
fi

step "5. An unmeasured image is indeterminate; a measured failure still fails"
if (cd "$API" && uv run python -c "
from datetime import UTC, datetime
from agent.api.routes_guidelines import image_verdict
from agent.guardrails.linter import lint
from agent.guardrails.matchers.image import text_coverage
from agent.schemas.guardrails import Authority, LintTarget, RuleSet

rule = text_coverage(authority=Authority(source='internal', reference='k', reviewed_at='2026-09-23'), maximum=0.2)
ruleset = RuleSet(ruleset_version='t', project_id='00000000-0000-0000-0000-000000000001',
                  guideline_id='00000000-0000-0000-0000-000000000002', compiler_version='t',
                  constants_version='t', compiled_at=datetime.now(UTC), rules=(rule,), hash='h')

# image_metrics=None is what a detector_unavailable measurement produces.
target = LintTarget(ref='i', surface='display_text', campaign_type='search', market='GB',
                    language='en', image_ref='sha', image_metrics=None)
result = lint([target], ruleset, now=datetime.now(UTC))

# The trap is not the one §18's wording suggests. An indeterminate finding still
# carries its rule's registered severity, and image.text_coverage.v1 is
# registered blocking — so the LINTER reports fail for an image nothing was
# measured on. That is not a pass, so law 31 is not broken; it is a different
# lie. It tells a writer their picture is bad when the truth is that the OCR
# engine is missing, and they will redo artwork that was fine.
assert result.verdict == 'fail', result.verdict
assert all(f.indeterminate for f in result.findings), 'nothing was actually measured'

verdict, unchecked = image_verdict(result)
assert verdict == 'indeterminate', verdict
assert unchecked is True

# And the half law 31 names directly: a measured failure must still say fail.
measured = LintTarget(ref='i', surface='display_text', campaign_type='search', market='GB',
                      language='en', image_ref='sha', image_metrics={'text_coverage_ratio': 0.34})
real = lint([measured], ruleset, now=datetime.now(UTC))
assert image_verdict(real) == ('fail', False), image_verdict(real)
print('unmeasured -> indeterminate (the linter alone said fail); measured 0.34 -> fail')
"); then
  ok "unmeasured and failed are told apart, and neither is a pass"
else
  bad "the image verdict does not distinguish unmeasured from failed"
fi

step "6. MUTATION CHECK — breaking law 31 must turn the suite red"
MUT="$API/src/agent/schemas/imaging.py"
cp "$MUT" /tmp/s3p5-imaging.bak
# The guard is `metrics()` omitting an absent metric. Defaulting it to 0.0 is
# the plausible-looking edit that would make every unmeasured image pass.
python3 - "$MUT" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
s = p.read_text()
s = s.replace(
    "return {key: value for key, value in found.items() if value is not None}",
    "return {key: (0.0 if value is None else value) for key, value in found.items()}",
)
p.write_text(s)
PY
if (cd "$API" && uv run pytest tests/test_imaging_precheck.py -q >/dev/null 2>&1); then
  bad "law 31 was deleted and the suite still passed — the tests prove nothing"
else
  ok "deleting the law-31 guard turns the suite red"
fi
cp /tmp/s3p5-imaging.bak "$MUT" && rm -f /tmp/s3p5-imaging.bak

step "7. Lint, format, types and the three guards"
(cd "$API" && uv run ruff check src tests scripts >/dev/null 2>&1) \
  && ok "ruff check" || bad "ruff check"
(cd "$API" && uv run ruff format --check src tests scripts >/dev/null 2>&1) \
  && ok "ruff format --check" || bad "ruff format --check"
(cd "$API" && uv run mypy src >/dev/null 2>&1) \
  && ok "mypy strict" || bad "mypy strict"
(cd "$API" && uv run python scripts/check_route_guards.py >/dev/null 2>&1) \
  && ok "every route declares require(Permission)" || bad "route guards"
(cd "$API" && uv run python scripts/check_guardrails_purity.py >/dev/null 2>&1) \
  && ok "guardrails/ stayed pure (it must not reach imaging/)" || bad "guardrails purity"

step "8. The route, against the stack"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_s3p5_image_lint.py -q; then
    ok "the image lint route end to end"
  else
    bad "the image lint route suite is red"
  fi
else
  skip "postgres is not running — 'make up' first (the route suite did not run)"
fi

step "9. The WORKER image carries the OCR engine and the api image does not"
# `docker info` is not enough to know Docker works: a daemon whose storage has
# filled answers `info` happily and then fails every build with an I/O error on
# its own metadata. Probing with a trivial build is the difference between
# "this image is wrong" and "this machine cannot build images", and reporting
# the second as the first is how a gate sends somebody to debug a Dockerfile
# that is fine.
DOCKER_OK=0
if docker info >/dev/null 2>&1; then
  if printf 'FROM alpine:3\nRUN true\n' | docker build -q -t s3p5-docker-probe - >/dev/null 2>&1; then
    DOCKER_OK=1
    docker rmi -f s3p5-docker-probe >/dev/null 2>&1 || true
  fi
fi

if [ "$DOCKER_OK" -eq 1 ]; then
  if docker build -q -f Dockerfile -t s3p5-worker-verify . >/dev/null 2>&1 \
     && docker run --rm s3p5-worker-verify sh -c 'tesseract --version >/dev/null 2>&1 && tesseract --list-langs 2>&1 | grep -qx eng'; then
    ok "worker: tesseract present with 'eng' language data"
  else
    bad "the worker image cannot run tesseract"
  fi
  # §6 keeps `api` slim. The Python packages are shared (one lockfile, the same
  # convention weasyprint already uses); only the native binary is worker-only.
  if docker build -q -f "$API/Dockerfile" -t s3p5-api-verify "$API" >/dev/null 2>&1 \
     && ! docker run --rm s3p5-api-verify sh -c 'command -v tesseract >/dev/null 2>&1'; then
    ok "api: no tesseract, exactly as §6 requires"
  else
    bad "the api image grew a tesseract dependency"
  fi
else
  skip "Docker cannot build here — the two image claims are UNVERIFIED, not passing"
fi

printf '\n'
# A summary that says "every check passed" while some never ran is the same
# lie this phase spent its whole budget arguing against. Skips are reported on
# their own line, every time, whether or not anything failed.
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS3-P5: every check that ran passed.\033[0m\n'
else
  printf '\033[31mS3-P5: %d check(s) failed.\033[0m\n' "$failures"
fi
if [ "$skipped" -gt 0 ]; then
  printf '\033[33mS3-P5: %d check(s) did NOT run — those claims are unverified.\033[0m\n' "$skipped"
fi
exit "$failures"
