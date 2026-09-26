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
# at runtime). Chromium is the installed Chrome for Testing (`chromiumPath()`).
set -euo pipefail
cd "$(dirname "$0")/../.."
REPO="$(cd ../.. && pwd)"

# Every name, port and subnet below is overridable, so a second copy of this
# stack can run beside a live one (S4-P24's `make browser-stage-04` runs it as
# `s4p24-03`): S4_PROJECT, S4_API_PORT, S4_AUX_PORT (catalogue), S4_SUBNET,
# S4_SUBNET6 and WEB_PORT. The compose file and the check read the same
# variables. S4_TEARDOWN=1 takes the stack down (`down -v`) on exit.
export S4_PROJECT="${S4_PROJECT:-s4p3}"
export S4_API_PORT="${S4_API_PORT:-8133}"
export S4_AUX_PORT="${S4_AUX_PORT:-8134}"
export S4_SUBNET="${S4_SUBNET:-172.31.233.0/24}"
export S4_SUBNET6="${S4_SUBNET6:-fd00:ada:33::/64}"
export WEB_PORT="${WEB_PORT:-3133}"
export API_INTERNAL_URL="http://127.0.0.1:${S4_API_PORT}"
export API_URL="${API_INTERNAL_URL}/api/v1"
export CATALOGUE_URL="http://127.0.0.1:${S4_AUX_PORT}"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/${S4_PROJECT}-shots}"
compose=(docker compose -p "$S4_PROJECT" -f "$REPO/docker-compose.yml" -f "$REPO/apps/web/scripts/s4p3/compose.s4p3.yml")
web=""
cleanup() {
  # `pnpm exec` does not pass the signal on: its `next start` child goes too.
  if [[ -n "$web" ]]; then pkill -TERM -P "$web" 2>/dev/null || true; kill "$web" 2>/dev/null || true; fi
  if [[ -n "${S4_TEARDOWN:-}" ]]; then "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

# The visual baselines (S4-P24) see the same sheet on every pass: the stack
# starts empty (`down -v`, this project's volumes only), as S4-P18…P23's do.
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

"${compose[@]}" up -d postgres redis catalogue api
"${compose[@]}" exec -T api alembic upgrade head
# Bootstrap runs before migrations on a fresh volume; a restart re-runs it.
"${compose[@]}" restart api >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "${API_URL}/health" >/dev/null 2>&1 && break
  sleep 1
done

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >"/tmp/${S4_PROJECT}-web.log" 2>&1 &
web=$!

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

REPO="$REPO" BASE_URL="http://127.0.0.1:${WEB_PORT}" node scripts/s4p3/browser-check.mjs
