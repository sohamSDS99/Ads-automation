#!/usr/bin/env bash
# S4-P22 browser check: a production build of the web tier against the REAL
# api and the REAL file server on the isolated `s4p22` stack, on the four runs
# `seed.py` makes (G8 with 20 assets, G8b, two H3 sets).
#
#   apps/web$ scripts/s4p22/run.sh                    # fresh stack, seed, build, serve, check
#   apps/web$ SKIP_BUILD=1 scripts/s4p22/run.sh
#   apps/web$ UPDATE_BASELINES=1 scripts/s4p22/run.sh  # re-record tests/visual/s4p22
#
# The stack starts empty every time (`down -v`, this project's volumes only),
# so the baselines see the same runs on every pass. Both rewrite targets are
# baked in at build time: /api/v1 → 127.0.0.1:8522 and /files → 127.0.0.1:8523.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

WEB_PORT="${WEB_PORT:-3522}"
export API_INTERNAL_URL="http://127.0.0.1:8522"
export WORKER_INTERNAL_URL="http://127.0.0.1:8523"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/s4p22-shots}"
compose=(docker compose -p s4p22 -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p22/compose.s4p22.yml")

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
  curl -fsS "http://127.0.0.1:8522/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done

SEED="$("${compose[@]}" exec -T worker python /app/s4p22/seed.py | tail -n 1)"
export SEED
echo "seeded: $SEED"

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >/tmp/s4p22-web.log 2>&1 &
web=$!
trap 'kill $web 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p22/browser-check.mjs
