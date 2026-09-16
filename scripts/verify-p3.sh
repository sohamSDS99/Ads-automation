#!/usr/bin/env bash
# P3's exit criteria, run against a live stack.
#
#   "Partial run of stages 1.1+1.2 produces validated outputs, halts on 1.1.5,
#    an `approver` (not an `operator`) can resume it, decision is audit-logged"
#                                                                    — PRD §17
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress and that chain is more than the test
# suite can prove from inside the process.
#
#   make up && ./scripts/verify-p3.sh
#   BASE_URL=http://localhost:3001 ./scripts/verify-p3.sh   # a non-default stack
#
# Needs a real OpenRouter key: this drives eight nodes against a live model, so
# it proves the prompts as well as the plumbing. Set OPENROUTER_API_KEY, or
# store the credential first and the script will use what is already there.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
APPROVER_EMAIL=${APPROVER_EMAIL:-p3-approver@example.com}
OPERATOR_EMAIL=${OPERATOR_EMAIL:-p3-operator@example.com}
MEMBER_PASSWORD=${MEMBER_PASSWORD:-quarry-lantern-98-fog}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); O=$(mktemp); P=$(mktemp); CSV=$(mktemp)
trap 'rm -f "$A" "$O" "$P" "$CSV"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ # jar email password
  local t; t=$(csrf "$1")
  curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login" | jq_ 'd["role"]'
}
psql_(){ $COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c "$1"; }

echo "── 0. sign in as the admin ─────────────────────────────────────────────"
chk "POST /auth/login as admin" "$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD")" "admin"
[ "$PASS" -ge 1 ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. an approver and an operator to test the gate with ────────────────"
invite(){ # email role -> token
  local t; t=$(csrf "$A")
  curl -s -b "$A" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"name\":\"P3 $2\",\"role\":\"$2\"}" "$B/users/invite" \
    | jq_ 'd["link"].rsplit("/",1)[-1]'
}
accept(){ # jar token name
  local t; t=$(csrf "$1")
  curl -s -o /dev/null -w '%{http_code}' -c "$1" -b "$1" -H "X-CSRF-Token: $t" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$3\",\"password\":\"$MEMBER_PASSWORD\"}" "$B/invites/$2/accept"
}
APPROVER_TOKEN=$(invite "$APPROVER_EMAIL" approver)
OPERATOR_TOKEN=$(invite "$OPERATOR_EMAIL" operator)
if [ -n "$APPROVER_TOKEN" ]; then
  chk "an approver accepts their invite" "$(accept "$P" "$APPROVER_TOKEN" Legal)" "200"
else
  chk "an approver signs in" "$(login "$P" "$APPROVER_EMAIL" "$MEMBER_PASSWORD")" "approver"
fi
if [ -n "$OPERATOR_TOKEN" ]; then
  chk "an operator accepts their invite" "$(accept "$O" "$OPERATOR_TOKEN" Ops)" "200"
else
  chk "an operator signs in" "$(login "$O" "$OPERATOR_EMAIL" "$MEMBER_PASSWORD")" "operator"
fi

echo "── 2. a project with CRM evidence ──────────────────────────────────────"
# Project CRUD is P6. Until then the row is created directly, which is also the
# honest thing to do — this script must not depend on a route that does not exist.
PROJECT=$(psql_ "INSERT INTO project (id, workspace_id, created_by, name, domain, product_context, markets)
   SELECT gen_random_uuid(), w.id, u.id, 'P3 verification', 'sdsmanager.com',
          '{\"pitch\": \"safety data sheet management\"}'::jsonb,
          '[{\"country\": \"US\", \"language\": \"en\"}]'::jsonb
   FROM workspace w, \"user\" u WHERE u.role = 'admin' LIMIT 1
   ON CONFLICT DO NOTHING RETURNING id;")
[ -n "$PROJECT" ] || PROJECT=$(psql_ "SELECT id FROM project WHERE name = 'P3 verification' LIMIT 1;")
[ -n "$PROJECT" ] && ok "a project to research ($PROJECT)" || { no "project"; exit 1; }

cat > "$CSV" <<'EOF'
Company,Industry,Country,Employees,Deal value,Close date
Acme Chemicals,Chemicals,US,120,24000,2025-03-04
Borax Supply,Chemicals,US,180,16000,2025-03-19
Rheinwerk GmbH,Manufacturing,DE,640,40000,2025-09-02
Nordmetall AG,Manufacturing,DE,900,20000,2025-10-11
EOF
MAP='{"Company":"account_name","Industry":"industry","Country":"country","Employees":"employee_count","Deal value":"deal_value","Close date":"created_at"}'
T=$(csrf "$A")
UPLOAD=$(curl -s -b "$A" -H "X-CSRF-Token: $T" \
  -F "file=@$CSV" -F "outcome=won" -F "mapping=$MAP" "$B/projects/$PROJECT/sources/csv")
chk "the CRM export is ingested" "$(printf '%s' "$UPLOAD" | jq_ 'd["accepted"]')" "4"

echo "── 3. an operator launches stages 1.1 + 1.2 ────────────────────────────"
NODES='["1.1.1","1.1.2","1.1.3","1.1.4","1.1.5","1.2.1","1.2.2","1.2.3"]'
T=$(csrf "$O")
RUN=$(curl -s -b "$O" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
  -d "{\"mode\":\"partial\",\"node_ids\":$NODES}" "$B/projects/$PROJECT/runs")
RUN_ID=$(printf '%s' "$RUN" | jq_ 'd["id"]')
chk "POST /projects/{id}/runs as an operator -> 8 nodes" \
    "$(printf '%s' "$RUN" | jq_ 'len(d["selected_node_ids"])')" "8"
[ -n "$RUN_ID" ] || { echo "no run to follow"; exit 1; }

printf "  …waiting for the worker (up to 5 min)"
STATUS=""
for _ in $(seq 1 100); do
  sleep 3; printf "."
  STATUS=$(curl -s -b "$A" "$B/runs/$RUN_ID" | jq_ 'd["status"]')
  case "$STATUS" in awaiting_approval|succeeded|failed|cancelled) break ;; esac
done
printf "\n"
chk "the run halts on the gate" "$STATUS" "awaiting_approval"

STATE=$(curl -s -b "$A" "$B/runs/$RUN_ID")
chk "1.1.5 is awaiting approval" \
    "$(printf '%s' "$STATE" | jq_ '[n["status"] for n in d["nodes"] if n["id"]=="1.1.5"][0]')" \
    "awaiting_approval"
chk "every other branch finished" \
    "$(printf '%s' "$STATE" | jq_ 'sum(1 for n in d["nodes"] if n["status"]=="succeeded")')" "7"
chk "nothing downstream is marked skipped" \
    "$(printf '%s' "$STATE" | jq_ 'sum(1 for n in d["nodes"] if n["status"]==\"skipped\")')" "0"

echo "── 4. the outputs are validated, and the numbers are Python's ──────────"
ICP=$(curl -s -b "$A" "$B/runs/$RUN_ID/nodes/1.1.2")
chk "1.1.2 segmented the CRM into two firmographic slices" \
    "$(printf '%s' "$ICP" | jq_ 'len(d["output"]["segments"])')" "2"
chk "…share of revenue is 60/40, computed in pandas" \
    "$(printf '%s' "$ICP" | jq_ '[s["share_of_revenue_pct"] for s in d["output"]["segments"]]')" \
    "[60.0, 40.0]"
chk "…and every segment cites the rows behind it" \
    "$(printf '%s' "$ICP" | jq_ 'str(all(s["evidence_ids"] for s in d["output"]["segments"])).lower()')" \
    "true"
ECON=$(curl -s -b "$A" "$B/runs/$RUN_ID/nodes/1.1.1")
chk "1.1.1 measured ACV from the CRM, not from the model" \
    "$(printf '%s' "$ECON" | jq_ 'd["output"]["economics"]["acv"]')" "25000.0"

echo "── 5. only an approver may decide ──────────────────────────────────────"
INBOX=$(curl -s -b "$P" "$B/approvals?run_id=$RUN_ID&mine=true")
APPROVAL=$(printf '%s' "$INBOX" | jq_ 'd["items"][0]["id"]')
chk "the gate is in the approver's inbox" \
    "$(printf '%s' "$INBOX" | jq_ 'd["items"][0]["node_id"]')" "1.1.5"
chk "…and it says they may decide it" \
    "$(printf '%s' "$INBOX" | jq_ 'str(d["items"][0]["can_decide"]).lower()')" "true"
chk "the operator's inbox is empty" \
    "$(curl -s -b "$O" "$B/approvals?mine=true" | jq_ 'len(d["items"])')" "0"

T=$(csrf "$O")
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$O" -H "X-CSRF-Token: $T" \
  -H 'Content-Type: application/json' -d '{"decision":"approve"}' "$B/approvals/$APPROVAL")
chk "an operator deciding the gate -> 403" "$S" "403"

T=$(csrf "$P")
DECIDED=$(curl -s -b "$P" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
  -d '{"decision":"approve","note":"Checked against our certificates."}' "$B/approvals/$APPROVAL")
chk "an approver deciding the gate -> approved" \
    "$(printf '%s' "$DECIDED" | jq_ 'd["approval"]["status"]')" "approved"
chk "…and the run is re-queued" "$(printf '%s' "$DECIDED" | jq_ 'str(d["resumed"]).lower()')" "true"

printf "  …waiting for the resumed run"
for _ in $(seq 1 60); do
  sleep 3; printf "."
  STATUS=$(curl -s -b "$A" "$B/runs/$RUN_ID" | jq_ 'd["status"]')
  case "$STATUS" in succeeded|failed|cancelled) break ;; esac
done
printf "\n"
chk "the run completes after the decision" "$STATUS" "succeeded"
chk "1.1.5 now holds the approved proposal" \
    "$(curl -s -b "$A" "$B/runs/$RUN_ID/nodes/1.1.5" | jq_ 'd["status"]')" "succeeded"

echo "── 6. the decision is audit-logged with the deciding user ──────────────"
AUDIT=$(curl -s -b "$A" "$B/audit?action=approval.decided")
chk "GET /audit shows the decision" \
    "$(printf '%s' "$AUDIT" | jq_ 'd["items"][0]["action"]')" "approval.decided"
chk "…naming the approver who made it" \
    "$(printf '%s' "$AUDIT" | jq_ 'd["items"][0]["actor_email"]')" "$APPROVER_EMAIL"
chk "…and the gate it decided" \
    "$(printf '%s' "$AUDIT" | jq_ 'd["items"][0]["meta"]["node_id"]')" "1.1.5"
chk "opening the gate is logged too, with no human actor" \
    "$(curl -s -b "$A" "$B/audit?action=approval.requested" | jq_ 'str(d["items"][0]["actor_email"] is None).lower()')" \
    "true"

echo "── 7. authz is server-side ─────────────────────────────────────────────"
D=$(mktemp); trap 'rm -f "$A" "$O" "$P" "$CSV" "$D"' EXIT
chk "GET /approvals without a session -> 401" \
    "$(curl -s -o /dev/null -w '%{http_code}' -c "$D" -b "$D" "$B/approvals")" "401"
T=$(csrf "$P")
chk "deciding an already-decided gate -> 409" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$P" -H "X-CSRF-Token: $T" \
       -H 'Content-Type: application/json' -d '{"decision":"reject"}' "$B/approvals/$APPROVAL")" "409"

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
