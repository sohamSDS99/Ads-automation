#!/usr/bin/env bash
# S4-P19 browser check: a production build of the web tier against the REAL
# api on the isolated `s4p19` stack, on a run the real executor takes through
# 4.2.1–4.2.4 with S4-P6's scripted model answers (`openrouter_server.py`).
#
#   apps/web$ scripts/s4p19/run.sh                    # fresh stack, build, serve, check
#   apps/web$ SKIP_BUILD=1 scripts/s4p19/run.sh
#   apps/web$ UPDATE_BASELINES=1 scripts/s4p19/run.sh  # re-record tests/visual/s4p19
#
# The stack starts empty every time (`down -v`, this project's volumes only),
# so the visual baselines see the same run on every pass. The api publishes
# 127.0.0.1:8419 and the web rewrite target is baked in at build time, so
# API_INTERNAL_URL is set for the build and again for `next start`. No worker
# runs: the run is text-only and `seed.py execute` runs it in-process.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

WEB_PORT="${WEB_PORT:-3419}"
export API_INTERNAL_URL="http://127.0.0.1:8419"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/s4p19-shots}"
compose=(docker compose -p s4p19 -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p19/compose.s4p19.yml")

# The counter parity test (§15.5 item 1) is part of this phase's check.
pnpm test:unit

"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${compose[@]}" up -d postgres redis openrouter api
for _ in $(seq 1 60); do
  "${compose[@]}" exec -T postgres pg_isready -U agent >/dev/null 2>&1 && break
  sleep 1
done
"${compose[@]}" exec -T api alembic upgrade head
# Bootstrap runs before migrations on a fresh volume; a restart re-runs it.
"${compose[@]}" restart api >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:8419/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >/tmp/s4p19-web.log 2>&1 &
web=$!
trap 'kill $web 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p19/browser-check.mjs
