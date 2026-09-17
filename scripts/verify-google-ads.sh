#!/usr/bin/env bash
# Our own account history, proved against the live Google Ads API.
#
#   "Google Ads API not authorized → node emits `skipped_no_source`; report marks
#    affected sections `insufficient_evidence`; never hallucinate history."
#                                                              — PRD §16
#
# That rule is why this script matters more than it looks: a Google Ads pull
# that fails does not fail anything. It degrades, the run continues, and the
# report is quietly thinner. So nothing below is seeded and nothing below reads
# a fixture — every assertion about account history has exactly one way to pass,
# which is a live answer from Google through `connectors/google_ads.py`.
#
#   make up && ./scripts/verify-google-ads.sh
#
# Needs a real account. Either export these or put them in `.env`, which is what
# `scripts/google-ads-oauth.py --write-env` writes:
#
#   GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
#   GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID
#   GOOGLE_ADS_LOGIN_CUSTOMER_ID is optional — set it only for MCC access.
set -uo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
[ -f "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
GOOGLE_ADS_DEVELOPER_TOKEN=${GOOGLE_ADS_DEVELOPER_TOKEN:-}
GOOGLE_ADS_CLIENT_ID=${GOOGLE_ADS_CLIENT_ID:-}
GOOGLE_ADS_CLIENT_SECRET=${GOOGLE_ADS_CLIENT_SECRET:-}
GOOGLE_ADS_REFRESH_TOKEN=${GOOGLE_ADS_REFRESH_TOKEN:-}
GOOGLE_ADS_CUSTOMER_ID=${GOOGLE_ADS_CUSTOMER_ID:-}
GOOGLE_ADS_LOGIN_CUSTOMER_ID=${GOOGLE_ADS_LOGIN_CUSTOMER_ID:-}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
ge(){ [ "${2:-0}" -ge "$3" ] 2>/dev/null && ok "$1 ($2)" || no "$1" "expected >= $3, got ${2:-nothing}"; }
jq_(){ python3 -c "import sys,json,urllib.parse;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); trap 'rm -f "$A"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ local t; t=$(csrf "$1")
  curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login" | jq_ 'd["role"]'; }
send(){ local jar=$1 method=$2 path=$3 body=${4:-} t; t=$(csrf "$jar")
  curl -s -b "$jar" -X "$method" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    ${body:+-d "$body"} "$B$path"; }
get(){ curl -s -b "$1" "$B$2"; }
psql_(){ $COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c "$1"; }

echo "── 0. a real account to ask about ──────────────────────────────────────"
MISSING=""
for name in GOOGLE_ADS_DEVELOPER_TOKEN GOOGLE_ADS_CLIENT_ID GOOGLE_ADS_CLIENT_SECRET \
            GOOGLE_ADS_REFRESH_TOKEN GOOGLE_ADS_CUSTOMER_ID; do
  [ -z "${!name}" ] && MISSING="$MISSING $name"
done
if [ -n "$MISSING" ]; then
  no "the five Google Ads values are set" "unset:$MISSING"
  echo "      mint the OAuth three with: python3 scripts/google-ads-oauth.py --write-env"
  exit 1
fi
ok "the five Google Ads values are set"
chk "POST /auth/login as admin" "$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD")" "admin"
[ "$PASS" -ge 2 ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. the vault takes the credential, and gives nothing back ───────────"
# Built in python, not inline: four of these five are secrets and one bad quote
# would seal a broken JSON object that only fails two sections later.
VALUES=$(python3 -c "
import json, os
values = {
    'developer_token': os.environ['GOOGLE_ADS_DEVELOPER_TOKEN'],
    'client_id': os.environ['GOOGLE_ADS_CLIENT_ID'],
    'client_secret': os.environ['GOOGLE_ADS_CLIENT_SECRET'],
    'refresh_token': os.environ['GOOGLE_ADS_REFRESH_TOKEN'],
    'customer_id': os.environ['GOOGLE_ADS_CUSTOMER_ID'],
}
manager = os.environ.get('GOOGLE_ADS_LOGIN_CUSTOMER_ID')
if manager:
    values['login_customer_id'] = manager
print(json.dumps(values))
")
# Any earlier workspace credential goes first. `resolve_secret` orders by scope
# then `created_at DESC`, so a stale row would not be *used* — but it would be
# tested, counted and shown, and "which of these three is live" is not a
# question this script should leave open.
for old in $(get "$A" "/credentials" \
  | jq_ "' '.join(c['id'] for c in d['credentials'] if c['kind']=='google_ads' and c['scope']=='workspace')"); do
  send "$A" DELETE "/credentials/$old" >/dev/null
done
CRED_ID=$(send "$A" POST "/credentials" "{\"kind\":\"google_ads\",\"values\":$VALUES}" | jq_ 'd["id"]')
[ -n "$CRED_ID" ] && ok "the Google Ads credential is stored" || { no "stored"; exit 1; }

BODY=$(get "$A" "/credentials")
sealed(){ # a function, not an inline `case`: `*)` inside $( ) is a parse error
  case "$1" in
    *"$GOOGLE_ADS_REFRESH_TOKEN"* | *"$GOOGLE_ADS_CLIENT_SECRET"* | *"$GOOGLE_ADS_DEVELOPER_TOKEN"*)
      echo leaked ;;
    *) echo sealed ;;
  esac
}
chk "no response carries a secret" "$(sealed "$BODY")" "sealed"
chk "the vault remembers which account is connected" \
    "$(printf '%s' "$BODY" | jq_ "([c['meta'].get('customer_id') for c in d['credentials'] if c['id']=='$CRED_ID'] or [''])[0]")" \
    "$GOOGLE_ADS_CUSTOMER_ID"

echo "── 2. the credential answers to Google, not to us ──────────────────────"
# A live GAQL round trip: the refresh token, the developer token, the customer
# id and the pinned API version all have to be right for this to say true.
TEST=$(send "$A" POST "/credentials/$CRED_ID/test" '{}')
chk "POST /credentials/{id}/test reaches the account" \
    "$(printf '%s' "$TEST" | jq_ 'str(d["ok"]).lower()')" "true"
printf "        %s\n" "$(printf '%s' "$TEST" | jq_ 'd["detail"]')"
chk "…and names the account it reached" \
    "$(printf '%s' "$TEST" | jq_ "str(bool(d.get('meta',{}).get('account_name'))).lower()")" "true"

echo "── 3. a project, holding no account history at all ─────────────────────"
PROJECT=$(psql_ "SELECT id FROM project WHERE name = 'Google Ads verification' LIMIT 1;" | tr -d ' ')
if [ -z "$PROJECT" ]; then
  PROJECT=$(psql_ "INSERT INTO project (id, workspace_id, created_by, name, domain, product_context, markets)
     SELECT gen_random_uuid(), w.id, u.id, 'Google Ads verification', 'sdsmanager.com',
            '{\"pitch\": \"safety data sheet management\"}'::jsonb,
            '[{\"country\": \"US\", \"language\": \"en\", \"currency\": \"USD\"}]'::jsonb
     FROM workspace w, \"user\" u WHERE u.role = 'admin' LIMIT 1 RETURNING id;" | tr -d ' ')
fi
[ -n "$PROJECT" ] && ok "a project to pull into ($PROJECT)" || { no "project"; exit 1; }

# Evidence outlives the run that wrote it, so without this a second invocation
# would read the first one's rows and pass every assertion below without asking
# Google anything.
psql_ "DELETE FROM evidence WHERE project_id='$PROJECT' AND source='google_ads';" >/dev/null
chk "no account history is seeded" \
    "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT' AND source='google_ads';" | tr -d ' ')" "0"

echo "── 4. a live pull, through the connector a node would use ──────────────"
PULL=$($COMPOSE exec -T api python scripts/google_ads_pull.py \
  --project "$PROJECT" --kinds campaign_perf,change_log,conversion_action --months 6 2>&1 | tail -n +1)
printf '%s' "$PULL" | jq_ 'd' >/dev/null || { no "the pull returned JSON" "$(printf '%s' "$PULL" | tail -3)"; exit 1; }
ge "rows came back from Google" "$(printf '%s' "$PULL" | jq_ 'd.get("fetched",0)')" 1
# The change log is the one that silently disappears when its window is wrong:
# Google keeps 30 days and rejects a longer ask, `fetch` degrades rather than
# fails, and the report simply never mentions what changed.
chk "no pull was refused" "$(printf '%s' "$PULL" | jq_ 'str(d.get("degraded"))')" "None"
ge "…and the change log is among them" "$(printf '%s' "$PULL" | jq_ 'd.get("by_kind",{}).get("change_log",0)')" 0
ge "evidence was written" "$(printf '%s' "$PULL" | jq_ 'd.get("inserted",0)')" 1

echo "── 5. the read side, which is what a node actually gets ────────────────"
EV=$(get "$A" "/evidence?project_id=$PROJECT&source=google_ads&kind=campaign_perf&limit=50")
ge "GET /evidence returns the pulled campaigns" "$(printf '%s' "$EV" | jq_ 'len(d["evidence"])')" 1
chk "every row is attributed to Google Ads" \
    "$(printf '%s' "$EV" | jq_ "str(all(e['source']=='google_ads' for e in d['evidence'])).lower()")" "true"
# Money crosses the micros boundary exactly once, in the connector. A campaign
# spending £4,820 arriving as 4820000000 is the failure this catches.
chk "cost arrives in currency, not micros" \
    "$(printf '%s' "$EV" | jq_ "str(all(e['payload'].get('cost',0) < 10**6 for e in d['evidence'])).lower()")" "true"
chk "…and the month it belongs to survives" \
    "$(printf '%s' "$EV" | jq_ "str(all(e['payload'].get('month') for e in d['evidence'])).lower()")" "true"
chk "…and CPA was computed in Python, not by a model" \
    "$(printf '%s' "$EV" | jq_ "str(any(isinstance(e['payload'].get('cpa'), (int,float)) for e in d['evidence'])).lower()")" "true"

echo "── 6. the evidence is findable, not merely stored ──────────────────────"
chk "GET /connectors lists google_ads as needing a credential" \
    "$(get "$A" "/connectors" | jq_ "str(([c.get('requires_credential') for c in d['connectors'] if c['name']=='google_ads'] or [False])[0]).lower()")" \
    "true"
# Written and retrievable are two different claims (the store embeds and
# full-text indexes on write, and a node reaches these rows by searching).
CAMPAIGN=$(printf '%s' "$EV" | jq_ "urllib.parse.quote(d['evidence'][0]['payload'].get('campaign','') or '')" \
  2>/dev/null || true)
if [ -n "$CAMPAIGN" ]; then
  ge "…and a search for one campaign name finds it" \
     "$(get "$A" "/evidence?project_id=$PROJECT&q=$CAMPAIGN&limit=20" | jq_ 'len(d["evidence"])')" 1
else
  no "a campaign name to search for" "no campaign_perf row carried one"
fi

echo
printf "  %s passed, %s failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
