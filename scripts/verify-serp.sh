#!/usr/bin/env bash
# The SERP source and the OpenRouter key, proved against a live stack.
#
#   "All connectors implement BaseConnector.fetch(params)… a node never sources
#    a fact"                                              — PRD §9, §18 law 1
#
# What this script is for: nothing below is seeded. The competitive evidence
# node 1.3.1 reads has to arrive through `connectors/serp.py`, out of a real
# Google result page, fetched through the Bright Data proxy the credential in
# the vault addresses. If the proxy, the credential or the parse is wrong, the
# assertions on SERP evidence fail rather than quietly reading a fixture.
#
#   make up && ./scripts/verify-serp.sh
#
# Needs both live accounts. Either set them in the environment, or put them in
# `.env` and this script will read them from there:
#
#   OPENROUTER_API_KEY, BRIGHTDATA_API_KEY
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress.
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
APPROVER_EMAIL=${APPROVER_EMAIL:-serp-approver@example.com}
MEMBER_PASSWORD=${MEMBER_PASSWORD:-quarry-lantern-98-fog}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
BRIGHTDATA_API_KEY=${BRIGHTDATA_API_KEY:-}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
ge(){ [ "${2:-0}" -ge "$3" ] 2>/dev/null && ok "$1 ($2)" || no "$1" "expected >= $3, got ${2:-nothing}"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); P=$(mktemp); CSV=$(mktemp)
trap 'rm -f "$A" "$P" "$CSV"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ local t; t=$(csrf "$1")
  curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login" | jq_ 'd["role"]'; }
send(){ local jar=$1 method=$2 path=$3 body=${4:-} t; t=$(csrf "$jar")
  curl -s -b "$jar" -X "$method" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    ${body:+-d "$body"} "$B$path"; }
get(){ curl -s -b "$1" "$B$2"; }
psql_(){ $COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c "$1"; }

echo "── 0. both accounts are present ────────────────────────────────────────"
[ -n "$OPENROUTER_API_KEY" ] && ok "an OpenRouter key to store" \
  || { no "OPENROUTER_API_KEY is unset"; exit 1; }
[ -n "$BRIGHTDATA_API_KEY" ] \
  && ok "a Bright Data SERP account to store" \
  || { no "BRIGHTDATA_API_KEY is unset"; exit 1; }
chk "POST /auth/login as admin" "$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD")" "admin"
[ "$PASS" -ge 3 ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. the vault takes both keys, and proves each one upstream ──────────"
store(){ # kind, values-json -> id
  local id
  id=$(get "$A" "/credentials" | jq_ "([c['id'] for c in d['credentials'] if c['kind']=='$1' and c['scope']=='workspace'] or [''])[0]")
  [ -n "$id" ] && { printf '%s' "$id"; return; }
  send "$A" POST "/credentials" "{\"kind\":\"$1\",\"values\":$2}" | jq_ 'd["id"]'
}
KEY_ID=$(store openrouter "{\"api_key\":\"$OPENROUTER_API_KEY\"}")
BRD_ID=$(store brightdata "{\"api_key\":\"$BRIGHTDATA_API_KEY\"}")
[ -n "$KEY_ID" ] && ok "the OpenRouter key is stored" || no "the OpenRouter key is stored"
[ -n "$BRD_ID" ] && ok "the Bright Data account is stored" || no "the Bright Data account is stored"

BODY=$(get "$A" "/credentials")
sealed(){ # a function, not an inline `case`: `*)` inside $( ) is a parse error
  case "$1" in
    *"$OPENROUTER_API_KEY"* | *"$BRIGHTDATA_API_KEY"*) echo leaked ;;
    *) echo sealed ;;
  esac
}
chk "no response carries either secret" "$(sealed "$BODY")" "sealed"
chk "the vault shows the key's last 4 and nothing more" \
    "$(printf '%s' "$BODY" | jq_ "([c['meta'].get('last4') for c in d['credentials'] if c['id']=='$BRD_ID'] or [''])[0]")" \
    "${BRIGHTDATA_API_KEY: -4}"

# Both tests are live calls, not shape checks: the first spends an OpenRouter
# round-trip, the second buys one result page from Bright Data.
OR_TEST=$(send "$A" POST "/credentials/$KEY_ID/test" '{}')
chk "the OpenRouter key answers to OpenRouter" "$(printf '%s' "$OR_TEST" | jq_ 'str(d["ok"]).lower()')" "true"
printf "        %s\n" "$(printf '%s' "$OR_TEST" | jq_ 'd["detail"]')"

BRD_TEST=$(send "$A" POST "/credentials/$BRD_ID/test" '{}')
chk "the SERP proxy answers with a parsed result page" \
    "$(printf '%s' "$BRD_TEST" | jq_ 'str(d["ok"]).lower()')" "true"
printf "        %s\n" "$(printf '%s' "$BRD_TEST" | jq_ 'd["detail"]')"
ge "…carrying organic results" "$(printf '%s' "$BRD_TEST" | jq_ 'd["meta"].get("probe_organic", 0)')" 1
chk "…from the engine we asked for" \
    "$(printf '%s' "$BRD_TEST" | jq_ 'd["meta"].get("search_engine")')" "google"

echo "── 2. a project, and the history a SERP probe is aimed by ──────────────"
PROJECT=$(psql_ "SELECT id FROM project WHERE name = 'SERP verification' LIMIT 1;")
if [ -z "$PROJECT" ]; then
  PROJECT=$(psql_ "INSERT INTO project (id, workspace_id, created_by, name, domain, product_context, markets)
     SELECT gen_random_uuid(), w.id, u.id, 'SERP verification', 'sdsmanager.com',
            '{\"pitch\": \"safety data sheet management\"}'::jsonb,
            '[{\"country\": \"US\", \"language\": \"en\", \"currency\": \"USD\"}]'::jsonb
     FROM workspace w, \"user\" u WHERE u.role = 'admin' LIMIT 1 RETURNING id;")
fi
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
# `rows_accepted`, not `accepted` — the ingest result's field name and the
# response model's are different words for the same number.
chk "the CRM export is ingested" "$(printf '%s' "$UPLOAD" | jq_ 'd["rows_accepted"]')" "4"

# Our own Google Ads history. Node 1.2.2 prices these, and node 1.3.1 aims the
# SERP probe at the terms it priced — so these are the real market phrases the
# proxy will be asked about, not a made-up string.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'google_ads', 'search_term_pnl', p.payload,
        md5('serpverify:stp:' || p.payload::text)
 FROM (VALUES
   ('{\"search_term\":\"sds management software\",\"campaign\":\"Brand\",\"month\":\"2025-01\",\"cost\":90,\"conversions\":3,\"conversion_value\":1800,\"clicks\":40,\"impressions\":500}'::jsonb),
   ('{\"search_term\":\"chemical inventory software\",\"campaign\":\"Generic\",\"month\":\"2025-02\",\"cost\":420,\"conversions\":2,\"conversion_value\":2600,\"clicks\":190,\"impressions\":6100}'::jsonb),
   ('{\"search_term\":\"safety data sheet software\",\"campaign\":\"Generic\",\"month\":\"2025-03\",\"cost\":310,\"conversions\":1,\"conversion_value\":900,\"clicks\":150,\"impressions\":5200}'::jsonb)
 ) AS p(payload) ON CONFLICT DO NOTHING;" >/dev/null

# Deliberately absent: `serp_snapshot`, `serp_ad`, `domain_competitor`. Every
# assertion below about competitive evidence has exactly one way to pass.
#
# The delete is what makes this script re-runnable as a *proof* rather than as a
# recital. Evidence outlives the run that wrote it (PRD §6), so a second
# invocation would read the first one's rows out of the store, skip the pull
# entirely, and pass every assertion below without touching the proxy.
psql_ "DELETE FROM evidence WHERE project_id='$PROJECT'
       AND kind IN ('serp_snapshot','serp_ad','serp_question','serp_related','domain_competitor');" >/dev/null
SEEDED=$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT'
                AND kind IN ('serp_snapshot','serp_ad','serp_question','serp_related','domain_competitor');")
chk "no competitive evidence is seeded" "$SEEDED" "0"

echo "── 3. a run that can only get its competitors off a live SERP ──────────"
APPROVER_TOKEN=$(send "$A" POST "/users/invite" \
  "{\"email\":\"$APPROVER_EMAIL\",\"name\":\"SERP approver\",\"role\":\"approver\"}" \
  | jq_ 'd["link"].rsplit("/",1)[-1]')
if [ -n "$APPROVER_TOKEN" ]; then
  T=$(csrf "$P")
  curl -s -o /dev/null -c "$P" -b "$P" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
    -d "{\"name\":\"Marketing\",\"password\":\"$MEMBER_PASSWORD\"}" "$B/invites/$APPROVER_TOKEN/accept"
else
  login "$P" "$APPROVER_EMAIL" "$MEMBER_PASSWORD" >/dev/null
fi

RUN=$(send "$A" POST "/projects/$PROJECT/runs" '{"mode":"partial","node_ids":["1.3.1","1.3.2","1.4.1"]}')
RUN_ID=$(printf '%s' "$RUN" | jq_ 'd["id"]')
[ -n "$RUN_ID" ] && ok "a partial run is queued ($RUN_ID)" || { no "no run to follow"; exit 1; }

wait_for(){
  # Progress goes to stderr. It is read by a person; stdout is read by `$( )`,
  # and a status with forty dots in front of it matches nothing.
  printf "  …waiting for the worker" >&2
  local status=""
  for _ in $(seq 1 200); do
    sleep 3; printf "." >&2
    status=$(get "$A" "/runs/$RUN_ID" | jq_ 'd["status"]')
    case "$status" in awaiting_approval|succeeded|failed|cancelled) break ;; esac
  done
  printf "\n" >&2; printf '%s' "$status"
}
decide(){ send "$P" POST "/approvals/$1" \
  '{"decision":"approve","note":"Verified by scripts/verify-serp.sh"}'; }

STATUS=$(wait_for)
while [ "$STATUS" = "awaiting_approval" ]; do
  PENDING=$(get "$P" "/approvals?run_id=$RUN_ID&mine=true" | jq_ 'd["items"][0]["id"]')
  [ -n "$PENDING" ] || { no "the run is waiting on a gate nobody can answer"; break; }
  decide "$PENDING" >/dev/null
  STATUS=$(wait_for)
done
chk "the run completes" "$STATUS" "succeeded"

echo "── 4. the evidence came off a real result page ─────────────────────────"
count(){ psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT' AND source='serp' AND kind='$1';"; }
ge "serp_snapshot rows written by the proxy" "$(count serp_snapshot)" 1
ge "serp_question rows — People Also Ask" "$(count serp_question)" 1
ge "serp_related rows — related searches" "$(count serp_related)" 1

# Whether Google serves ads on a given term at a given minute is Google's call,
# not ours — one invocation of this script saw two, the next saw none. So the
# assertion is conditional on what was actually on the page: `ad_domains` is
# taken off the same response as the ads, so a snapshot naming an advertiser and
# no `serp_ad` row beside it is a parser regression, and an empty page is not.
ADS_ON_PAGE=$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT'
                     AND source='serp' AND kind='serp_snapshot'
                     AND jsonb_array_length(payload->'ad_domains') > 0;")
if [ "${ADS_ON_PAGE:-0}" -gt 0 ]; then
  ge "serp_ad rows — live competitor copy" "$(count serp_ad)" 1
else
  ok "no ads were served on any probe SERP, so there are none to store"
fi

chk "every SERP row is stamped with the serp source" \
    "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT'
              AND kind LIKE 'serp\\_%' AND source <> 'serp';")" "0"
chk "no favicon data URI reached the store" \
    "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT'
              AND source='serp' AND payload::text LIKE '%data:image%';")" "0"
chk "an organic result is keyed by hostname, not by display name" \
    "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT' AND kind='serp_snapshot'
              AND payload::text LIKE '%\"domain\": \"%.%\"%';")" \
    "$(count serp_snapshot)"

echo "── 5. the nodes read it ────────────────────────────────────────────────"
node_(){ get "$A" "/runs/$RUN_ID/nodes/$1"; }
SET=$(node_ 1.3.1)
ge "1.3.1 found competitors" "$(printf '%s' "$SET" | jq_ 'len(d["output"]["competitors"])')" 1
ge "…against a stated number of SERPs checked" \
   "$(printf '%s' "$SET" | jq_ 'd["output"]["serp_terms_checked"]')" 1
chk "…and the SERP is what put them there" \
    "$(printf '%s' "$SET" | jq_ 'str(any("serp" in c["overlap_basis"] for c in d["output"]["competitors"])).lower()')" \
    "true"
chk "…and we are not our own competitor" \
    "$(printf '%s' "$SET" | jq_ 'str("sdsmanager.com" not in [c["domain"] for c in d["output"]["competitors"]]).lower()')" \
    "true"

CORPUS=$(node_ 1.3.2)
if [ "${ADS_ON_PAGE:-0}" -gt 0 ]; then
  ge "1.3.2 read the ads off the SERP" "$(printf '%s' "$CORPUS" | jq_ 'len(d["output"]["ads"])')" 1
  chk "…every one of them quoting real copy" \
      "$(printf '%s' "$CORPUS" | jq_ 'str(all(a["headline"] or a["description"] for a in d["output"]["ads"])).lower()')" \
      "true"
else
  chk "1.3.2 says it had no creative to read rather than inventing some" \
      "$(printf '%s' "$CORPUS" | jq_ 'len(d["output"]["ads"])')" "0"
fi

UNIVERSE=$(node_ 1.4.1)
chk "1.4.1 seeds on what Google associates with the term" \
    "$(printf '%s' "$UNIVERSE" | jq_ 'str(bool({"serp_related","serp_questions"} & set(d["output"]["source_counts"]))).lower()')" \
    "true"
printf "        sources: %s\n" "$(printf '%s' "$UNIVERSE" | jq_ 'd["output"]["source_counts"]')"

echo "── 6. the OpenRouter key did the thinking ──────────────────────────────"
ge "nodes billed to a real model" \
   "$(psql_ "SELECT count(*) FROM node_run WHERE run_id='$RUN_ID' AND cost_usd > 0;")" 1
printf "        models: %s\n" \
  "$(psql_ "SELECT string_agg(DISTINCT model, ', ') FROM node_run WHERE run_id='$RUN_ID' AND model IS NOT NULL;")"
printf "        run cost: \$%s over %s in / %s out tokens\n" \
  "$(psql_ "SELECT COALESCE(sum(cost_usd),0) FROM node_run WHERE run_id='$RUN_ID';")" \
  "$(psql_ "SELECT COALESCE(sum(token_in),0) FROM node_run WHERE run_id='$RUN_ID';")" \
  "$(psql_ "SELECT COALESCE(sum(token_out),0) FROM node_run WHERE run_id='$RUN_ID';")"

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
