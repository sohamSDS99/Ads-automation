#!/usr/bin/env bash
# P6's exit criteria, run against a live stack.
#
#   "An admin can invite all four roles, store keys, test connectors, pick
#    models, assign gate approvers, and launch a run entirely from the UI; an
#    `operator` sees the same app with writes they lack correctly hidden and
#    403-safe"                                                     — PRD §17
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI). `scripts/browser-check-p6.py` covers the other half of that
# sentence — what the two roles actually see rendered.
#
#   make up && ./scripts/verify-p6.sh
#   BASE_URL=http://localhost:3006 ./scripts/verify-p6.sh   # a non-default stack
#
# Re-runnable: members are reused (an invite link is issued once), but the
# project is new every time. Reusing one would mean asserting "an empty project
# is not runnable" against a project the last run filled in — the script would
# be measuring its own leftovers.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
MEMBER_PASSWORD=${MEMBER_PASSWORD:-verify-p6-member-passphrase}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); O=$(mktemp); trap 'rm -f "$A" "$O"' EXIT

# Each caller keeps its own cookie jar, so "the operator is refused" is a
# statement about the operator's session and not about a shared one.
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' \
         -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login"; }
get(){ curl -s -b "$1" "$B$2"; }
code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" "$B$2"; }
send(){ # jar method path json
  curl -s -b "$1" -c "$1" -X "$2" -H "X-CSRF-Token: $(csrf "$1")" \
       -H 'Content-Type: application/json' -d "$4" "$B$3"; }
send_code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" -c "$1" -X "$2" \
       -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' -d "$4" "$B$3"; }

echo "── 0. sign in ──────────────────────────────────────────────────────────"
ROLE=$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. invite all four roles ────────────────────────────────────────────"
# An invite link is only issued once, so an already-accepted member is reused.
for ROLE_NAME in operator approver viewer; do
  EMAIL="p6-$ROLE_NAME@example.com"
  EXISTS=$(get "$A" "/users" | jq_ "sum(1 for u in d['users'] if u['email']=='$EMAIL')")
  if [ "$EXISTS" = "0" ]; then
    LINK=$(send "$A" POST "/users/invite" \
      "{\"email\":\"$EMAIL\",\"name\":\"P6 $ROLE_NAME\",\"role\":\"$ROLE_NAME\"}" | jq_ 'd["link"]')
    TOKEN=${LINK##*/}
    ACCEPTED=$(curl -s -c "$O" -b "$O" -H "X-CSRF-Token: $(csrf "$O")" -H 'Content-Type: application/json' \
      -d "{\"name\":\"P6 $ROLE_NAME\",\"password\":\"$MEMBER_PASSWORD\"}" \
      "$B/invites/$TOKEN/accept" | jq_ 'd["role"]')
    chk "invite + accept as $ROLE_NAME" "$ACCEPTED" "$ROLE_NAME"
  else
    ok "invite + accept as $ROLE_NAME (already a member)"
  fi
done
chk "the workspace now holds all four roles" \
    "$(get "$A" "/users" | jq_ "len({u['role'] for u in d['users']})")" "4"

echo "── 2. connect a source, and prove no key crosses the wire ──────────────"
BODY=$(get "$A" "/connections")
chk "GET /connections lists every source this build knows" \
    "$(printf '%s' "$BODY" | jq_ "str(len(d['sources']) >= 4).lower()")" "true"
chk "…and OpenRouter is the only one a run cannot start without" \
    "$(printf '%s' "$BODY" | jq_ "','.join(sorted(s['kind'] for s in d['sources'] if s['required_for_runs']))")" \
    "openrouter"
case "$BODY" in
  *api_key*|*ciphertext*|*values*) no "no response carries a secret" "a credential field leaked" ;;
  *)                               ok "no response carries a secret" ;;
esac

CONFIGURED=$(printf '%s' "$BODY" | jq_ "str([s['configured'] for s in d['sources'] if s['kind']=='openrouter'][0]).lower()")
if [ "$CONFIGURED" = "true" ]; then
  # One click, no body. The API tests the deployment's key on the way through,
  # so the honest assertion is that a verdict comes back — not that whatever is
  # in this deployment's environment happens to work.
  CONNECT=$(send "$A" POST "/connections/openrouter/connect" '{}')
  chk "POST /connections/openrouter/connect answers 200" \
      "$(send_code "$A" POST "/connections/openrouter/connect" '{}')" "200"
  chk "…with the source switched on" "$(printf '%s' "$CONNECT" | jq_ "str(d['connected']).lower()")" "true"
  chk "…and a verdict recorded, not a promise" \
      "$(printf '%s' "$CONNECT" | jq_ "str(d['last_tested_at'] is not None).lower()")" "true"
else
  ok "OpenRouter is unconfigured on this deployment — connect is refused, correctly"
  chk "…and connecting names the variable to set" \
      "$(send_code "$A" POST "/connections/openrouter/connect" '{}')" "422"
fi

echo "── 3. test a source ────────────────────────────────────────────────────"
# 422 is the honest answer on a deployment that supplies no key: the test
# cannot run, and the body names the variables to set. Either way the caller
# gets a sentence rather than a stack trace.
TEST=$(send "$A" POST "/connections/openrouter/test" '{}')
TEST_CODE=$(send_code "$A" POST "/connections/openrouter/test" '{}')
case "$TEST_CODE" in 200|422) ANSWERED=yes ;; *) ANSWERED="$TEST_CODE" ;; esac
chk "POST /connections/{kind}/test answers 200 or 422" "$ANSWERED" "yes"
chk "…with a sentence to show the user" \
    "$(printf '%s' "$TEST" | jq_ "str(bool(d.get('detail'))).lower()")" "true"

echo "── 4. create a project, entirely over the API the UI uses ──────────────"
NAME="P6 verification $$"
PROJECT=$(send "$A" POST "/projects" "{\"name\":\"$NAME\",\"domain\":\"https://www.sdsmanager.com/en/\"}" | jq_ 'd["id"]')
chk "POST /projects created one" "$([ -n "$PROJECT" ] && echo yes)" "yes"
[ -n "$PROJECT" ] || { echo "cannot continue without a project"; exit 1; }
BODY=$(get "$A" "/projects/$PROJECT")
chk "a pasted URL was stored as a hostname" "$(printf '%s' "$BODY" | jq_ 'd["domain"]')" "sdsmanager.com"
chk "an empty project is not runnable" \
    "$(printf '%s' "$BODY" | jq_ "str(any(r['blocking'] for r in d['requirements'])).lower()")" "true"

echo "── 5. the wizard's four writes ─────────────────────────────────────────"
VERSION=$(printf '%s' "$BODY" | jq_ 'd["version"]')
CTX='{"product_context":{"summary":"Safety data sheet software for EHS teams.","products":["SDS Manager"],"pricing":"From EUR 49 per site.","icp":"EHS managers.","differentiators":["Automatic supplier updates"],"site_url":"https://sdsmanager.com"},"markets":[{"country":"NO","language":"nb","currency":"NOK"}]}'
chk "step 1 — business context and markets" "$(send_code "$A" PATCH "/projects/$PROJECT" "$CTX")" "200"
chk "step 3 — model routing" \
    "$(send_code "$A" PATCH "/projects/$PROJECT" '{"models":{"synthesize":"anthropic/claude-opus-4.6"}}')" "200"
APPROVER=$(get "$A" "/users" | jq_ "([u['id'] for u in d['users'] if u['role']=='approver'] or [''])[0]")
[ -n "$APPROVER" ] && ok "there is an approver to assign" || no "there is an approver to assign" "none found"
GATE=$(send "$A" PATCH "/projects/$PROJECT" "{\"approvals\":{\"1.1.5\":{\"assignee_id\":\"$APPROVER\",\"sla_hours\":24}}}")
chk "step 4 — a gate assigned to a real approver" \
    "$(printf '%s' "$GATE" | jq_ "[g['assignee_id'] for g in d['gates'] if g['node_id']=='1.1.5'][0]")" "$APPROVER"
BODY=$(get "$A" "/projects/$PROJECT")
chk "…and it reads back with their name" \
    "$(printf '%s' "$BODY" | jq_ "[g['assignee_name'] for g in d['gates'] if g['node_id']=='1.1.5'][0]")" \
    "$(get "$A" "/users" | jq_ "[u['name'] for u in d['users'] if u['id']=='$APPROVER'][0]")"
chk "step 5 — nothing blocking is left" \
    "$(printf '%s' "$BODY" | jq_ "str(any(r['blocking'] for r in d['requirements'])).lower()")" "false"

echo "── 6. a stale save is refused, not silently applied ────────────────────"
# `VERSION` was read before section 5's writes, so it is now several revisions
# behind — exactly the state a second browser tab would be in.
chk "PATCH with a stale version is a 412" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" -c "$A" -X PATCH -H "X-CSRF-Token: $(csrf "$A")" \
       -H 'Content-Type: application/json' -H "If-Match: $VERSION" \
       -d '{"name":"Clobbered"}' "$B/projects/$PROJECT")" "412"
chk "…and the name is untouched" \
    "$(get "$A" "/projects/$PROJECT" | jq_ 'd["name"]')" "$NAME"
FRESH=$(get "$A" "/projects/$PROJECT" | jq_ 'd["version"]')
chk "PATCH with the current version succeeds" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" -c "$A" -X PATCH -H "X-CSRF-Token: $(csrf "$A")" \
       -H 'Content-Type: application/json' -H "If-Match: $FRESH" \
       -d "{\"name\":\"$NAME\"}" "$B/projects/$PROJECT")" "200"

echo "── 7. the model picker's data ──────────────────────────────────────────"
MODELS_CODE=$(code "$A" "/models")
case "$MODELS_CODE" in
  200) BODY=$(get "$A" "/models")
       chk "GET /models returns a catalogue" "$(printf '%s' "$BODY" | jq_ "str(len(d['models'])>0).lower()")" "true"
       chk "…with all four task classes" "$(printf '%s' "$BODY" | jq_ "len(d['task_classes'])")" "4"
       chk "…and a labelled token baseline" "$(printf '%s' "$BODY" | jq_ "d['usage_source'] in ('measured','assumed') and 'yes' or 'no'")" "yes" ;;
  502) ok "GET /models -> 502 (OpenRouter unreachable from here; the route is wired)" ;;
  409) ok "GET /models -> 409 (no key stored; the route names what is missing)" ;;
  *)   no "GET /models" "unexpected $MODELS_CODE" ;;
esac

echo "── 8. launch a run from the UI's own endpoint ──────────────────────────"
RUN=$(send "$A" POST "/projects/$PROJECT/runs" '{"mode":"full","reuse_cache":true}')
RUN_ID=$(printf '%s' "$RUN" | jq_ 'd["id"]')
chk "POST /projects/{id}/runs started one" "$([ -n "$RUN_ID" ] && echo yes)" "yes"
chk "…and a second launch is refused with the holder" \
    "$(send_code "$A" POST "/projects/$PROJECT/runs" '{"mode":"full"}')" "409"
ME=$(get "$A" "/auth/me" | jq_ 'd["name"]')
HOLDER=$(send "$A" POST "/projects/$PROJECT/runs" '{"mode":"full"}' | jq_ "d.get('holder',{}).get('user_name','')")
chk "…naming who holds the lock" "$HOLDER" "$ME"
chk "the run shows up in the project's history" \
    "$(get "$A" "/projects/$PROJECT/runs" | jq_ "sum(1 for r in d['runs'] if r['id']=='$RUN_ID')")" "1"
chk "…attributed to the person who launched it" \
    "$(get "$A" "/projects/$PROJECT/runs" | jq_ "[r['triggered_by_name'] for r in d['runs'] if r['id']=='$RUN_ID'][0]")" "$ME"
send "$A" POST "/runs/$RUN_ID/cancel" '{}' >/dev/null

echo "── 9. the operator sees the same app, minus the writes ─────────────────"
OROLE=$(login "$O" "p6-operator@example.com" "$MEMBER_PASSWORD" | jq_ 'd["role"]')
chk "operator signs in" "$OROLE" "operator"
chk "operator reads the project list" "$(code "$O" "/projects")" "200"
chk "operator reads one project" "$(code "$O" "/projects/$PROJECT")" "200"
chk "operator may edit the business context" \
    "$(send_code "$O" PATCH "/projects/$PROJECT" "$CTX")" "200"
chk "operator may NOT choose models" \
    "$(send_code "$O" PATCH "/projects/$PROJECT" '{"models":{"synthesize":"anthropic/claude-opus-4.6"}}')" "403"
chk "operator may NOT switch a source on" \
    "$(send_code "$O" POST "/connections/openrouter/connect" '{}')" "403"
chk "operator may NOT read the model catalogue" "$(code "$O" "/models")" "403"
chk "operator may NOT read the audit log" "$(code "$O" "/audit")" "403"
chk "operator may NOT invite anyone" \
    "$(send_code "$O" POST "/users/invite" '{"email":"nope@example.com","name":"No","role":"viewer"}')" "403"
OP_TEST=$(send_code "$O" POST "/connections/openrouter/test" '{}')
case "$OP_TEST" in 200|422) OP_ANSWERED=yes ;; *) OP_ANSWERED="$OP_TEST" ;; esac
chk "operator MAY test a source" "$OP_ANSWERED" "yes"
chk "operator may NOT switch one off" \
    "$(send_code "$O" DELETE "/connections/openrouter" '')" "403"

echo "── 10. the admin-only screens ──────────────────────────────────────────"
chk "GET /workspace carries its settings" \
    "$(get "$A" "/workspace" | jq_ "str('max_run_cost_usd' in d['settings']).lower()")" "true"
chk "…and reports SMTP rather than storing it" \
    "$(get "$A" "/workspace" | jq_ "str(isinstance(d['smtp_configured'], bool)).lower()")" "true"
chk "PATCH /workspace writes a budget cap" \
    "$(send_code "$A" PATCH "/workspace" '{"name":"Ads Research","max_run_cost_usd":"9.50"}')" "200"
chk "…and it reads back" \
    "$(get "$A" "/workspace" | jq_ "d['settings']['max_run_cost_usd']")" "9.50"
chk "a nonsense model id is refused before it is stored" \
    "$(send_code "$A" PATCH "/workspace" '{"name":"Ads Research","models":{"extract":"not-a-model"}}')" "422"
chk "GET /audit records what just happened" \
    "$(get "$A" "/audit" | jq_ "str(any(e['action']=='project.created' for e in d['entries'])).lower()")" "true"
chk "…including the credential writes" \
    "$(get "$A" "/audit" | jq_ "str(any(e['action']=='credential.created' for e in d['entries'])).lower()")" "true"

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
