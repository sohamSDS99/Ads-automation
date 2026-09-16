#!/usr/bin/env bash
# P5a's exit criteria, run against a live stack.
#
#   "emits `ResearchReport` and all 5 export formats; PDF has TOC + page
#    numbers; DOCX TOC updates in Word"                           — PRD §17, P5
#
# P5 in the PRD is one phase covering stage 1.5, the synthesis nodes and the
# export system. P5a is the second half: the report contract, the five
# renderers, the export job and the download path. The nodes that WRITE a
# report are P5b, so this script seeds one through the API's own database and
# then proves the rest end to end.
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress — and because the download crosses
# two more hops than the test suite can reach: api -> worker file server ->
# Volume. That chain is the whole point of the phase, and it is exactly what an
# in-process test cannot prove.
#
#   make up && ./scripts/verify-p5a.sh
#   BASE_URL=http://localhost:3001 ./scripts/verify-p5a.sh   # a non-default stack
#
# Idempotent: the project, run and report it needs are created once and reused.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}

PASS=0; FAIL=0; SKIPPED=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
has(){ case "$2" in *"$3"*) ok "$1";; *) no "$1" "missing: $3";; esac; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

C=$(mktemp); OUT=$(mktemp -d); trap 'rm -rf "$C" "$OUT"' EXIT
csrf(){ curl -s -c "$C" -b "$C" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$C" | awk '{print $7}'; }

echo "── 0. sign in ──────────────────────────────────────────────────────────"
T=$(csrf)
ROLE=$(curl -s -c "$C" -b "$C" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" "$B/auth/login" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. seed a report (P5b's job; done here so P5a can be proven) ─────────"
# Runs inside the api container: it is the only place with the database URL, and
# this is the one step of the phase that has no endpoint yet.
SEED=$($COMPOSE exec -T api python - <<'PY' 2>&1 | tail -3
import asyncio, json, pathlib, sys, uuid

sys.path.insert(0, "/app/src")
from agent.db.models import Project, Report, Run, RunStatus, RunTrigger, User
from agent.db.session import get_sessionmaker
from agent.export.contract import ResearchReport
from agent.export.markdown import render_markdown
import sqlalchemy as sa

FIXTURE = pathlib.Path("/app/tests/fixtures/report_golden.json")
PROJECT = "P5a verification"


async def main() -> None:
    payload = json.loads(FIXTURE.read_text())
    async with get_sessionmaker()() as session:
        user = (await session.execute(sa.select(User).limit(1))).scalar_one()

        project = (
            await session.execute(sa.select(Project).where(Project.name == PROJECT))
        ).scalar_one_or_none()
        if project is None:
            project = Project(
                workspace_id=user.workspace_id,
                name=PROJECT,
                domain="northwind.example",
                created_by=user.id,
            )
            session.add(project)
            await session.flush()

        run = (
            await session.execute(
                sa.select(Run)
                .where(Run.project_id == project.id)
                .order_by(Run.started_at.desc().nullslast(), Run.id.desc())
            )
        ).scalars().first()
        if run is None:
            run = Run(
                workspace_id=user.workspace_id,
                project_id=project.id,
                triggered_by=user.id,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.SUCCEEDED,
            )
            session.add(run)
            await session.flush()

        payload["project_id"] = str(project.id)
        payload["run_id"] = str(run.id)
        parsed = ResearchReport.model_validate(payload)

        report = (
            await session.execute(sa.select(Report).where(Report.run_id == run.id))
        ).scalar_one_or_none()
        if report is None:
            session.add(
                Report(
                    run_id=run.id,
                    schema_version=parsed.schema_version,
                    payload=json.loads(parsed.model_dump_json()),
                    markdown=render_markdown(parsed, project_name=project.name),
                )
            )
        await session.commit()
        print(f"RUN_ID={run.id}")


asyncio.run(main())
PY
)
RUN_ID=$(printf '%s' "$SEED" | sed -n 's/^RUN_ID=//p' | tr -d '\r')
if [ -z "$RUN_ID" ]; then
  no "seed a report" "$SEED"
  echo "cannot continue without a report"; exit 1
fi
ok "seeded a report for run $RUN_ID"

echo "── 2. GET /reports/{run_id} ────────────────────────────────────────────"
BODY=$(curl -s -b "$C" "$B/reports/$RUN_ID")
chk "schema_version"        "$(printf '%s' "$BODY" | jq_ 'd["schema_version"]')" "1.0"
chk "launch_readiness"      "$(printf '%s' "$BODY" | jq_ 'd["payload"]["launch_readiness"]')" "go_with_fixes"
chk "priced keywords"       "$(printf '%s' "$BODY" | jq_ 'len(d["payload"]["priced_keyword_list"])')" "5"
has "markdown is rendered"  "$BODY" "Paid Ads Research Report"

echo "── 3. all five formats generate and download ───────────────────────────"
for FMT in pdf docx md json csv; do
  T=$(csrf)
  JOB=$(curl -s -b "$C" -c "$C" -X POST -H "X-CSRF-Token: $T" \
        "$B/reports/$RUN_ID/export?format=$FMT" | jq_ 'd["job_id"]')
  if [ -z "$JOB" ]; then no "POST export?format=$FMT" "no job_id returned"; continue; fi

  # The worker renders it. Poll rather than sleep: a PDF with screenshots takes
  # longer than an md, and a fixed sleep either flakes or wastes a minute.
  STATUS=queued
  for _ in $(seq 1 60); do
    STATUS=$(curl -s -b "$C" "$B/exports/$JOB" | jq_ 'd["status"]')
    case "$STATUS" in ready|failed) break;; esac
    sleep 1
  done
  if [ "$STATUS" != "ready" ]; then
    no "export $FMT reaches ready" "status=$STATUS: $(curl -s -b "$C" "$B/exports/$JOB" | jq_ 'd["error"]')"
    continue
  fi
  ok "export $FMT reaches ready"

  FILE="$OUT/report.$FMT"
  DISPOSITION=$(curl -s -b "$C" -D - -o "$FILE" "$B/exports/$JOB/download" | tr -d '\r' \
                | grep -i '^content-disposition:')
  SIZE=$(wc -c < "$FILE" | tr -d ' ')
  REPORTED=$(curl -s -b "$C" "$B/exports/$JOB" | jq_ 'd["bytes"]')
  chk "download $FMT is the size the API reported" "$SIZE" "$REPORTED"
  has "download $FMT is an attachment" "$DISPOSITION" "attachment;"
  has "download $FMT is named for the report" "$DISPOSITION" ".$FMT"
done

echo "── 4. the bytes are the format they claim ──────────────────────────────"
has "PDF magic number"   "$(head -c 5 "$OUT/report.pdf" 2>/dev/null)" "%PDF-"
has "DOCX is a zip"      "$(head -c 2 "$OUT/report.docx" 2>/dev/null)" "PK"
has "MD is the report"   "$(head -c 40 "$OUT/report.md" 2>/dev/null)" "Paid Ads Research Report"
chk "JSON parses"        "$(jq_ 'd["schema_version"]' < "$OUT/report.json")" "1.0"
has "CSV leads with Editor's columns" "$(head -c 40 "$OUT/report.csv" 2>/dev/null)" "Campaign,Ad Group,Keyword"

echo "── 5. the PDF has a TOC and page numbers ───────────────────────────────"
# §12's acceptance. Two checks, because they need different tools.
#
# The page COUNT is readable from the file's own structure. The page-number and
# contents TEXT is not: WeasyPrint subsets its fonts to hex glyph ids, so
# without the embedded ToUnicode CMap a grep finds font data, not words. That
# needs poppler. When poppler is absent this SKIPS loudly rather than passing —
# a check that quietly downgrades to "no news is good news" is worse than none.
PDF_PAGES=$(python3 - "$OUT/report.pdf" <<'PYPAGES' 2>/dev/null
import pathlib, re, sys, zlib

raw = pathlib.Path(sys.argv[1]).read_bytes()
blob = []
for match in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
    try:
        blob.append(zlib.decompress(match.group(1)))
    except zlib.error:
        continue
joined = b"".join(blob) + raw
# `/Type /Pages` is the tree node, not a page.
print(joined.count(b"/Type /Page") - joined.count(b"/Type /Pages"))
PYPAGES
)
if [ "${PDF_PAGES:-0}" -gt 1 ] 2>/dev/null; then
  ok "PDF has $PDF_PAGES pages"
else
  no "PDF has more than one page" "counted ${PDF_PAGES:-0}"
fi

if command -v pdftotext >/dev/null 2>&1; then
  pdftotext -layout "$OUT/report.pdf" "$OUT/report.txt" 2>/dev/null
  TXT=$(cat "$OUT/report.txt" 2>/dev/null)
  has "PDF has a contents page"     "$TXT" "Contents"
  has "PDF cover names the project" "$TXT" "P5a verification"
  # The TOC's page numbers come from target-counter; a dangling href prints 0.
  if printf '%s' "$TXT" | grep -qE '\.{5,} *[1-9][0-9]*'; then
    ok "contents entries carry real page numbers"
  else
    no "contents entries carry real page numbers" "no leader dots + number found"
  fi
  if printf '%s' "$TXT" | grep -qE "[0-9]+ */ *$PDF_PAGES"; then
    ok "PDF footers number pages (n / $PDF_PAGES)"
  else
    no "PDF footers number pages" "no 'n / $PDF_PAGES' footer found"
  fi
  has "PDF footers carry the run id" "$TXT" "Run $RUN_ID"
else
  printf "  \033[33mSKIP\033[0m PDF text checks — install poppler (pdftotext) to run them\n"
  SKIPPED=$((SKIPPED+1))
fi

echo "── 6. the DOCX TOC will update in Word ─────────────────────────────────"
DOCX_CHECK=$(python3 - "$OUT/report.docx" <<'PY' 2>&1
import re, sys, zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    document = archive.read("word/document.xml").decode()
    settings = archive.read("word/settings.xml").decode()
    footers = "".join(
        archive.read(name).decode()
        for name in archive.namelist()
        if name.startswith("word/footer")
    )

print(f"FIELD={'TOC ' in document}")
print(f"DIRTY={'w:dirty=\"true\"' in document}")
print(f"UPDATE={'updateFields' in settings}")
print(f"STYLES={sorted(set(re.findall(r'Heading[123]', document)))}")
print(f"PAGEFIELD={' PAGE ' in footers}")
print(f"TABLES={document.count('<w:tbl>')}")
PY
)
has "DOCX carries a TOC field"          "$DOCX_CHECK" "FIELD=True"
has "the field is marked dirty"         "$DOCX_CHECK" "DIRTY=True"
has "Word is told to update on open"    "$DOCX_CHECK" "UPDATE=True"
has "headings use real Heading styles"  "$DOCX_CHECK" "['Heading1', 'Heading2', 'Heading3']"
has "footer numbers pages with a field" "$DOCX_CHECK" "PAGEFIELD=True"

echo "── 7. the download path is authorised, not merely routed ───────────────"
ANON=$(curl -s -o /dev/null -w '%{http_code}' "$B/reports/$RUN_ID")
chk "GET /reports without a session" "$ANON" "401"

# The worker's file server must refuse an unsigned request. It has no ingress,
# so the probe runs from inside the api container, over the private network —
# which is exactly the position an attacker who reached the network would be in.
UNSIGNED=$($COMPOSE exec -T api python -c "
import httpx, os
url = os.environ.get('WORKER_INTERNAL_URL', 'http://worker:8081')
print(httpx.get(url + '/files/exports/anything/report.pdf', timeout=10).status_code)
" 2>&1 | tail -1 | tr -d '\r')
chk "worker file server refuses an unsigned read" "$UNSIGNED" "403"

HEALTH=$($COMPOSE exec -T api python -c "
import httpx, os
url = os.environ.get('WORKER_INTERNAL_URL', 'http://worker:8081')
print(httpx.get(url + '/health', timeout=10).json()['service'])
" 2>&1 | tail -1 | tr -d '\r')
chk "worker file server is reachable on the private network" "$HEALTH" "fileserver"

echo
echo "──────────────────────────────────────────────────────────────────────────"
printf "  %d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
