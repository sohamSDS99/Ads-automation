#!/usr/bin/env bash
# S4-P23 browser check: a production build of the web tier against the REAL
# api, the REAL file server and the REAL arq worker on the isolated `s4p23`
# stack, on the four runs `seed.py` makes (one project, packages A–D).
#
#   apps/web$ scripts/s4p23/run.sh                    # fresh stack, seed, build, serve, check
#   apps/web$ SKIP_BUILD=1 scripts/s4p23/run.sh
#   apps/web$ UPDATE_BASELINES=1 scripts/s4p23/run.sh  # re-record tests/visual/s4p23
#
# The stack starts empty every time (`down -v`, this project's volumes only):
# the check RELEASES package A as v1 and B as v2, so a second pass on the same
# volume would find nothing to release. Both rewrite targets are baked in at
# build time: /api/v1 → 127.0.0.1:8623 and /files → 127.0.0.1:8624.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

# Every name, port and subnet below is overridable, so a second copy of this
# stack can run beside a live one (S4-P24's `make browser-stage-04` runs it as
# `s4p24-23`): S4_PROJECT, S4_API_PORT, S4_FILES_PORT (file server),
# S4_AUX_PORT (catalogue), S4_SUBNET, S4_SUBNET6 and WEB_PORT. The compose file
# and the check read the same variables. S4_TEARDOWN=1 takes the stack down
# (`down -v`) on exit.
export S4_PROJECT="${S4_PROJECT:-s4p23}"
export S4_API_PORT="${S4_API_PORT:-8623}"
export S4_FILES_PORT="${S4_FILES_PORT:-8624}"
export S4_AUX_PORT="${S4_AUX_PORT:-8625}"
export S4_SUBNET="${S4_SUBNET:-172.31.223.0/24}"
export S4_SUBNET6="${S4_SUBNET6:-fd00:ada:223::/64}"
export WEB_PORT="${WEB_PORT:-3623}"
export API_INTERNAL_URL="http://127.0.0.1:${S4_API_PORT}"
export WORKER_INTERNAL_URL="http://127.0.0.1:${S4_FILES_PORT}"
export API_URL="${API_INTERNAL_URL}/api/v1"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/${S4_PROJECT}-shots}"
compose=(docker compose -p "$S4_PROJECT" -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p23/compose.s4p23.yml")
web=""
cleanup() {
  # `pnpm exec` does not pass the signal on: its `next start` child goes too.
  if [[ -n "$web" ]]; then pkill -TERM -P "$web" 2>/dev/null || true; kill "$web" 2>/dev/null || true; fi
  if [[ -n "${S4_TEARDOWN:-}" ]]; then "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

pnpm test:unit

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d postgres redis catalogue api worker
for _ in $(seq 1 60); do
  "${compose[@]}" exec -T postgres pg_isready -U agent >/dev/null 2>&1 && break
  sleep 1
done
"${compose[@]}" exec -T api alembic upgrade head
# Bootstrap runs before migrations on a fresh volume; a restart re-runs it.
"${compose[@]}" restart api >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "${API_URL}/health" >/dev/null 2>&1 && break
  sleep 1
done

SEED="$("${compose[@]}" exec -T worker python /app/s4p23/seed.py | tail -n 1)"
export SEED
echo "seeded: $SEED"

# Release writes the package files through the worker's queue, so the arq
# consumer starts now — in the worker container, on the Volume the file
# server serves. The seed's in-process runs enqueued their own jobs; they are
# finished, and the queue they filled is emptied first so nothing re-runs.
"${compose[@]}" exec -T redis redis-cli -n 0 DEL arq:queue >/dev/null
# arq carries its own file server; on 8082 it leaves 8081 to the one api reads.
"${compose[@]}" exec -d -e FILE_SERVER_PORT=8082 worker arq agent.worker.WorkerSettings
for _ in $(seq 1 30); do
  "${compose[@]}" exec -T redis redis-cli -n 0 --scan --pattern 'arq:*health-check*' | grep -q . && break
  sleep 1
done

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >"/tmp/${S4_PROJECT}-web.log" 2>&1 &
web=$!

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p23/browser-check.mjs
