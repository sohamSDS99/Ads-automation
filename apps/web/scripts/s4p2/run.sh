#!/usr/bin/env bash
# S4-P2 browser check: a production build against the fixture API, no backend.
#
#   apps/web$ scripts/s4p2/run.sh            # build, serve, check, stop
#   apps/web$ SKIP_BUILD=1 scripts/s4p2/run.sh
#
# The rewrite target is baked into the build (output: standalone), so the
# build and the server both get API_INTERNAL_URL; server-session.ts reads it
# again at runtime. Uses the full Chromium build already on disk
# (channel: "chromium") rather than downloading a headless shell.
set -euo pipefail
cd "$(dirname "$0")/../.."

STUB_PORT="${STUB_PORT:-18410}"
WEB_PORT="${WEB_PORT:-3410}"
export API_INTERNAL_URL="http://127.0.0.1:${STUB_PORT}"
export NEXT_TELEMETRY_DISABLED=1
export SHOTS="${SHOTS:-/tmp/s4p2-shots}"

if [[ -z "${SKIP_BUILD:-}" ]]; then pnpm build; fi

STUB_PORT="$STUB_PORT" node scripts/s4p2/stub-api.mjs &
stub=$!
pnpm exec next start -p "$WEB_PORT" -H 127.0.0.1 >/tmp/s4p2-web.log 2>&1 &
web=$!
trap 'kill $stub $web 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${WEB_PORT}/login" >/dev/null 2>&1 && break
  sleep 0.5
done

BASE_URL="http://127.0.0.1:${WEB_PORT}" STUB_URL="http://127.0.0.1:${STUB_PORT}" \
  node scripts/s4p2/browser-check.mjs
