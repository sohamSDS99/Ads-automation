#!/usr/bin/env bash
# S4-P3 browser check: a production build of the web tier against the REAL api
# (Postgres, Redis, S4-P0/P1 routes) on the isolated `s4p3` compose stack, with
# OpenRouter's catalogue served from S4-P1's recordings.
#
#   apps/web$ scripts/s4p3/run.sh            # stack up, build, serve, check
#   apps/web$ SKIP_BUILD=1 scripts/s4p3/run.sh
#
# The api publishes 127.0.0.1:8133 (compose.s4p3.yml), and the web rewrite
# target is baked in at build time (output: standalone), so API_INTERNAL_URL
# is set for the build and again for `next start` (server-session.ts reads it
# at runtime). Chromium is the full build on disk (channel: "chromium").
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

WEB_PORT="${WEB_PORT:-3133}"
export API_INTERNAL_URL="http://127.0.0.1:8133"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/s4p3-shots}"
compose=(docker compose -p s4p3 -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p3/compose.s4p3.yml")

"${compose[@]}" up -d postgres redis catalogue api
"${compose[@]}" exec -T api alembic upgrade head
# Bootstrap runs before migrations on a fresh volume; a restart re-runs it.
"${compose[@]}" restart api >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:8133/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >/tmp/s4p3-web.log 2>&1 &
web=$!
trap 'kill $web 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p3/browser-check.mjs
