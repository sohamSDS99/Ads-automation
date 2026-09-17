#!/usr/bin/env bash
# What ends a session, proved against a running stack.
#
#   "once I am logged in, I will be logged in until I change my decision"
#
# The unit suite asserts the TTL *decision* against a Redis double. This asserts
# the consequence against real Redis, through the browser's own path — because
# the two ways this could still be wrong are both invisible to a double: a
# cookie that expires before the session does, and a revocation that stopped
# working when the expiry was removed.
#
#   make up && ./scripts/verify-session.sh
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
jq_(){ python3 -c "import sys,json;d=json.load(sys.stdin);print($1)" 2>/dev/null; }

A=$(mktemp); HEADERS=$(mktemp)
trap 'rm -f "$A" "$HEADERS"' EXIT
csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
redis_(){ $COMPOSE exec -T redis redis-cli "$@" | tr -d '\r'; }
sid_of(){ grep -E "\sara_session\s" "$1" | awk '{print $7}'; }

echo "── 1. signing in ───────────────────────────────────────────────────────"
T=$(csrf "$A")
ROLE=$(curl -s -D "$HEADERS" -c "$A" -b "$A" -H "X-CSRF-Token: $T" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" "$B/auth/login" | jq_ 'd["role"]')
chk "POST /auth/login as admin" "$ROLE" "admin"
[ "$PASS" -ge 1 ] || { echo "cannot continue without a session"; exit 1; }

SID=$(sid_of "$A")
[ -n "$SID" ] && ok "the browser was handed a session cookie" || { no "no session cookie"; exit 1; }

echo "── 2. neither cookie expires before the other ──────────────────────────"
# The bug this catches: the CSRF cookie used to run for 12 hours against the
# session cookie's 30 days, so a browser could hold a valid session and no way
# to make a write with it.
#
# `sed`, not `grep -o '…[^\r\n]*'`: inside a bracket expression BSD grep reads
# `\r` as the two characters backslash and r, so that pattern also excludes
# every literal `r` and `n` and stops matching mid-header. It returns empty
# rather than wrong, which is the failure mode that looks like a real defect.
max_age(){ grep -i "set-cookie: $1=" "$HEADERS" | sed -n 's/.*[Mm]ax-[Aa]ge=\([0-9]*\).*/\1/p' | head -1; }
SESSION_AGE=$(max_age ara_session)
CSRF_AGE=$(max_age csrf)
chk "the session and CSRF cookies share one lifetime" "$SESSION_AGE" "$CSRF_AGE"
chk "…and it is the 400 days Chrome will actually keep" "$SESSION_AGE" "34560000"

echo "── 3. the session itself has no expiry ─────────────────────────────────"
# -1 is redis-cli for "this key has no TTL". -2 would be "no such key".
chk "the Redis key carries no TTL" "$(redis_ ttl "session:$SID")" "-1"
chk "…and neither does the index that revocation walks" \
    "$(redis_ ttl "$(redis_ --scan --pattern 'user_sessions:*' | head -1)")" "-1"

echo "── 4. using it does not put one back ───────────────────────────────────"
# `touch` rewrites the key once a minute. The rewrite is where a TTL would
# reappear, so the check has to happen after one.
chk "an authenticated read works" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" "$B/auth/me")" "200"
printf "  …waiting out the 60s touch interval"
for _ in $(seq 1 13); do sleep 5; printf "."; done; printf "\n"
chk "a later read works too" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" "$B/auth/me")" "200"
chk "the key was rewritten and still has no TTL" "$(redis_ ttl "session:$SID")" "-1"

echo "── 5. what does still end it ───────────────────────────────────────────"
# This is the half that makes the other half safe. If removing the clocks had
# broken revocation, the session would be immortal rather than long-lived.
T=$(csrf "$A")
chk "POST /auth/logout answers 204" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" -X POST -H "X-CSRF-Token: $T" "$B/auth/logout")" \
    "204"
chk "…and the session is gone from Redis" "$(redis_ exists "session:$SID")" "0"
chk "…and the cookie no longer authenticates" \
    "$(curl -s -o /dev/null -w '%{http_code}' -b "$A" "$B/auth/me")" "401"

echo
printf "%d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
