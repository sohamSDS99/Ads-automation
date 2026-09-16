#!/usr/bin/env bash
# P4's exit criteria, run against a live stack.
#
#   "Partial run produces ≥100 competitor creatives and ≥2,000 classified,
#    priced keywords with page mapping"                            — PRD §17
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI), because `api` has no ingress.
#
#   make up && ./scripts/verify-p4.sh
#   COMPOSE_CMD="docker compose -p ads-research-agent-p4" ./scripts/verify-p4.sh
#
# Needs a real OpenRouter key: this drives fourteen nodes and ~25 batched
# classification calls against a live model, so it proves the prompts as well as
# the plumbing.
#
# Selecting the nine P4 nodes widens to fourteen — 1.3.4 sits behind the legal
# gate 1.1.5 — so the run stops twice and this script answers both gates.
#
# There is no Chromium and no DataForSEO login in a default local stack, and a
# connector that cannot reach its source degrades to empty (PRD §16). The
# competitive and demand evidence is therefore written straight into `evidence`,
# which is also what a previous run's pull would have left behind. `hash` only
# has to be unique per project for the dedupe constraint, so a digest of the
# payload stands in for the real content hash here.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
APPROVER_EMAIL=${APPROVER_EMAIL:-p4-approver@example.com}
OPERATOR_EMAIL=${OPERATOR_EMAIL:-p4-operator@example.com}
MEMBER_PASSWORD=${MEMBER_PASSWORD:-quarry-lantern-98-fog}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
ge(){ [ "${2:-0}" -ge "$3" ] 2>/dev/null && ok "$1 ($2)" || no "$1" "expected >= $3, got ${2:-nothing}"; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); O=$(mktemp); P=$(mktemp); CSV=$(mktemp)
trap 'rm -f "$A" "$O" "$P" "$CSV"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ local t; t=$(csrf "$1")
  curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login" | jq_ 'd["role"]'; }
psql_(){ $COMPOSE exec -T postgres psql -qtAX -U agent -d agent -c "$1"; }

echo "── 0. sign in as the admin ─────────────────────────────────────────────"
chk "POST /auth/login as admin" "$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD")" "admin"
[ "$PASS" -ge 1 ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. an approver and an operator ──────────────────────────────────────"
invite(){ local t; t=$(csrf "$A")
  curl -s -b "$A" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"email\":\"$1\",\"name\":\"P4 $2\",\"role\":\"$2\"}" "$B/users/invite" \
    | jq_ 'd["link"].rsplit("/",1)[-1]'; }
accept(){ local t; t=$(csrf "$1")
  curl -s -o /dev/null -w '%{http_code}' -c "$1" -b "$1" -H "X-CSRF-Token: $t" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$3\",\"password\":\"$MEMBER_PASSWORD\"}" "$B/invites/$2/accept"; }
APPROVER_TOKEN=$(invite "$APPROVER_EMAIL" approver)
OPERATOR_TOKEN=$(invite "$OPERATOR_EMAIL" operator)
if [ -n "$APPROVER_TOKEN" ]; then
  chk "an approver accepts their invite" "$(accept "$P" "$APPROVER_TOKEN" Marketing)" "200"
else
  chk "an approver signs in" "$(login "$P" "$APPROVER_EMAIL" "$MEMBER_PASSWORD")" "approver"
fi
if [ -n "$OPERATOR_TOKEN" ]; then
  chk "an operator accepts their invite" "$(accept "$O" "$OPERATOR_TOKEN" Ops)" "200"
else
  chk "an operator signs in" "$(login "$O" "$OPERATOR_EMAIL" "$MEMBER_PASSWORD")" "operator"
fi

echo "── 2. a project, its CRM export and the evidence 1.3/1.4 read ──────────"
PROJECT=$(psql_ "INSERT INTO project (id, workspace_id, created_by, name, domain, product_context, markets)
   SELECT gen_random_uuid(), w.id, u.id, 'P4 verification', 'sdsmanager.com',
          '{\"pitch\": \"safety data sheet management\"}'::jsonb,
          '[{\"country\": \"US\", \"language\": \"en\", \"currency\": \"USD\"}]'::jsonb
   FROM workspace w, \"user\" u WHERE u.role = 'admin' LIMIT 1
   ON CONFLICT DO NOTHING RETURNING id;")
[ -n "$PROJECT" ] || PROJECT=$(psql_ "SELECT id FROM project WHERE name = 'P4 verification' LIMIT 1;")
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

# Our own Google Ads history: two campaigns and three search terms, one of
# which converts. 1.4.4 must refuse to block that one.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'google_ads', 'search_term_pnl', p.payload,
        md5('stp:' || p.payload::text)
 FROM (VALUES
   ('{\"search_term\":\"sds management software\",\"campaign\":\"Brand\",\"month\":\"2025-01\",\"cost\":90,\"conversions\":3,\"conversion_value\":1800,\"clicks\":40,\"impressions\":500}'::jsonb),
   ('{\"search_term\":\"free sds template\",\"campaign\":\"Generic\",\"month\":\"2025-01\",\"cost\":260,\"conversions\":0,\"conversion_value\":0,\"clicks\":200,\"impressions\":7000}'::jsonb),
   ('{\"search_term\":\"sds jobs\",\"campaign\":\"Generic\",\"month\":\"2025-06\",\"cost\":50,\"conversions\":0,\"conversion_value\":0,\"clicks\":30,\"impressions\":900}'::jsonb)
 ) AS p(payload) ON CONFLICT DO NOTHING;" >/dev/null

# Three competing advertisers, under both the spellings the system meets them
# by: the domain the keyword vendor reports and the name the Transparency
# Center was searched under.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'dataforseo', 'domain_competitor',
        jsonb_build_object('competitor_domain', c.domain, 'for_domain', 'sdsmanager.com',
                           'avg_position', 2.4, 'intersections', c.n,
                           'paid_keyword_count', c.n * 3, 'paid_estimated_traffic_cost', c.cost),
        md5('dc:' || c.domain)
 FROM (VALUES ('chemwatch.net', 240, 18000.0), ('sdsbinder.com', 120, 4500.0),
              ('msdsonline.com', 60, 0.0)) AS c(domain, n, cost)
 ON CONFLICT DO NOTHING;" >/dev/null

psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'dataforseo', 'serp_snapshot',
        jsonb_build_object('keyword', k.term, 'results', jsonb_build_array(
          jsonb_build_object('rank', 1, 'domain', 'chemwatch.net'),
          jsonb_build_object('rank', 2, 'domain', 'sdsbinder.com'),
          jsonb_build_object('rank', 3, 'domain', 'msdsonline.com'))),
        md5('serp:' || k.term)
 FROM (VALUES ('sds management software'), ('free sds template')) AS k(term)
 ON CONFLICT DO NOTHING;" >/dev/null

# 120 creatives: 40 per advertiser, which is what clears the ≥100 criterion.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'transparency', 'competitor_creative',
        jsonb_build_object(
          'advertiser', a.name,
          'ad_id', 'CR-' || replace(a.name, ' ', '') || '-' || g.i,
          'format', 'text', 'first_shown', '2025-01-05', 'last_shown', '2025-10-18',
          'creative_text', a.name || ': compliance without the binders. Start a free trial today. Offer ' || g.i,
          'destination_url', 'https://' || a.domain || '/sds',
          'regions', jsonb_build_array('US'),
          'screenshot_path', 'creatives/verify/' || replace(a.name, ' ', '') || '.png'),
        md5('cc:' || a.name || g.i)
 FROM (VALUES ('Chemwatch','chemwatch.net'), ('SDS Binder','sdsbinder.com'),
              ('MSDSonline','msdsonline.com')) AS a(name, domain),
      LATERAL generate_series(0, 39) AS g(i)
 ON CONFLICT DO NOTHING;" >/dev/null

# 2,400 priced keywords in four families, a quarter of them carrying monthly
# history so seasonality has something to find and the totals can differ.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'dataforseo', 'keyword_metrics',
   jsonb_build_object(
     'keyword', t.keyword, 'search_volume', 10 + (t.i % 90),
     'cpc', 4.5, 'low_top_of_page_bid', 3.2, 'high_top_of_page_bid', 9.8,
     'competition', 'HIGH', 'competition_index', 70,
     'origin', 'site', 'domain', 'sdsmanager.com',
     'monthly_searches', CASE WHEN t.i % 4 = 0 THEN (
        SELECT jsonb_agg(jsonb_build_object('year', 2025, 'month', m,
                 'search_volume', (10 + (t.i % 90)) * CASE WHEN m IN (9,10) THEN 2 ELSE 1 END))
        FROM generate_series(1, 12) AS m) ELSE '[]'::jsonb END),
   md5('kw:' || t.keyword)
 FROM (SELECT f.term || ' ' || g.i AS keyword, g.i AS i
       FROM (VALUES ('sds software', 800), ('ghs labeling', 800),
                    ('chemical inventory', 600), ('free sds template', 200)) AS f(term, n),
            LATERAL generate_series(0, f.n - 1) AS g(i)) AS t
 ON CONFLICT DO NOTHING;" >/dev/null

# Our own pages: two answer a keyword family, one answers none of them.
psql_ "INSERT INTO evidence (id, project_id, source, kind, payload, hash)
 SELECT gen_random_uuid(), '$PROJECT'::uuid, 'web', 'page', p.payload, md5('pg:' || p.payload::text)
 FROM (VALUES
  ('{\"url\":\"https://sdsmanager.com/sds-software\",\"title\":\"SDS software for chemical manufacturers\",\"h1\":\"SDS software that keeps your library current\",\"h2\":[\"Why SDS software\"],\"meta_description\":\"Manage safety data sheets in one place.\",\"text_excerpt\":\"software for managing safety data sheets\"}'::jsonb),
  ('{\"url\":\"https://sdsmanager.com/ghs-labeling\",\"title\":\"GHS labeling software\",\"h1\":\"GHS labeling made simple\",\"h2\":[\"GHS labeling rules\"],\"meta_description\":\"Print compliant GHS labels.\",\"text_excerpt\":\"labeling for hazardous chemicals\"}'::jsonb),
  ('{\"url\":\"https://sdsmanager.com/about\",\"title\":\"About us\",\"h1\":\"Our story\",\"h2\":[],\"meta_description\":\"Who we are.\",\"text_excerpt\":\"a company founded in Norway\"}'::jsonb)
 ) AS p(payload) ON CONFLICT DO NOTHING;" >/dev/null

ge "competitor creatives seeded" "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT' AND kind='competitor_creative';")" 100
ge "keyword metrics seeded" "$(psql_ "SELECT count(*) FROM evidence WHERE project_id='$PROJECT' AND kind='keyword_metrics';")" 2400

echo "── 3. an operator launches stages 1.3 + 1.4 ────────────────────────────"
NODES='["1.3.1","1.3.2","1.3.3","1.3.4","1.4.1","1.4.2","1.4.3","1.4.4","1.4.5"]'
T=$(csrf "$O")
RUN=$(curl -s -b "$O" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
  -d "{\"mode\":\"partial\",\"node_ids\":$NODES}" "$B/projects/$PROJECT/runs")
RUN_ID=$(printf '%s' "$RUN" | jq_ 'd["id"]')
chk "9 selected nodes widen to their 14-node closure" \
    "$(printf '%s' "$RUN" | jq_ 'len(d["selected_node_ids"])')" "14"
[ -n "$RUN_ID" ] || { echo "no run to follow"; exit 1; }

wait_for(){ # jar, then the statuses to stop on
  printf "  …waiting for the worker"
  local status=""
  for _ in $(seq 1 200); do
    sleep 3; printf "."
    status=$(curl -s -b "$A" "$B/runs/$RUN_ID" | jq_ 'd["status"]')
    case "$status" in awaiting_approval|succeeded|failed|cancelled) break ;; esac
  done
  printf "\n"
  printf '%s' "$status"
}
STATUS=$(wait_for)
chk "the run halts on the legal gate first" "$STATUS" "awaiting_approval"
STATE=$(curl -s -b "$A" "$B/runs/$RUN_ID")
chk "…and the gate it halted on is 1.1.5" \
    "$(printf '%s' "$STATE" | jq_ '[n[\"id\"] for n in d[\"nodes\"] if n[\"status\"]==\"awaiting_approval\"][0]')" \
    "1.1.5"
chk "nothing is marked skipped" \
    "$(printf '%s' "$STATE" | jq_ 'sum(1 for n in d[\"nodes\"] if n[\"status\"]==\"skipped\")')" "0"

echo "── 4. the P4 exit criteria ─────────────────────────────────────────────"
node_(){ curl -s -b "$A" "$B/runs/$RUN_ID/nodes/$1"; }
CORPUS=$(node_ 1.3.2)
ge "≥100 competitor creatives in the corpus" \
   "$(printf '%s' "$CORPUS" | jq_ 'len(d["output"]["ads"])')" 100
chk "every ad in the corpus was read, none silently dropped" \
    "$(printf '%s' "$CORPUS" | jq_ 'd["output"]["unread_ads"]')" "0"
chk "every ad carries the screenshot it was captured from" \
    "$(printf '%s' "$CORPUS" | jq_ 'str(all(a["screenshot_path"] for a in d["output"]["ads"])).lower()')" \
    "true"

CLASSIFIED=$(node_ 1.4.2)
ge "≥2,000 classified keywords" \
   "$(printf '%s' "$CLASSIFIED" | jq_ 'len(d["output"]["classified"])')" 2000
chk "no term came back unlabelled" \
    "$(printf '%s' "$CLASSIFIED" | jq_ 'd["output"]["unclassified_count"]')" "0"
chk "no classification batch failed" \
    "$(printf '%s' "$CLASSIFIED" | jq_ 'd["output"]["failed_batches"]')" "0"

DEMAND=$(node_ 1.4.3)
ge "≥2,000 priced keywords" \
   "$(printf '%s' "$DEMAND" | jq_ 'd["output"]["totals"]["terms_priced"]')" 2000
chk "a term the vendor never returned is unpriced, not zero" \
    "$(printf '%s' "$DEMAND" | jq_ 'str(d["output"]["totals"]["terms_unpriced"] > 0).lower()')" "true"

MAPPED=$(node_ 1.4.5)
ge "every theme is mapped to a page or named as a gap" \
   "$(printf '%s' "$MAPPED" | jq_ 'len(d["output"]["mapping"])')" 1
chk "the SDS software theme lands on the SDS software page" \
    "$(printf '%s' "$MAPPED" | jq_ '([m["best_url"] for m in d["output"]["mapping"] if m["term_cluster"]=="software"] or [None])[0]')" \
    "https://sdsmanager.com/sds-software"

SPEND=$(node_ 1.3.3)
chk "one company is one spend estimate, not two spellings" \
    "$(printf '%s' "$SPEND" | jq_ 'len(d["output"]["estimates"])')" "3"
chk "every spend estimate states the method behind it" \
    "$(printf '%s' "$SPEND" | jq_ 'str(all(e["method"] and e["basis"] for e in d["output"]["estimates"])).lower()')" \
    "true"

BLOCK=$(node_ 1.4.4)
chk "a term that converts is never blocked" \
    "$(printf '%s' "$BLOCK" | jq_ 'str(\"sds management software\" not in [n[\"term\"] for n in d[\"output\"][\"negatives\"]]).lower()')" \
    "true"

echo "── 5. both gates, in order ─────────────────────────────────────────────"
decide(){ local t; t=$(csrf "$P")
  curl -s -b "$P" -H "X-CSRF-Token: $t" -H 'Content-Type: application/json' \
    -d "{\"decision\":\"approve\",\"note\":\"Verified by scripts/verify-p4.sh\"}" "$B/approvals/$1"; }
inbox(){ curl -s -b "$P" "$B/approvals?run_id=$RUN_ID&mine=true"; }

LEGAL=$(inbox | jq_ 'd["items"][0]["id"]')
chk "the legal gate is in the approver's inbox" "$(inbox | jq_ 'd["items"][0]["node_id"]')" "1.1.5"
chk "approving it re-queues the run" "$(decide "$LEGAL" | jq_ 'str(d["resumed"]).lower()')" "true"

STATUS=$(wait_for)
chk "the run stops again, on the differentiation gate" "$STATUS" "awaiting_approval"
DIFF=$(inbox | jq_ 'd["items"][0]["id"]')
chk "…and that gate is 1.3.4" "$(inbox | jq_ 'd["items"][0]["node_id"]')" "1.3.4"
chk "1.3.4 proposes a claim for a human to decide" \
    "$(curl -s -b "$A" "$B/runs/$RUN_ID/nodes/1.3.4" | jq_ 'str(bool(d["output"]["recommended_claim"])).lower()')" \
    "true"
chk "approving it re-queues the run" "$(decide "$DIFF" | jq_ 'str(d["resumed"]).lower()')" "true"

STATUS=$(wait_for)
chk "the run completes once both gates are answered" "$STATUS" "succeeded"

echo "── 6. both decisions are audit-logged ──────────────────────────────────"
AUDIT=$(curl -s -b "$A" "$B/audit?action=approval.decided")
ge "two gate decisions recorded" \
   "$(printf '%s' "$AUDIT" | jq_ 'sum(1 for i in d["items"] if i["meta"].get("node_id") in ("1.1.5","1.3.4"))')" 2

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
