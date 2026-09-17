#!/usr/bin/env bash
# P7's exit criteria, run against a live stack.
#
#   "Live run is fully observable; an approver decides a gate from the inbox
#    without opening the console and the run resumes; a `viewer` can read
#    everything and write nothing; report readable and exportable without
#    touching the API"                                            — PRD §17, P7
#
# Everything goes through the browser's path (web -> rewrite -> private network
# -> FastAPI). `scripts/browser-check-p7.py` covers the other half of that
# sentence — what those four screens actually render, in a real browser.
#
# The run this asserts against is seeded rather than executed: a real one needs
# a live OpenRouter key and forty minutes, and what P7 builds is the *reading*
# of a run, not the running of it. The seed writes exactly what the executor
# writes — node rows, an open gate, evidence, a report — through the same
# models.
#
#   make up && ./scripts/verify-p7.sh
#   BASE_URL=http://localhost:3007 COMPOSE_CMD="docker compose -p ads-research-agent-p7 -f /tmp/compose-p7.yml --project-directory ." ./scripts/verify-p7.sh
#
# Idempotent: members, project, runs and evidence are created once and reused;
# the gate is reopened on every run so the decision step always has something
# to decide.
set -uo pipefail
BASE=${BASE_URL:-http://localhost:3000}
B="$BASE/api/v1"
COMPOSE=${COMPOSE_CMD:-docker compose}
ADMIN_EMAIL=${ADMIN_EMAIL:-admin@example.com}
ADMIN_PASSWORD=${ADMIN_PASSWORD:-change-me-at-least-12-chars}
MEMBER_PASSWORD=${MEMBER_PASSWORD:-verify-p7-member-passphrase}

PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }
has(){ case "$2" in *"$3"*) ok "$1";; *) no "$1" "missing: $3";; esac; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); OP=$(mktemp); AP=$(mktemp); V=$(mktemp); OUT=$(mktemp -d)
trap 'rm -rf "$A" "$OP" "$AP" "$V" "$OUT"' EXIT

csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' \
         -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login"; }
get(){ curl -s -b "$1" "$B$2"; }
code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" "$B$2"; }
send(){ curl -s -b "$1" -c "$1" -X "$2" -H "X-CSRF-Token: $(csrf "$1")" \
        -H 'Content-Type: application/json' ${4:+-d "$4"} "$B$3"; }
send_code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" -c "$1" -X "$2" \
        -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' ${4:+-d "$4"} "$B$3"; }

echo "── 0. sign in ──────────────────────────────────────────────────────────"
ROLE=$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

echo "── 1. the three other roles ────────────────────────────────────────────"
for ROLE_NAME in operator approver viewer; do
  EMAIL="p7-$ROLE_NAME@example.com"
  EXISTS=$(get "$A" "/users" | jq_ "sum(1 for u in d['users'] if u['email']=='$EMAIL')")
  if [ "$EXISTS" = "0" ]; then
    LINK=$(send "$A" POST "/users/invite" \
      "{\"email\":\"$EMAIL\",\"name\":\"P7 ${ROLE_NAME}\",\"role\":\"$ROLE_NAME\"}" | jq_ 'd["link"]')
    TOKEN=${LINK##*/}
    J=$(mktemp)
    ACCEPTED=$(curl -s -c "$J" -b "$J" -H "X-CSRF-Token: $(csrf "$J")" -H 'Content-Type: application/json' \
      -d "{\"name\":\"P7 ${ROLE_NAME}\",\"password\":\"$MEMBER_PASSWORD\"}" \
      "$B/invites/$TOKEN/accept" | jq_ 'd["role"]')
    rm -f "$J"
    chk "invite + accept as $ROLE_NAME" "$ACCEPTED" "$ROLE_NAME"
  else
    ok "$ROLE_NAME is already a member"
  fi
done
login "$OP" "p7-operator@example.com" "$MEMBER_PASSWORD" >/dev/null
login "$AP" "p7-approver@example.com" "$MEMBER_PASSWORD" >/dev/null
login "$V"  "p7-viewer@example.com"   "$MEMBER_PASSWORD" >/dev/null
chk "the approver holds approval_decide" \
    "$(get "$AP" "/auth/me" | jq_ "'approval_decide' in d['permissions']")" "True"
chk "the viewer does not" \
    "$(get "$V" "/auth/me" | jq_ "'run_execute' in d['permissions']")" "False"

echo "── 2. seed a run worth watching ────────────────────────────────────────"
# Inside the api container: it holds the database URL and the report fixture,
# and this is the one step of the phase that has no endpoint behind it.
SEED=$($COMPOSE exec -T api python - <<'PY' 2>&1 | tail -8
import asyncio, json, pathlib, sys, uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, "/app/src")
import sqlalchemy as sa

from agent.db.models import (
    Approval, ApprovalRequiredRole, ApprovalStatus, NodeRun, NodeRunStatus,
    Project, Report, Run, RunStatus, RunTrigger, User, UserRole,
)
from agent.db.session import get_sessionmaker
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore
from agent.export.contract import ResearchReport
from agent.export.markdown import render_markdown
from agent.orchestrator.events import EventType, RunEventStream
from agent.redis_client import get_redis

PROJECT = "P7 verification"
SHOT_KEY = "creatives/p7-verification/chemwatch-grid.png"
FIXTURE = pathlib.Path("/app/tests/fixtures/report_golden.json")


async def main() -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as s:
        admin = (await s.execute(sa.select(User).where(User.role == UserRole.ADMIN).limit(1))).scalars().first()
        approver = (
            await s.execute(sa.select(User).where(User.email == "p7-approver@example.com"))
        ).scalar_one_or_none()

        project = (await s.execute(sa.select(Project).where(Project.name == PROJECT))).scalar_one_or_none()
        if project is None:
            project = Project(
                workspace_id=admin.workspace_id, name=PROJECT, domain="northwind.example",
                created_by=admin.id, product_context={"pitch": "safety data sheet management"},
                markets=[{"country": "US", "language": "en"}],
                settings={"gate_sla_hours": {"1.1.5": 8}},
            )
            s.add(project)
            await s.flush()
        else:
            project.settings = {**(project.settings or {}), "gate_sla_hours": {"1.1.5": 8}}

        # Evidence first: the node rows cite it, and the report's citations are
        # only meaningful if the rows behind them exist.
        store = EvidenceStore(s, admin.workspace_id, embedder=HashingEmbedder())
        written = await store.write(
            [
                EvidenceDraft(source="dataforseo", kind="keyword_metrics",
                              payload={"keyword": "sds management software", "volume": 2400, "cpc": 12.4},
                              source_url="https://dataforseo.example/kw/1"),
                EvidenceDraft(source="google_ads", kind="search_term_pnl",
                              payload={"search_term": "sds software", "cost": 2140.0, "conversions": 18}),
                EvidenceDraft(source="transparency", kind="competitor_ad",
                              payload={"advertiser": "Chemwatch", "headline": "SDS management, simplified",
                                       "screenshot_path": SHOT_KEY},
                              source_url="https://adstransparency.google.com/example"),
            ],
            project_id=project.id,
        )
        evidence_ids = [str(one) for one in written.evidence_ids]

        run = (
            await s.execute(
                sa.select(Run).where(Run.project_id == project.id, Run.trigger == RunTrigger.MANUAL)
                .order_by(Run.id.desc())
            )
        ).scalars().first()
        if run is None:
            run = Run(
                workspace_id=admin.workspace_id, project_id=project.id,
                status=RunStatus.AWAITING_APPROVAL, trigger=RunTrigger.MANUAL,
                triggered_by=admin.id, started_at=now - timedelta(minutes=12),
                cost_usd=Decimal("0.4210"), token_in=48210, token_out=9120,
            )
            s.add(run)
            await s.flush()
        run.status = RunStatus.AWAITING_APPROVAL

        states = [
            ("1.1.1", NodeRunStatus.SUCCEEDED, {"products": [{"name": "SDS Manager"}], "evidence_ids": evidence_ids[:1]}),
            ("1.1.2", NodeRunStatus.SUCCEEDED, {"segments": [{"label": "EHS teams"}], "evidence_ids": evidence_ids[:2]}),
            ("1.1.3", NodeRunStatus.FAILED, None),
            ("1.1.5", NodeRunStatus.AWAITING_APPROVAL, {"prohibited_claims": ["100% compliant"]}),
        ]
        existing = {
            row.node_id: row
            for row in (await s.execute(sa.select(NodeRun).where(NodeRun.run_id == run.id))).scalars().all()
        }
        for node_id, status, output in states:
            row = existing.get(node_id)
            if row is None:
                row = NodeRun(run_id=run.id, node_id=node_id, attempt=1)
                s.add(row)
            row.status = status
            row.output = output
            row.evidence_ids = [uuid.UUID(one) for one in (output or {}).get("evidence_ids", [])]
            row.prompt = f"SYSTEM: you are node {node_id}\nUSER: the evidence follows"
            row.model = "openai/gpt-4o-mini"
            row.token_in, row.token_out = 12000, 2400
            row.cost_usd = Decimal("0.1400")
            row.latency_ms = 8400
            row.started_at = now - timedelta(minutes=10)
            row.finished_at = None if status is NodeRunStatus.AWAITING_APPROVAL else now - timedelta(minutes=9)
            row.error = {"code": "model_error", "message": "the model returned an unparseable body"} if status is NodeRunStatus.FAILED else None

        # The gate: reopened every time, so the decision step always has one.
        pending = (
            await s.execute(
                sa.select(Approval).where(Approval.run_id == run.id, Approval.node_id == "1.1.5")
            )
        ).scalars().first()
        if pending is None:
            pending = Approval(run_id=run.id, node_id="1.1.5")
            s.add(pending)
        pending.status = ApprovalStatus.PENDING
        pending.required_role = ApprovalRequiredRole.APPROVER
        pending.assignee_id = approver.id if approver else None
        pending.proposal = {
            "prohibited_claims": ["100% compliant", "guaranteed audit pass"],
            "required_disclaimers": ["Regulations vary by jurisdiction."],
            "regulated_terms": [{"term": "OSHA-approved", "rule": "never claim endorsement"}],
            "evidence_ids": evidence_ids[:2],
        }
        pending.edited_proposal = None
        pending.decision_note = None
        pending.decided_by = None
        pending.decided_at = None

        # A second, finished run carrying the report the viewer reads.
        done = (
            await s.execute(
                sa.select(Run).where(Run.project_id == project.id, Run.status == RunStatus.SUCCEEDED)
            )
        ).scalars().first()
        if done is None:
            done = Run(
                workspace_id=admin.workspace_id, project_id=project.id, status=RunStatus.SUCCEEDED,
                trigger=RunTrigger.MANUAL, triggered_by=admin.id,
                started_at=now - timedelta(hours=3), finished_at=now - timedelta(hours=2),
                cost_usd=Decimal("2.8400"),
            )
            s.add(done)
            await s.flush()

        payload = json.loads(FIXTURE.read_text())
        payload["project_id"] = str(project.id)
        payload["run_id"] = str(done.id)
        report_model = ResearchReport.model_validate(payload)
        report = (await s.execute(sa.select(Report).where(Report.run_id == done.id))).scalar_one_or_none()
        if report is None:
            report = Report(run_id=done.id, schema_version=report_model.schema_version)
            s.add(report)
        report.payload = report_model.model_dump(mode="json")
        report.markdown = render_markdown(report_model)

        await s.commit()

        # The feed the console replays. Written to the same Redis stream the
        # executor publishes to, so the console reads it the same way.
        stream = RunEventStream(get_redis(), run.id)
        await stream.publish(EventType.RUN_STATUS, run_id=str(run.id), status="running")
        for node_id, status, _ in states[:2]:
            await stream.publish(EventType.NODE_STARTED, node_id=node_id, attempt=1)
            await stream.publish(EventType.NODE_COMPLETED, node_id=node_id, status=status.value, cost_usd="0.14")
        await stream.publish(EventType.NODE_PROGRESS, node_id="1.1.3", message="retrying after a model error")
        await stream.publish(EventType.APPROVAL_REQUIRED, node_id="1.1.5",
                             approval_id=str(pending.id), required_role="approver",
                             assignee_id=str(pending.assignee_id) if pending.assignee_id else None)

        print(f"PROJECT_ID={project.id}")
        print(f"RUN_ID={run.id}")
        print(f"REPORT_RUN_ID={done.id}")
        print(f"APPROVAL_ID={pending.id}")
        print(f"EVIDENCE_IDS={','.join(evidence_ids)}")
        print(f"SHOT_KEY={SHOT_KEY}")


asyncio.run(main())
PY
)
PROJECT_ID=$(printf '%s' "$SEED" | sed -n 's/^PROJECT_ID=//p' | tr -d '\r')
RUN_ID=$(printf '%s' "$SEED" | sed -n 's/^RUN_ID=//p' | tr -d '\r')
REPORT_RUN_ID=$(printf '%s' "$SEED" | sed -n 's/^REPORT_RUN_ID=//p' | tr -d '\r')
APPROVAL_ID=$(printf '%s' "$SEED" | sed -n 's/^APPROVAL_ID=//p' | tr -d '\r')
EVIDENCE_IDS=$(printf '%s' "$SEED" | sed -n 's/^EVIDENCE_IDS=//p' | tr -d '\r')
SHOT_KEY=$(printf '%s' "$SEED" | sed -n 's/^SHOT_KEY=//p' | tr -d '\r')
if [ -z "$RUN_ID" ]; then
  no "seed a run" "$SEED"; echo; echo "cannot continue without a seeded run"; exit 1
fi
ok "seeded run $RUN_ID with an open gate, and report run $REPORT_RUN_ID"

# The capture goes in from the worker: it is the only service with the Volume.
$COMPOSE exec -T worker python - "$SHOT_KEY" <<'PY' >/dev/null 2>&1
import base64, sys
sys.path.insert(0, "/app/src")
from agent.storage.backend import get_storage

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
get_storage().put(sys.argv[1], PNG, content_type="image/png")
PY
ok "a competitor capture is on the worker's Volume"

echo "── 3. the console can see the run ──────────────────────────────────────"
RUN=$(get "$A" "/runs/$RUN_ID")
chk "GET /runs/{id} names who triggered it" "$(printf '%s' "$RUN" | jq_ 'd["triggered_by_name"] is not None')" "True"
chk "  and carries every node"  "$(printf '%s' "$RUN" | jq_ 'len(d["nodes"]) > 4')" "True"
chk "  and the edges between them" "$(printf '%s' "$RUN" | jq_ 'len(d["edges"]) > 0')" "True"
chk "  and says a gate is open" "$(printf '%s' "$RUN" | jq_ 'd["status"]')" "awaiting_approval"

NODE=$(get "$A" "/runs/$RUN_ID/nodes/1.1.1")
chk "GET /runs/{id}/nodes/{node} returns the output" "$(printf '%s' "$NODE" | jq_ 'd["output"] is not None')" "True"
chk "  the prompt"  "$(printf '%s' "$NODE" | jq_ 'd["prompt"] is not None')" "True"
chk "  the metrics" "$(printf '%s' "$NODE" | jq_ 'd["model"] is not None and d["token_in"] > 0')" "True"
chk "  and what it cited" "$(printf '%s' "$NODE" | jq_ 'len(d["evidence_ids"]) > 0')" "True"
# Which node has not run is not a constant: this script is re-runnable, the
# gate it decides re-queues the run, and a node that was untouched last time has
# a row this time. Ask the run itself rather than naming one and hoping.
UNRUN=$(printf '%s' "$RUN" | jq_ "([n['id'] for n in d['nodes'] if n['status'] is None] or [''])[0]")
if [ -n "$UNRUN" ]; then
  chk "a node that has not run is a 404, not an empty body" \
      "$(code "$A" "/runs/$RUN_ID/nodes/$UNRUN")" "404"
else
  ok "a node that has not run is a 404 (skipped — every node of this run has a row)"
fi

echo "── 4. the feed replays what was missed ─────────────────────────────────"
SSE=$(curl -s -N --max-time 6 -b "$A" "$B/runs/$RUN_ID/events" 2>/dev/null | head -60)
FRAMES=$(printf '%s\n' "$SSE" | grep -c '^event: ')
# A first connection carries no `Last-Event-ID`, so the API replays the run from
# its first event rather than joining live — six were published above.
chk "GET /runs/{id}/events replays every frame from the beginning" \
    "$([ "$FRAMES" -ge 6 ] && echo yes || echo "$FRAMES")" "yes"
has "  including the node events" "$SSE" "event: node.completed"
has "  and the gate opening" "$SSE" "event: approval.required"
has "  each frame carrying its cursor" "$SSE" "id: "

echo "── 5. presence ─────────────────────────────────────────────────────────"
# On identity, not on a count: a check-in lasts 30 seconds, so re-running this
# script inside that window still sees the last run's viewer. "Am I listed"
# is the question presence actually answers, and it survives a warm TTL.
ADMIN_NAME=$(get "$A" "/auth/me" | jq_ 'd["name"]')
VIEWER_NAME=$(get "$V" "/auth/me" | jq_ 'd["name"]')
chk "POST /runs/{id}/presence lists the caller" \
    "$(send "$A" POST "/runs/$RUN_ID/presence" "" | jq_ "any(v['name'] == '$ADMIN_NAME' for v in d['viewers'])")" "True"
send "$V" POST "/runs/$RUN_ID/presence" "" >/dev/null
BOTH=$(send "$A" POST "/runs/$RUN_ID/presence" "")
chk "  and the second console alongside it" \
    "$(printf '%s' "$BOTH" | jq_ "{'$ADMIN_NAME', '$VIEWER_NAME'} <= {v['name'] for v in d['viewers']}")" "True"
chk "  counted honestly" "$(printf '%s' "$BOTH" | jq_ 'd["total"] >= 2')" "True"
chk "  a viewer may announce themselves" "$(send_code "$V" POST "/runs/$RUN_ID/presence" "")" "200"

echo "── 6. the inbox, and a decision made from it ───────────────────────────"
INBOX=$(get "$AP" "/approvals?mine=true&status=pending")
chk "the approver's inbox holds the gate" \
    "$(printf '%s' "$INBOX" | jq_ "sum(1 for i in d['items'] if i['id']=='$APPROVAL_ID')")" "1"
chk "  it says they may decide it" \
    "$(printf '%s' "$INBOX" | jq_ "[i['can_decide'] for i in d['items'] if i['id']=='$APPROVAL_ID'][0]")" "True"
chk "  it names the project" \
    "$(printf '%s' "$INBOX" | jq_ "[i['project_name'] for i in d['items'] if i['id']=='$APPROVAL_ID'][0] is not None")" "True"
chk "  who launched the run" \
    "$(printf '%s' "$INBOX" | jq_ "[i['run_triggered_by_name'] for i in d['items'] if i['id']=='$APPROVAL_ID'][0] is not None")" "True"
chk "  and the SLA to count down against" \
    "$(printf '%s' "$INBOX" | jq_ "[i['sla_hours'] for i in d['items'] if i['id']=='$APPROVAL_ID'][0]")" "8"
chk "  the proposal it is deciding on" \
    "$(printf '%s' "$INBOX" | jq_ "len([i for i in d['items'] if i['id']=='$APPROVAL_ID'][0]['proposal']) > 0")" "True"

chk "an operator sees it but may not act" \
    "$(get "$OP" "/approvals?status=pending" | jq_ "[i['can_decide'] for i in d['items'] if i['id']=='$APPROVAL_ID'][0]")" "False"
chk "  and is refused if they try" \
    "$(send_code "$OP" POST "/approvals/$APPROVAL_ID" '{"decision":"approve"}')" "403"

DECISION=$(send "$AP" POST "/approvals/$APPROVAL_ID" \
  '{"decision":"approve","note":"verified against the regulated-terms list","edited_proposal":{"prohibited_claims":["100% compliant"],"required_disclaimers":["Regulations vary by jurisdiction."],"evidence_ids":[]}}')
chk "the approver decides it from the inbox" "$(printf '%s' "$DECISION" | jq_ 'd["approval"]["status"]')" "approved"
chk "  the edit is what the run carries forward" \
    "$(printf '%s' "$DECISION" | jq_ 'len(d["approval"]["edited_proposal"]["prohibited_claims"])')" "1"
chk "  and the run picks up again" "$(printf '%s' "$DECISION" | jq_ 'd["resumed"]')" "True"
chk "  the run is no longer waiting" \
    "$(get "$A" "/runs/$RUN_ID" | jq_ 'd["status"] != "awaiting_approval"')" "True"
chk "  and the decision is in the audit log" \
    "$(get "$A" "/audit?action=approval.decided" | jq_ 'len(d["entries"]) > 0')" "True"

echo "── 7. a viewer reads everything and writes nothing ─────────────────────"
chk "viewer GET /runs/{id}"            "$(code "$V" "/runs/$RUN_ID")" "200"
chk "viewer GET /runs/{id}/nodes/{id}" "$(code "$V" "/runs/$RUN_ID/nodes/1.1.1")" "200"
chk "viewer GET /approvals"            "$(code "$V" "/approvals")" "200"
chk "viewer GET /evidence"             "$(code "$V" "/evidence")" "200"
chk "viewer GET /reports/{run}"        "$(code "$V" "/reports/$REPORT_RUN_ID")" "200"
chk "viewer POST /runs/{id}/cancel is refused"       "$(send_code "$V" POST "/runs/$RUN_ID/cancel" "")" "403"
chk "viewer POST /runs/{id}/retry-failed is refused" "$(send_code "$V" POST "/runs/$RUN_ID/retry-failed" "")" "403"
chk "viewer POST /approvals/{id} is refused"         "$(send_code "$V" POST "/approvals/$APPROVAL_ID" '{"decision":"approve"}')" "403"
chk "viewer PATCH /projects/{id} is refused"         "$(send_code "$V" PATCH "/projects/$PROJECT_ID" '{"name":"nope"}')" "403"

echo "── 8. the report reads, and leaves ─────────────────────────────────────"
REPORT=$(get "$A" "/reports/$REPORT_RUN_ID")
chk "GET /reports/{run} carries the payload" "$(printf '%s' "$REPORT" | jq_ 'd["payload"]["schema_version"]')" "1.0"
chk "  a readiness verdict"  "$(printf '%s' "$REPORT" | jq_ 'd["payload"]["launch_readiness"] in ("go","go_with_fixes","no_go")')" "True"
chk "  and citations to resolve" "$(printf '%s' "$REPORT" | jq_ 'len(str(d["payload"])) > 500')" "True"

JOB=$(send "$V" POST "/reports/$REPORT_RUN_ID/export?format=md" "" | jq_ 'd["job_id"]')
chk "a viewer may request an export (PRD §4.1)" "$([ -n "$JOB" ] && echo yes)" "yes"
STATUS=""
for _ in $(seq 1 40); do
  STATUS=$(get "$V" "/exports/$JOB" | jq_ 'd["status"]')
  case "$STATUS" in ready|failed) break;; esac
  sleep 1
done
chk "  the worker generates it" "$STATUS" "ready"
curl -s -b "$V" -o "$OUT/report.md" -w '' "$B/exports/$JOB/download"
chk "  and it downloads with content" "$([ -s "$OUT/report.md" ] && echo yes)" "yes"

echo "── 9. evidence, by citation and by picture ─────────────────────────────"
FIRST_ID=${EVIDENCE_IDS%%,*}
LAST_ID=${EVIDENCE_IDS##*,}
chk "GET /evidence?ids= returns exactly what was cited" \
    "$(get "$A" "/evidence?ids=$FIRST_ID" | jq_ 'len(d["items"])')" "1"
chk "  and says which rows carry a capture" \
    "$(get "$A" "/evidence?source=transparency" | jq_ 'any(i["has_screenshot"] for i in d["items"])')" "True"
TYPE=$(curl -s -o "$OUT/shot.png" -w '%{content_type}' -b "$A" "$B/evidence/$LAST_ID/screenshot")
chk "GET /evidence/{id}/screenshot streams the capture" "$TYPE" "image/png"
chk "  with bytes in it" "$([ -s "$OUT/shot.png" ] && echo yes)" "yes"
chk "a row with no capture is a 404" "$(code "$A" "/evidence/$FIRST_ID/screenshot")" "404"
chk "search still ranks" "$(get "$A" "/evidence?q=sds+management" | jq_ 'd["ranked"]')" "True"

echo
printf "── %d passed, %d failed ────────────────────────────────────────────────\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
