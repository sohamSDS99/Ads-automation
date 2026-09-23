#!/usr/bin/env bash
# S3-P6's acceptance list, from the Stage 03 PRD §21:
#
#   A full 19-node run emits a `ContentGuideline` passing every §12.1
#   invariant; publish is transactional and mints an immutable `RuleSet`;
#   publishing with an undecided gate or an incomplete H1 returns `409` listing
#   what is outstanding; all six exports generate; two exports of a published
#   version are byte-identical; Stage 04's endpoint returns a pinnable ruleset
#   and `404` when nothing is published.
#
# Steps 1-9 need nothing but the repo. Step 10 needs the stack, because
# "publish is one transaction" is a claim about a database and nothing smaller
# than a database proves it.
#
# Two steps are **mutation checks** (6 and 9). They break a guarantee on purpose
# and assert the suite goes red. A test that would still pass with the guard
# deleted is not evidence, and the only way to know is to delete it and look.
#
# §21 says "19-node". The DAG is **18**: 3.5.3 (`disapproval_rule_synthesis`)
# is explicitly S3-P9's, and it reads a `DisapprovalEvent` history that S3-P9's
# ingest is what creates. Step 2 asserts 18 and names the absentee, rather than
# asserting 19 and failing on a node the PRD itself defers.
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
      tests/test_guideline_synthesis.py \
      tests/test_guideline_critique.py \
      tests/test_guideline_nodes_3_5.py \
      tests/test_guideline_nodes_3_6.py \
      tests/test_guideline_exports.py -q); then
  ok "the assembler, the ten assertions, 3.5.2, 3.6.1/3.6.2 and the six exports"
else
  bad "a unit suite for this phase is red"
fi

step "2. The guideline DAG is complete, and 3.6.1 folds every node"
if (cd "$API" && uv run python -c "
from agent.db.models import RunStage
from agent.orchestrator.dag import get_dag
from agent.orchestrator.registry import get_registry

registry = get_registry()
ids = sorted(i for i in registry.ids if i.startswith('3.'))
assert len(ids) == 18, f'expected 18 stage-03 nodes, found {len(ids)}: {ids}'
assert '3.5.3' not in ids, '3.5.3 is S3-P9; if it landed, update this check and §21'

depends = set(registry.spec('3.6.1').depends_on)
others = {i for i in ids if not i.startswith('3.6.')}
assert depends == others, f'3.6.1 does not fold every node: {others - depends}'
assert registry.spec('3.6.2').depends_on == ('3.6.1',)

waves = [set(w) for w in get_dag(RunStage.GUIDELINE).waves()]
assert waves[-1] == {'3.6.2'} and waves[-2] == {'3.6.1'}, waves
print(f'18 nodes, {len(waves)} waves, 3.6.1 folds {len(depends)}')
" ); then
  ok "18 nodes, 5 waves, 3.6.1 depends on all 16 non-report nodes"
else
  bad "the DAG is not what §11's edges describe"
fi

step "3. A rulebook compiles, and compiling it twice gives one hash"
if (cd "$API" && uv run python -c "
import datetime as dt
from agent.guardrails import compiler
from agent.guidelines.constants import load_content_constants
from tests.guideline_fixtures import GENERATED_AT, build

payload = build().model_dump(mode='json')
constants = load_content_constants()
a = compiler.compile(payload, constants, (), compiled_at=GENERATED_AT)
b = compiler.compile(payload, constants, (), compiled_at=dt.datetime.now(dt.UTC))
assert a.hash == b.hash, f'{a.hash} != {b.hash}'
assert len(a.rules) > 0, 'a rulebook that compiles to no rules enforces nothing'
print(f'{len(a.rules)} rules -> {a.ruleset_version}')
"); then
  ok 'the ruleset is reproducible, and compiled_at is outside the hash'
else
  bad "compiling the same rulebook twice did not give one hash"
fi

step "4. Every flattened rule is registered and every category is reachable"
if (cd "$API" && uv run python -c "
from agent.guardrails.registry import RULES
from tests.guideline_fixtures import build

rules = build().rules
unknown = sorted({r.rule_id for r in rules if r.rule_id not in RULES})
assert not unknown, f'unregistered rule ids would compile to nothing: {unknown}'

# The sections a rulebook must actually enforce. A section that stops
# flattening still renders in the PDF, so only this notices.
want = {'voice', 'lexicon', 'claim', 'offer', 'asset_spec', 'disclosure', 'governance'}
have = {r.category for r in rules}
assert want <= have, f'these sections reached no rule: {sorted(want - have)}'
print(f'{len(rules)} rules across {len(have)} categories')
"); then
  ok "no unregistered rule id, and all seven authored sections enforce something"
else
  bad "a section is stated in prose and enforced by nothing"
fi

step "5. The ten critique assertions pass a healthy rulebook and catch a broken one"
if (cd "$API" && uv run python -c "
import uuid
from datetime import UTC, datetime
from agent.export.guideline_contract import RegisteredClaim
from agent.guidelines import critique as checks
from tests.guideline_fixtures import build

now = datetime.now(UTC)
clean = checks.blocking(checks.run_checks(build(), now=now))
assert not clean, [i.finding for i in clean]

# An approved claim nobody signed — §11 assertion 3.
broken = build(claims=[RegisteredClaim(
    claim_id=uuid.uuid4(), claim_text='The best SDS software',
    normalized_text='the best sds software', status='approved')])
found = {i.check for i in checks.blocking(checks.run_checks(broken, now=now))}
assert 'approved_claims_signed' in found, found
print('10 assertions: healthy clean, unsigned-approved caught')
"); then
  ok "a healthy rulebook publishes and an unsigned approved claim does not"
else
  bad "the critique either over-fires on a healthy rulebook or misses a real defect"
fi

step "6. MUTATION — deleting the signature assertion must turn the suite red"
CRIT="$API/src/agent/guidelines/critique.py"
cp "$CRIT" /tmp/s3p6-critique.bak
if python3 - "$CRIT" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "    issues.extend(check_approved_claims_are_signed(guideline, now=now, hashes=signature_set_hashes))"
if old not in s:
    sys.exit("mutation target not found — this check would have proved nothing")
p.write_text(s.replace(old, "    pass  # mutated"))
PY
then
  if (cd "$API" && uv run pytest tests/test_guideline_critique.py -q >/dev/null 2>&1); then
    bad "the suite still passes with assertion 3 deleted — it is not testing it"
  else
    ok "deleting §11 assertion 3 turns the critique suite red"
  fi
else
  bad "could not apply the mutation"
fi
cp /tmp/s3p6-critique.bak "$CRIT"

step "7. All six exports generate, and are byte-identical across a moved clock"
if (cd "$API" && uv run python -c "
import datetime as dt
from unittest.mock import patch
from agent.db.models import ExportFormat
from agent.export.jobs import CONTENT_GUIDELINE_FORMATS, render_guideline
from agent.guardrails import compiler
from agent.guidelines.constants import load_content_constants
from tests.guideline_fixtures import GENERATED_AT, build

g = build()
rs = compiler.compile(g.model_dump(mode='json'), load_content_constants(), (), compiled_at=GENERATED_AT)
kw = dict(project_name='SDS Manager', compiled_ruleset=rs.model_dump(mode='json'), ruleset_hash=rs.hash)
assert len(CONTENT_GUIDELINE_FORMATS) == 6, CONTENT_GUIDELINE_FORMATS

real = dt.datetime
class Shifted(real):
    @classmethod
    def now(cls, tz=None): return real.now(tz) + dt.timedelta(hours=1)

for fmt in sorted(CONTENT_GUIDELINE_FORMATS, key=lambda f: f.value):
    first = render_guideline(fmt, g, **kw).payload
    with patch.object(dt, 'datetime', Shifted):
        second = render_guideline(fmt, g, **kw).payload
    assert len(first) > 0, fmt
    assert first == second, f'{fmt.value} is not reproducible across a clock tick'
    print(f'  {fmt.value:14} {len(first):>8} bytes')
"); then
  ok "six formats, each reproducible an hour later"
else
  bad "an export failed to generate or was not byte-identical"
fi

step "8. Every draft export is watermarked; a published one is not"
if (cd "$API" && uv run python -c "
import io, openpyxl
from agent.export.guideline_markdown import render_guideline_markdown
from agent.export.guideline_pdf import render_guideline_html
from agent.export.guideline_view import DRAFT_WATERMARK
from agent.export.guideline_xlsx import render_guideline_xlsx
from tests.guideline_fixtures import build

draft = build()
assert DRAFT_WATERMARK in render_guideline_markdown(draft, project_name='X')
html = render_guideline_html(draft, project_name='X')
assert 'class=\"watermark\"' in html and 'class=\"draft-banner\"' in html
css = open('src/agent/export/templates/guideline.css').read()
block = css.split('.watermark {', 1)[1].split('}', 1)[0]
assert 'position: fixed' in block, 'the mark would render once and scroll away'

book = openpyxl.load_workbook(io.BytesIO(render_guideline_xlsx(draft)))
for name in book.sheetnames:
    assert DRAFT_WATERMARK in str(book[name]['A1'].value), name

published = build(); published.status = 'published'
assert DRAFT_WATERMARK not in render_guideline_markdown(published, project_name='X')
print(f'watermarked: md, print html (fixed), {len(book.sheetnames)} xlsx sheets')
"); then
  ok "a draft cannot circulate as a legal sign-off record, in any format"
else
  bad "a draft export is missing its watermark, or a published one carries one"
fi

step "9. MUTATION — a tampered ruleset must be refused, not exported"
if (cd "$API" && uv run python -c "
from agent.export.ruleset_json import RulesetExportError, verify
from agent.guardrails import compiler
from agent.guidelines.constants import load_content_constants
from tests.guideline_fixtures import GENERATED_AT, build

rs = compiler.compile(build().model_dump(mode='json'), load_content_constants(), (), compiled_at=GENERATED_AT)
compiled = rs.model_dump(mode='json')
verify(compiled, stored_hash=rs.hash)          # the faithful row passes

compiled['rules'] = []                          # somebody emptied the rules
try:
    verify(compiled, stored_hash=rs.hash)
except RulesetExportError:
    print('tampered ruleset refused')
else:
    raise SystemExit('a ruleset whose hash is a lie was handed to Stage 04')
"); then
  ok "the hash is re-derived and a mismatch refuses the export"
else
  bad "a tampered ruleset would reach Stage 04"
fi

step "10. Publish, the 409s and the Stage 04 endpoint (needs the stack)"
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
  if docker compose run --rm test pytest tests/integration/test_s3p6_publish.py -q; then
    ok "publish is transactional, refuses with a list, and the pin resolves"
  else
    bad "the publish integration suite is red"
  fi
else
  skip "postgres is not running — run 'make up' first (§12.4 is a claim about a transaction)"
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
  printf '\033[32mS3-P6 acceptance: %d checks passed\033[0m' "$((10 - skipped))"
  [ "$skipped" -gt 0 ] && printf '\033[33m, %d skipped\033[0m' "$skipped"
  printf '\n'
  exit 0
fi
printf '\033[31mS3-P6 acceptance: %d check(s) failed\033[0m\n' "$failures"
exit 1
