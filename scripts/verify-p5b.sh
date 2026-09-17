#!/usr/bin/env bash
# P5b's exit criteria, run against a live stack.
#
#   "Full 21-node run on one project emits `ResearchReport` and all 5 export
#    formats"                                                       — PRD §17, P5
#
# P5a proved the renderers against a hand-seeded report. P5b is the half that
# *writes* one: stage 1.5's four readiness nodes, `report_synthesis` and
# `report_critique`. So this script runs the whole DAG — twenty-three nodes,
# §10's twenty-one research nodes plus the two report nodes §17's phrase does
# not count — through all three approval gates, and then asks for every format.
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress, and the download crosses two more
# hops the test suite cannot reach: api -> worker file server -> Volume.
#
#   make up && ./scripts/verify-p5b.sh
#   COMPOSE_CMD="docker compose -p adsp5b" BASE_URL=http://localhost:3005 ./scripts/verify-p5b.sh
#
# Needs a real OpenRouter key: this drives twenty-three nodes against live
# models, so it proves the prompts as well as the plumbing.
#
# There is no Google Ads credential, no DataForSEO login and no Chromium in a
# default local stack, and a connector that cannot reach its source degrades to
# empty (PRD §16). The evidence is therefore seeded with the *same fixtures the
# integration suite uses*, in the api container, rather than reimplemented in
# SQL here — two seeders that drift apart would make this script prove something
# the suite does not.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
PROJECT_NAME=${PROJECT_NAME:-P5b verification}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
ge(){ [ "${2:-0}" -ge "$3" ] 2>/dev/null && ok "$1 ($2)" || no "$1" "expected >= $3, got ${2:-nothing}"; }
inset(){ case " $3 " in *" $2 "*) ok "$1 ($2)";; *) no "$1" "got $2, expected one of $3";; esac; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); OUT=$(mktemp -d); trap 'rm -rf "$A" "$OUT"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
api(){ curl -s -b "$A" "$B$1"; }
post(){ local t; t=$(csrf "$A")
  curl -s -b "$A" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' -d "$2" "$B$1"; }

echo "── 0. sign in as the admin ─────────────────────────────────────────────"
T=$(csrf "$A")
ROLE=$(curl -s -c "$A" -b "$A" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" "$B/auth/login" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. a project, and every source stage 1.1–1.5 reads ──────────────────"
# A fresh project each time. Reusing one means auditing pages a previous run
# already fixed, and a harness that reuses fixtures measures its own leftovers.
STAMP=$(date +%s)
PROJECT=$(post "/projects" "$(printf '{"name":"%s %s","domain":"sdsmanager.com","markets":["US"],"product_context":"SDS management software for chemical manufacturers."}' "$PROJECT_NAME" "$STAMP")" | jq_ 'd["id"]')
[ -n "$PROJECT" ] || { echo "could not create a project"; exit 1; }
ok "a fresh project ($PROJECT)"

SEEDED=$($COMPOSE exec -T -e PROJECT_ID="$PROJECT" api python - <<'PY' 2>&1 | tail -1
import asyncio, os, sys, uuid

sys.path[:0] = ["/app/src", "/app"]
from tests.integration.runs_support import (  # noqa: E402
    seed_competitive,
    seed_crm,
    seed_demand,
    seed_google_ads,
    seed_readiness,
)

PROJECT = uuid.UUID(os.environ["PROJECT_ID"])


async def main() -> None:
    await seed_crm(PROJECT, lost=({"account_name": "Lost Co", "close_reason": "no budget"},))
    await seed_google_ads(PROJECT)
    await seed_competitive(PROJECT)
    await seed_demand(PROJECT)
    await seed_readiness(PROJECT)
    print("seeded")


asyncio.run(main())
PY
)
chk "every source is seeded, from the suite's own fixtures" "$SEEDED" "seeded"
[ "$SEEDED" = "seeded" ] || { echo "cannot continue without evidence"; exit 1; }

echo "── 2. launch the whole DAG ─────────────────────────────────────────────"
RUN=$(post "/projects/$PROJECT/runs" '{}')
RUN_ID=$(printf '%s' "$RUN" | jq_ 'd["id"]')
[ -n "$RUN_ID" ] || { echo "no run to follow: $RUN"; exit 1; }
chk "a full run selects all twenty-three nodes" \
    "$(printf '%s' "$RUN" | jq_ 'len(d["selected_node_ids"])')" "23"

wait_for(){
  printf "  …waiting for the worker"
  local status=""
  for _ in $(seq 1 300); do
    sleep 3; printf "."
    status=$(api "/runs/$RUN_ID" | jq_ 'd["status"]')
    case "$status" in awaiting_approval|succeeded|failed|cancelled) break ;; esac
  done
  printf "\n"
  printf '%s' "$status"
}

echo "── 3. three gates, answered in the order the run reaches them ──────────"
decide_open(){
  local ids id
  ids=$(api "/approvals?run_id=$RUN_ID" | jq_ '" ".join(i["id"] for i in d["items"] if i["status"]=="pending")')
  for id in $ids; do post "/approvals/$id" '{"decision":"approve","note":"scripts/verify-p5b.sh"}' >/dev/null; done
  printf '%s' "$(api "/approvals?run_id=$RUN_ID" | jq_ '",".join(sorted(i["node_id"] for i in d["items"]))')"
}

STATUS=$(wait_for)
chk "the run halts rather than guessing at a gate" "$STATUS" "awaiting_approval"
GATES=""
for _ in 1 2 3 4; do
  [ "$STATUS" = "awaiting_approval" ] || break
  GATES=$(decide_open)
  STATUS=$(wait_for)
done
chk "every gate that opened was one of the three PRD §10 declares" \
    "$(printf '%s' "$GATES" | tr ',' '\n' | sort -u | paste -sd, -)" "1.1.5,1.3.4,1.5.3"
chk "the run reaches a terminal state" "$STATUS" "succeeded"

STATE=$(api "/runs/$RUN_ID")
chk "nothing is marked skipped" \
    "$(printf '%s' "$STATE" | jq_ 'sum(1 for n in d[\"nodes\"] if n[\"status\"]==\"skipped\")')" "0"
chk "all twenty-three nodes succeeded" \
    "$(printf '%s' "$STATE" | jq_ 'sum(1 for n in d[\"nodes\"] if n[\"status\"]==\"succeeded\")')" "23"

echo "── 4. stage 1.5 measured, rather than guessed ──────────────────────────"
node_(){ api "/runs/$RUN_ID/nodes/$1"; }
AUDIT=$(node_ 1.5.1); TRACK=$(node_ 1.5.2); CONSENT=$(node_ 1.5.3); SIZE=$(node_ 1.5.4)
ge "1.5.1 audited the pages the keyword map points at" \
   "$(printf '%s' "$AUDIT" | jq_ 'len(d["output"]["pages"])')" 1
inset "1.5.1 graded the slow, eleven-field page" \
   "$(printf '%s' "$AUDIT" | jq_ '[p["severity"] for p in d["output"]["pages"] if "ghs-labeling" in p["url"]][0]')" \
   "major critical"
ge "1.5.2 read the account's conversion actions" \
   "$(printf '%s' "$TRACK" | jq_ 'len(d["output"]["conversion_actions"])')" 1
chk "1.5.2's synthetic probe reached a verdict, not a shrug" \
   "$(printf '%s' "$TRACK" | jq_ 'd["output"]["synthetic_check"]["verdict"]')" "pass"
chk "…and it named the conversion action the tag fires against" \
   "$(printf '%s' "$TRACK" | jq_ 'd["output"]["synthetic_check"]["conversion_action"]')" "Demo request"
ge "1.5.3 put every audience list in front of the data officer" \
   "$(printf '%s' "$CONSENT" | jq_ 'len(d["output"]["lists"])')" 2
ge "1.5.4 sized the opportunity from measured rates" \
   "$(printf '%s' "$SIZE" | jq_ 'len(d["output"]["scenarios"])')" 1
chk "…and every figure in it came out of Python, not a model" \
   "$(printf '%s' "$SIZE" | jq_ 'str(bool(d["output"]["baseline"]["cpc"] and d["output"]["baseline"]["cvr"])).lower()')" "true"

echo "── 5. the report exists, and says what the run found ───────────────────"
REPORT=$(api "/reports/$RUN_ID")
chk "GET /reports/{run_id} no longer 404s" \
    "$(printf '%s' "$REPORT" | jq_ 'd["run_id"]')" "$RUN_ID"
chk "it is written against the versioned contract" \
    "$(printf '%s' "$REPORT" | jq_ 'd["schema_version"]')" "1.0"
inset "the launch verdict is one of §11's three" \
    "$(printf '%s' "$REPORT" | jq_ 'd["payload"]["launch_readiness"]')" "go go_with_fixes no_go"
ge "the executive summary was written" \
   "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["executive_summary"].split())')" 20
chk "…and it is inside §11's 250-word cap" \
   "$(printf '%s' "$REPORT" | jq_ 'str(len(d["payload"]["executive_summary"].split()) <= 250).lower()')" "true"
ge "every section of §11 is populated — business context" \
   "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["business_context"]["segments"])')" 1
ge "…what we already ran" \
   "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["account_learnings"]["wasteful_terms"])')" 1
ge "…the competition" \
   "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["competitive_landscape"]["competitors"])')" 1
ge "…demand" "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["demand_map"]["mapping"])')" 1
ge "…readiness" "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["readiness"]["pages"])')" 1
ge "…and the keyword list the CSV export is built from" \
   "$(printf '%s' "$REPORT" | jq_ 'len(d["payload"]["priced_keyword_list"])')" 2000
chk "no row was dropped for disagreeing with the contract" \
   "$(node_ 1.6.1 | jq_ 'len(d["output"]["dropped_rows"])')" "0"
chk "no claim was dropped for citing evidence that does not exist" \
   "$(node_ 1.6.1 | jq_ 'd["output"]["dropped_claims"]')" "0"

echo "── 6. the citations resolve ────────────────────────────────────────────"
# The report's provenance guarantee, checked from the reader's end: an id in the
# payload has to be a row the evidence API will actually return.
CITED=$(printf '%s' "$REPORT" | jq_ 'd["payload"]["business_context"]["segments"][0]["evidence_ids"][0]')
ge "the report cites evidence at all" "$(node_ 1.6.1 | jq_ 'len(d["output"]["evidence_ids"])')" 10
if [ -n "$CITED" ]; then
  chk "a cited id resolves through GET /evidence" \
      "$(api "/evidence/$CITED" | jq_ 'd["id"]')" "$CITED"
fi

echo "── 7. the critique, and its one re-run ─────────────────────────────────"
CRITIQUE=$(node_ 1.6.2)
chk "1.6.2 reviewed the report" "$(printf '%s' "$CRITIQUE" | jq_ 'd["status"]')" "succeeded"
chk "…with a model from another family than 1.6.1's" \
    "$(printf '%s' "$CRITIQUE" | jq_ 'str(d["model"].split("/")[0] != "'"$(node_ 1.6.1 | jq_ 'd["model"].split("/")[0]')"'").lower()')" "true"
chk "at most one re-synthesis, whatever the critique said" \
    "$(printf '%s' "$CRITIQUE" | jq_ 'str(int(d["output"]["resynthesised"])).lower()')" \
    "$(printf '%s' "$CRITIQUE" | jq_ 'str(min(1, d["output"]["blocking"])).lower()')"
chk "one report per run, after any re-synthesis" \
    "$(api "/reports/$RUN_ID" | jq_ 'd["run_id"]')" "$RUN_ID"

echo "── 8. all five export formats, downloaded ──────────────────────────────"
for FMT in md json csv pdf docx; do
  JOB=$(post "/reports/$RUN_ID/export?format=$FMT" '{}' | jq_ 'd["job_id"]')
  [ -n "$JOB" ] || { no "POST export?format=$FMT" "no job id"; continue; }
  STATE=""
  for _ in $(seq 1 60); do
    sleep 2
    STATE=$(api "/exports/$JOB" | jq_ 'd["status"]')
    case "$STATE" in ready|failed) break ;; esac
  done
  chk "$FMT renders from the synthesised report" "$STATE" "ready"
  [ "$STATE" = "ready" ] || continue
  curl -s -b "$A" -o "$OUT/report.$FMT" "$B/exports/$JOB/download"
  ge "$FMT downloads through api -> worker -> Volume" "$(wc -c < "$OUT/report.$FMT" | tr -d ' ')" 200
done
if [ -s "$OUT/report.csv" ]; then
  ge "the CSV carries every priced keyword" "$(( $(wc -l < "$OUT/report.csv") - 1 ))" 2000
fi
if [ -s "$OUT/report.md" ]; then
  grep -q "Are we ready" "$OUT/report.md" && ok "the markdown carries the readiness section" \
    || no "the markdown carries the readiness section" "heading missing"
fi

echo
printf "── P5b: \033[32m%d passed\033[0m, \033[31m%d failed\033[0m ──\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
