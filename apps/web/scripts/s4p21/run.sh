#!/usr/bin/env bash
# S4-P21 browser check: a production build of the web tier against the REAL
# api and the REAL file server on the isolated `s4p21` stack, on the three runs
# `seed.py` makes (images, video, 500 tiles).
#
#   apps/web$ scripts/s4p21/run.sh                    # fresh stack, seed, build, serve, check
#   apps/web$ SKIP_BUILD=1 scripts/s4p21/run.sh
#   apps/web$ UPDATE_BASELINES=1 scripts/s4p21/run.sh  # re-record tests/visual/s4p21
#
# The stack starts empty every time (`down -v`, this project's volumes only),
# so the baselines see the same runs on every pass. Both rewrite targets are
# baked in at build time: /api/v1 → 127.0.0.1:8421 and /files → 127.0.0.1:8423.
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

# Every name, port and subnet below is overridable, so a second copy of this
# stack can run beside a live one (S4-P24's `make browser-stage-04` runs it as
# `s4p24-21`): S4_PROJECT, S4_API_PORT, S4_FILES_PORT (file server),
# S4_AUX_PORT (catalogue), S4_SUBNET, S4_SUBNET6 and WEB_PORT. The compose file
# and the check read the same variables. S4_TEARDOWN=1 takes the stack down
# (`down -v`) on exit.
export S4_PROJECT="${S4_PROJECT:-s4p21}"
export S4_API_PORT="${S4_API_PORT:-8421}"
export S4_FILES_PORT="${S4_FILES_PORT:-8423}"
export S4_AUX_PORT="${S4_AUX_PORT:-8422}"
export S4_SUBNET="${S4_SUBNET:-172.31.221.0/24}"
export S4_SUBNET6="${S4_SUBNET6:-fd00:ada:221::/64}"
export WEB_PORT="${WEB_PORT:-3421}"
export API_INTERNAL_URL="http://127.0.0.1:${S4_API_PORT}"
export WORKER_INTERNAL_URL="http://127.0.0.1:${S4_FILES_PORT}"
export API_URL="${API_INTERNAL_URL}/api/v1"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/${S4_PROJECT}-shots}"
compose=(docker compose -p "$S4_PROJECT" -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p21/compose.s4p21.yml")
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

SEED="$("${compose[@]}" exec -T worker python /app/s4p21/seed.py | tail -n 1)"
export SEED
echo "seeded: $SEED"

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >"/tmp/${S4_PROJECT}-web.log" 2>&1 &
web=$!

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p21/browser-check.mjs
