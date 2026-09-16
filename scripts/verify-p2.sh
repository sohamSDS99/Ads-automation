#!/usr/bin/env bash
# P2's exit criteria, run against a live stack.
#
#   "Each connector writes deduped Evidence rows from a live or cassette fetch;
#    GET /evidence filters and searches"                          — PRD §17
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress and that chain is more than the test
# suite can prove from inside the process.
#
#   make up && ./scripts/verify-p2.sh
#   BASE_URL=http://localhost:3001 ./scripts/verify-p2.sh   # a non-default stack
#
# Idempotent: the project it needs is created once and reused.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

C=$(mktemp); CSV=$(mktemp); trap 'rm -f "$C" "$CSV"' EXIT
csrf(){ curl -s -c "$C" -b "$C" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$C" | awk '{print $7}'; }

echo "── 0. sign in ──────────────────────────────────────────────────────────"
T=$(csrf)
ROLE=$(curl -s -c "$C" -b "$C" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
      -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" "$B/auth/login" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. the connector catalogue ──────────────────────────────────────────"
BODY=$(curl -s -b "$C" "$B/connectors")
chk "GET /connectors -> all five" "$(printf '%s' "$BODY" | jq_ 'len(d["connectors"])')" "5"
chk "only two need a credential" \
    "$(printf '%s' "$BODY" | jq_ 'sum(1 for c in d["connectors"] if c["requires_credential"])')" "2"

echo "── 2. a project to hang evidence off ───────────────────────────────────"
# Project CRUD is P6. Until then the row is created directly, which is also the
# honest thing to do — this script must not depend on a route that does not exist.
PROJECT=$($COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c \
  "INSERT INTO project (id, workspace_id, created_by, name, domain)
   SELECT gen_random_uuid(), w.id, u.id, 'P2 verification', 'sdsmanager.com'
   FROM workspace w, \"user\" u WHERE u.role = 'admin' LIMIT 1
   ON CONFLICT DO NOTHING
   RETURNING id;" 2>/dev/null | tr -d '[:space:]')
if [ -z "$PROJECT" ]; then
  PROJECT=$($COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c \
    "SELECT id FROM project WHERE name = 'P2 verification' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')
fi
[ -n "$PROJECT" ] && ok "project $PROJECT" || { no "could not create a project"; exit 1; }

echo "── 3. CSV column mapping ───────────────────────────────────────────────"
printf 'Company,Industry,Deal Amount,Close Date\nAcme Ltd,Manufacturing,12000.50,2025-03-01\nBeta Inc,Chemicals,8400,2025-04-15\n' > "$CSV"
T=$(csrf)
BODY=$(curl -s -b "$C" -H "X-CSRF-Token: $T" -F "file=@$CSV;filename=won.csv;type=text/csv" \
       "$B/projects/$PROJECT/sources/csv/preview")
chk "preview proposes account_name for 'Company'" \
    "$(printf '%s' "$BODY" | jq_ '[c["suggested_field"] for c in d["columns"] if c["header"]=="Company"][0]')" "account_name"
chk "preview counts the data rows" "$(printf '%s' "$BODY" | jq_ 'd["row_count"]')" "2"

echo "── 4. the upload writes deduped evidence ───────────────────────────────"
MAP='{"Company":"account_name","Industry":"industry","Deal Amount":"deal_value","Close Date":"created_at"}'
post_csv(){ T=$(csrf); curl -s -b "$C" -H "X-CSRF-Token: $T" \
  -F "file=@$CSV;filename=won.csv;type=text/csv" -F "outcome=won" -F "mapping=$MAP" \
  "$B/projects/$PROJECT/sources/csv"; }
FIRST=$(post_csv)
chk "first upload accepts both rows" "$(printf '%s' "$FIRST" | jq_ 'd["rows_accepted"]')" "2"
SECOND=$(post_csv)
chk "re-uploading writes nothing new" "$(printf '%s' "$SECOND" | jq_ 'd["evidence_written"]')" "0"
chk "…and reports them as duplicates" "$(printf '%s' "$SECOND" | jq_ 'd["duplicates"]')" "2"

echo "── 5. GET /evidence filters ────────────────────────────────────────────"
chk "filter by source=csv" \
    "$(curl -s -b "$C" "$B/evidence?project_id=$PROJECT&source=csv" | jq_ 'len(d["items"])')" "2"
chk "filter by kind=crm_won" \
    "$(curl -s -b "$C" "$B/evidence?project_id=$PROJECT&kind=crm_won" | jq_ 'len(d["items"])')" "2"
chk "a kind that holds nothing returns nothing" \
    "$(curl -s -b "$C" "$B/evidence?project_id=$PROJECT&kind=keyword_metrics" | jq_ 'len(d["items"])')" "0"

echo "── 6. GET /evidence searches ───────────────────────────────────────────"
BODY=$(curl -s -b "$C" "$B/evidence?project_id=$PROJECT&q=Acme")
chk "a query returns a ranked page" "$(printf '%s' "$BODY" | jq_ 'str(d["ranked"]).lower()')" "true"
chk "…and finds the uploaded row" "$(printf '%s' "$BODY" | jq_ 'd["items"][0]["kind"]')" "crm_won"
MATCHED=$(printf '%s' "$BODY" | jq_ 'd["items"][0]["matched_by"]')
case "$MATCHED" in
  vector|text|both) ok "…and says which retriever matched ($MATCHED)" ;;
  *) no "matched_by" "expected vector|text|both, got $MATCHED" ;;
esac

echo "── 7. authz is server-side ─────────────────────────────────────────────"
D=$(mktemp); trap 'rm -f "$C" "$CSV" "$D"' EXIT
S=$(curl -s -o /dev/null -w '%{http_code}' -c "$D" -b "$D" "$B/evidence")
chk "GET /evidence without a session -> 401" "$S" "401"
T=$(csrf)
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$C" -H "X-CSRF-Token: $T" \
      -F "file=@$CSV" -F "outcome=won" -F "mapping=$MAP" \
      "$B/projects/00000000-0000-0000-0000-000000000000/sources/csv")
chk "uploading to an unknown project -> 404" "$S" "404"

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
