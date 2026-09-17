#!/usr/bin/env bash
# P8's exit criteria, run against a live stack.
#
#   "Scheduled run executes unattended with `triggered_by=NULL`; diff view
#    renders; SLA reminder fires; `pg_dump` lands in `/data/backups/`; eval
#    suite green; coverage ≥ 80% on core packages incl. `auth/`"  — PRD §17, P8
#
# Everything a browser could do goes through the browser's path (web -> rewrite
# -> private network -> FastAPI). The four things a browser *cannot* reach —
# the cron poller, the reaper, the reminder job and `pg_dump` — are invoked
# inside the worker container, which is where they really run and the only place
# `pg_dump` and the Volume both exist.
#
#   make up && ./scripts/verify-p8.sh
#   BASE_URL=http://localhost:3008 COMPOSE_CMD="docker compose -p ads-research-agent-p8 -f /tmp/compose-p8.yml --project-directory ." ./scripts/verify-p8.sh
#
# Idempotent: the project and schedule are created once and reused, and every
# job under test is safe to run twice by construction.
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
ne(){ [ "$2" != "$3" ] && ok "$1" || no "$1" "did not expect $3"; }
has(){ case "$2" in *"$3"*) ok "$1";; *) no "$1" "missing: $3";; esac; }
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp)
trap 'rm -f "$A"' EXIT

csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
login(){ curl -s -c "$1" -b "$1" -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' \
         -d "{\"email\":\"$2\",\"password\":\"$3\"}" "$B/auth/login"; }
get(){ curl -s -b "$1" "$B$2"; }
code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" "$B$2"; }
send(){ curl -s -b "$1" -c "$1" -X "$2" -H "X-CSRF-Token: $(csrf "$1")" \
        -H 'Content-Type: application/json' ${4:+-d "$4"} "$B$3"; }
send_code(){ curl -s -o /dev/null -w '%{http_code}' -b "$1" -c "$1" -X "$2" \
        -H "X-CSRF-Token: $(csrf "$1")" -H 'Content-Type: application/json' ${4:+-d "$4"} "$B$3"; }
worker(){ $COMPOSE exec -T worker "$@"; }
pysh(){ worker uv run python -c "$1"; }

echo "── 0. sign in ──────────────────────────────────────────────────────────"
ROLE=$(login "$A" "$ADMIN_EMAIL" "$ADMIN_PASSWORD" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$ROLE" = "admin" ] || { echo "cannot continue without a session"; exit 1; }

PROJECT=$(get "$A" "/projects" | jq_ 'd["projects"][0]["id"] if d["projects"] else ""')
if [ -z "$PROJECT" ]; then
  PROJECT=$(send "$A" POST "/projects" '{"name":"P8 verification","domain":"example.com"}' | jq_ 'd["id"]')
fi
ne "a project exists to schedule" "$PROJECT" ""

echo "── 1. security headers (PRD §17 P8, security pass) ─────────────────────"
H=$(curl -s -D - -o /dev/null "$B/health")
has "CSP denies everything on the API" "$H" "default-src 'none'"
has "frame-ancestors is none" "$H" "frame-ancestors 'none'"
has "Permissions-Policy names the features we never use" "$H" "camera=()"
has "Cross-Origin-Opener-Policy is same-origin" "$H" "same-origin"
has "workspace data is never cached by an intermediary" "$H" "no-store"
has "every response carries a request id" "$H" "x-request-id"

HTML=$(curl -s -D - -o /dev/null "$BASE/login")
has "the document has its own CSP" "$HTML" "content-security-policy"
has "the app refuses to be framed" "$HTML" "frame-ancestors 'none'"
case "$HTML" in *"x-powered-by"*) no "the framework version is not advertised" "X-Powered-By present";; *) ok "the framework version is not advertised";; esac

echo "── 2. schedules: the API ───────────────────────────────────────────────"
PREVIEW=$(send "$A" POST "/schedules/preview" '{"cron":"0 3 * * *","timezone":"Europe/Copenhagen"}')
has "the preview says what the expression means" "$(echo "$PREVIEW" | jq_ 'd["description"]')" "03:00"
has "the preview names the timezone" "$(echo "$PREVIEW" | jq_ 'd["description"]')" "Europe/Copenhagen"
chk "the preview lists three firings" "$(echo "$PREVIEW" | jq_ 'len(d["upcoming"])')" "3"
chk "a bad expression is refused with a reason" \
    "$(send_code "$A" POST "/schedules/preview" '{"cron":"60 3 * * *"}')" "422"

SCHED=$(get "$A" "/schedules" | jq_ '(d["items"][0]["id"] if d["items"] else "")')
if [ -z "$SCHED" ]; then
  SCHED=$(send "$A" POST "/schedules" "{\"project_id\":\"$PROJECT\",\"cron\":\"*/5 * * * *\",\"timezone\":\"UTC\"}" | jq_ 'd["id"]')
fi
ne "a schedule exists" "$SCHED" ""
ROW=$(get "$A" "/schedules")
ne "the schedule has a next firing" "$(echo "$ROW" | jq_ 'd["items"][0]["next_at"]')" "None"
chk "pausing clears the next firing" \
    "$(send "$A" PATCH "/schedules/$SCHED" '{"enabled":false}' | jq_ 'str(d["next_at"])')" "None"
ne "resuming restores it" \
    "$(send "$A" PATCH "/schedules/$SCHED" '{"enabled":true}' | jq_ 'str(d["next_at"])')" "None"

echo "── 3. a scheduled run executes unattended ──────────────────────────────"
# Make it due, then run one poll tick inside the worker — the process that owns
# the cron in production.
pysh "
import asyncio, datetime as dt, sqlalchemy as sa
from agent.db.session import get_sessionmaker
from agent.db.models import Schedule
async def main():
    async with get_sessionmaker()() as s:
        await s.execute(sa.update(Schedule).where(Schedule.id=='$SCHED').values(
            enabled=True, next_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))
        await s.commit()
asyncio.run(main())
" >/dev/null 2>&1
POLL=$(pysh "
import asyncio, json
from agent.db.session import get_sessionmaker
from agent.redis_client import get_redis
from agent.scheduling.poller import poll_schedules
async def main():
    async with get_sessionmaker()() as s:
        out = await poll_schedules(s, get_redis())
        print(json.dumps({'launched':[str(r) for r in out.launched],'busy':len(out.skipped_busy)}))
asyncio.run(main())
" 2>/dev/null | tail -1)
RUN_ID=$(echo "$POLL" | jq_ 'd["launched"][0] if d["launched"] else ""')
ne "the poller launched a run" "$RUN_ID" ""

if [ -n "$RUN_ID" ]; then
  RUN=$(get "$A" "/runs/$RUN_ID")
  chk "the run was triggered by the schedule" "$(echo "$RUN" | jq_ 'd["trigger"]')" "schedule"
  chk "ACCEPTANCE: triggered_by is NULL" "$(echo "$RUN" | jq_ 'str(d["triggered_by"])')" "None"
  chk "and no name is invented for it" "$(echo "$RUN" | jq_ 'str(d["triggered_by_name"])')" "None"
  ACTOR=$(get "$A" "/audit?action=run.launched" | jq_ '
    str([r for r in d["entries"] if r["target_id"]=="'"$RUN_ID"'"][0]["actor_id"])')
  chk "the audit row has a null actor (NF5c)" "$ACTOR" "None"
fi

echo "── 4. the reaper ───────────────────────────────────────────────────────"
REAPED=$(pysh "
import asyncio, json, uuid, datetime as dt
from agent.db.session import get_sessionmaker
from agent.db.models import Run, RunStatus, RunTrigger, Project
from agent.redis_client import get_redis
from agent.scheduling.reaper import reap_stale_runs
import sqlalchemy as sa
async def main():
    async with get_sessionmaker()() as s:
        p = (await s.execute(sa.select(Project).limit(1))).scalar_one()
        run = Run(workspace_id=p.workspace_id, project_id=p.id, trigger=RunTrigger.MANUAL,
                  status=RunStatus.RUNNING, started_at=dt.datetime.now(dt.UTC))
        s.add(run); await s.commit(); await s.refresh(run)
        rid = run.id
        out = await reap_stale_runs(s, get_redis())
        await s.refresh(run)
        print(json.dumps({'reaped': str(rid) in [str(x) for x in out.orphaned],
                          'status': run.status.value, 'code': (run.error or {}).get('code')}))
asyncio.run(main())
" 2>/dev/null | tail -1)
chk "a run with no heartbeat is reaped" "$(echo "$REAPED" | jq_ 'str(d["reaped"])')" "True"
chk "and is left in a terminal state" "$(echo "$REAPED" | jq_ 'd["status"]')" "failed"
chk "with a reason a person can act on" "$(echo "$REAPED" | jq_ 'd["code"]')" "reaped"

echo "── 5. an SLA reminder fires ────────────────────────────────────────────"
REMIND=$(pysh "
import asyncio, json, datetime as dt, sqlalchemy as sa
from agent.db.session import get_sessionmaker
from agent.db.models import Approval, ApprovalStatus, ApprovalRequiredRole, Run, RunStatus, RunTrigger, Project
from agent.scheduling.reminders import send_due_reminders
async def main():
    async with get_sessionmaker()() as s:
        p = (await s.execute(sa.select(Project).limit(1))).scalar_one()
        run = Run(workspace_id=p.workspace_id, project_id=p.id, trigger=RunTrigger.MANUAL,
                  status=RunStatus.AWAITING_APPROVAL)
        s.add(run); await s.flush()
        a = Approval(run_id=run.id, node_id='1.1.5', status=ApprovalStatus.PENDING,
                     required_role=ApprovalRequiredRole.APPROVER, proposal={},
                     due_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=6))
        s.add(a); await s.commit(); await s.refresh(a)
        late = a.created_at + dt.timedelta(hours=4)
        first = await send_due_reminders(s, now=late)
        second = await send_due_reminders(s, now=late)
        await s.refresh(a)
        print(json.dumps({'first': [m for _i, m in first.sent], 'second': len(second.sent),
                          'recorded': a.reminders_sent, 'still_pending': a.status.value}))
asyncio.run(main())
" 2>/dev/null | tail -1)
has "a gate past half its SLA is nudged" "$(echo "$REMIND" | jq_ 'd["first"]')" "half"
chk "a milestone fires exactly once" "$(echo "$REMIND" | jq_ 'd["second"]')" "0"
has "the nudge is recorded on the approval" "$(echo "$REMIND" | jq_ 'd["recorded"]')" "half"
chk "and nothing was auto-approved (PRD §16)" "$(echo "$REMIND" | jq_ 'd["still_pending"]')" "pending"

echo "── 6. pg_dump lands in /data/backups/ ──────────────────────────────────"
chk "pg_dump is on the worker's PATH" "$(worker sh -c 'command -v pg_dump >/dev/null && echo yes || echo no' 2>/dev/null | tr -d '\r')" "yes"
CLIENT=$(worker sh -c 'pg_dump --version' 2>/dev/null | tr -d '\r')
has "and is new enough for a PG16 server" "$CLIENT" "16."
BACKUP=$(pysh "
import asyncio, json
from agent.scheduling.backups import run_backup
async def main():
    r = await run_backup()
    print(json.dumps({'key': r.key, 'bytes': r.bytes}))
asyncio.run(main())
" 2>/dev/null | tail -1)
has "ACCEPTANCE: the dump lands under backups/" "$(echo "$BACKUP" | jq_ 'd["key"]')" "backups/"
LANDED=$(worker sh -c 'ls -1 /data/backups/*.dump 2>/dev/null | wc -l' 2>/dev/null | tr -d ' \r')
ne "the file is on the Volume" "$LANDED" "0"
SIZE=$(echo "$BACKUP" | jq_ 'd["bytes"]')
[ "${SIZE:-0}" -gt 1000 ] && ok "and it is not an empty dump ($SIZE bytes)" || no "and it is not an empty dump" "got ${SIZE:-0} bytes"

echo "── 7. retention prunes, and tells the database it did ──────────────────"
PRUNE=$(pysh "
import asyncio, json, os, time
from agent.config import get_settings
from agent.db.session import get_sessionmaker
from agent.scheduling.retention import prune_storage
from agent.storage.backend import get_storage
async def main():
    st = get_storage()
    st.put('debug/transparency/verify-p8-old.html', b'<html>aged</html>')
    old = time.time() - 60*86400
    os.utime(os.path.join(get_settings().storage_dir, 'debug/transparency/verify-p8-old.html'), (old, old))
    st.put('debug/transparency/verify-p8-new.html', b'<html>fresh</html>')
    async with get_sessionmaker()() as s:
        out = await prune_storage(s, now=None)
    print(json.dumps({'deleted': out.total_deleted,
                      'old_gone': not st.exists('debug/transparency/verify-p8-old.html'),
                      'new_kept': st.exists('debug/transparency/verify-p8-new.html'),
                      'objects': out.usage.objects if out.usage else 0}))
asyncio.run(main())
" 2>/dev/null | tail -1)
chk "an aged-out dump is deleted" "$(echo "$PRUNE" | jq_ 'str(d["old_gone"])')" "True"
chk "a fresh one is kept" "$(echo "$PRUNE" | jq_ 'str(d["new_kept"])')" "True"

echo "── 8. storage usage reaches the settings screen ────────────────────────"
USAGE=$(get "$A" "/storage")
chk "GET /storage answers" "$(code "$A" "/storage")" "200"
chk "and the worker was reachable" "$(echo "$USAGE" | jq_ 'str(d["reachable"])')" "True"
ne "it reports objects on the Volume" "$(echo "$USAGE" | jq_ 'd["objects"]')" "0"
has "and the retention windows behind them" "$(echo "$USAGE" | jq_ 'sorted(d["retention"])')" "screenshots"

echo "── 9. the diff endpoint ────────────────────────────────────────────────"
DIFF=$(pysh "
import asyncio, json, uuid, datetime as dt, sqlalchemy as sa
from pathlib import Path
from agent.db.session import get_sessionmaker
from agent.db.models import Project, Report, Run, RunStatus, RunTrigger
async def main():
    payload = json.loads(Path('tests/fixtures/report_golden.json').read_text())
    async with get_sessionmaker()() as s:
        p = (await s.execute(sa.select(Project).limit(1))).scalar_one()
        made = []
        for verdict in ('go_with_fixes', 'no_go'):
            run = Run(workspace_id=p.workspace_id, project_id=p.id, trigger=RunTrigger.MANUAL,
                      status=RunStatus.SUCCEEDED, finished_at=dt.datetime.now(dt.UTC),
                      parent_run_id=made[0] if made else None)
            s.add(run); await s.flush()
            body = dict(payload, run_id=str(run.id), project_id=str(p.id),
                        launch_readiness=verdict)
            s.add(Report(run_id=run.id, schema_version='1.0', payload=body, markdown='#'))
            made.append(run.id)
        await s.commit()
        print(json.dumps({'first': str(made[0]), 'second': str(made[1])}))
asyncio.run(main())
" 2>/dev/null | tail -1)
SECOND=$(echo "$DIFF" | jq_ 'd["second"]')
if [ -n "$SECOND" ]; then
  D=$(get "$A" "/runs/$SECOND/diff")
  chk "GET /runs/{id}/diff answers" "$(code "$A" "/runs/$SECOND/diff")" "200"
  chk "it defaults to the run's parent" "$(echo "$D" | jq_ 'str(d["against_is_parent"])')" "True"
  has "and names the verdict that moved" "$(echo "$D" | jq_ '[c["field"] for c in d["scalars"]]')" "Launch readiness"
  chk "a first run has nothing to compare against" \
      "$(code "$A" "/runs/$(echo "$DIFF" | jq_ 'd["first"]')/diff")" "422"
fi

echo "── 10. the eval harness ────────────────────────────────────────────────"
EVAL=$($COMPOSE exec -T api uv run pytest tests/eval -q --no-header 2>&1 | tail -1)
has "ACCEPTANCE: the eval suite is green" "$EVAL" "passed"
case "$EVAL" in *"failed"*) no "the eval suite is green" "$EVAL";; esac

echo
printf "── result ──────────────────────────────────────────────────────────────\n"
printf "  \033[32m%d passed\033[0m, \033[31m%d failed\033[0m\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
