#!/usr/bin/env bash
# PRD §19.1's acceptance list, run against a live stack.
#
# Everything goes through http://localhost:3000 — the browser's path, not the
# API's. `api` has no ingress, so a pass here means the whole chain works
# (browser -> web rewrite -> private network -> FastAPI), which is more than the
# test suite can prove from inside the process.
#
#   make up && ./scripts/verify-p0b.sh
#
# Idempotent: every address it creates is unique per run.
set -uo pipefail
B=http://localhost:3000/api/v1
J=$(mktemp); trap 'rm -f "$J"' EXIT
PASS=0; FAIL=0
ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
no(){ printf "  \033[31mFAIL\033[0m %s — %s\n" "$1" "${2:-}"; FAIL=$((FAIL+1)); }
chk(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "expected $3, got $2"; }

csrf(){ curl -s -c "$1" -b "$1" "$B/auth/csrf" >/dev/null; grep -E '\scsrf\s' "$1" | awk '{print $7}'; }
code(){ printf '%s' "$1" | tail -1; }

echo "── 1. bootstrap is closed ──────────────────────────────────────────────"
C=$(mktemp); T=$(csrf "$C")
S=$(curl -s -o /dev/null -w '%{http_code}' -c "$C" -b "$C" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' -d '{}' "$B/auth/bootstrap")
chk "POST /auth/bootstrap on a bootstrapped workspace -> 409" "$S" "409"

echo "── 2. CSRF ─────────────────────────────────────────────────────────────"
S=$(curl -s -o /dev/null -w '%{http_code}' -c "$C" -b "$C" -H 'Content-Type: application/json' \
      -d '{"email":"admin@example.com","password":"change-me-at-least-12-chars"}' "$B/auth/login")
chk "POST without X-CSRF-Token -> 403" "$S" "403"

echo "── 3. admin signs in ───────────────────────────────────────────────────"
A=$(mktemp); T=$(csrf "$A")
BODY=$(curl -s -c "$A" -b "$A" -H "X-CSRF-Token: $T" -H 'Content-Type: application/json' \
      -d '{"email":"admin@example.com","password":"change-me-at-least-12-chars"}' "$B/auth/login")
ROLE=$(printf '%s' "$BODY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["role"])' 2>/dev/null)
chk "POST /auth/login as admin -> role admin" "$ROLE" "admin"
PERMS=$(curl -s -b "$A" "$B/auth/me" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["permissions"]))')
chk "GET /auth/me -> 8 permissions" "$PERMS" "8"
grep -q "ara_session" "$A" && ok "session cookie set" || no "session cookie set"

echo "── 4. invite one user of each role, each accepts ───────────────────────"
# macOS ships bash 3.2, which has no associative arrays.
expected_perms(){ case "$1" in admin) echo 8;; operator) echo 3;; approver) echo 2;; viewer) echo 1;; esac; }
for ROLE_N in admin operator approver viewer; do
  EMAIL="verify-$ROLE_N-$RANDOM@example.com"
  AT=$(grep -E '\scsrf\s' "$A" | awk '{print $7}')
  INV=$(curl -s -c "$A" -b "$A" -H "X-CSRF-Token: $AT" -H 'Content-Type: application/json' \
        -d "{\"email\":\"$EMAIL\",\"name\":\"Verify $ROLE_N\",\"role\":\"$ROLE_N\"}" "$B/users/invite")
  LINK=$(printf '%s' "$INV" | python3 -c 'import sys,json;print(json.load(sys.stdin)["link"])' 2>/dev/null)
  if [ -z "$LINK" ]; then no "invite $ROLE_N" "$INV"; continue; fi
  TOKEN="${LINK##*/}"

  U=$(mktemp); UT=$(csrf "$U")
  PREV=$(curl -s -b "$U" "$B/invites/$TOKEN" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')
  chk "GET /invites/{token} ($ROLE_N) -> valid" "$PREV" "valid"
  ACC=$(curl -s -c "$U" -b "$U" -H "X-CSRF-Token: $UT" -H 'Content-Type: application/json' \
        -d '{"name":"Verified Person","password":"quarry-lantern-98-fog"}' "$B/invites/$TOKEN/accept")
  GOT=$(printf '%s' "$ACC" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["role"] + ":" + str(len(d["permissions"])))' 2>&1)
  chk "accept as $ROLE_N -> PRD §4.1 permission set" "$GOT" "$ROLE_N:$(expected_perms "$ROLE_N")"

  # A replay of the same link must be refused.
  R=$(mktemp); RT=$(csrf "$R")
  S=$(curl -s -o /dev/null -w '%{http_code}' -c "$R" -b "$R" -H "X-CSRF-Token: $RT" -H 'Content-Type: application/json' \
        -d '{"name":"Replay","password":"quarry-lantern-98-fog"}' "$B/invites/$TOKEN/accept")
  chk "re-using the $ROLE_N link -> 410" "$S" "410"
  rm -f "$U" "$R"

  if [ "$ROLE_N" = "operator" ]; then OPERATOR_EMAIL=$EMAIL; fi
  if [ "$ROLE_N" = "viewer" ]; then VIEWER_EMAIL=$EMAIL; fi
done

echo "── 5. a viewer is refused every write ──────────────────────────────────"
V=$(mktemp); VT=$(csrf "$V")
curl -s -o /dev/null -c "$V" -b "$V" -H "X-CSRF-Token: $VT" -H 'Content-Type: application/json' \
     -d "{\"email\":\"$VIEWER_EMAIL\",\"password\":\"quarry-lantern-98-fog\"}" "$B/auth/login"
VT=$(grep -E '\scsrf\s' "$V" | awk '{print $7}')
for CALL in "PATCH:/workspace:{\"name\":\"Hijacked\"}" "POST:/users/invite:{\"email\":\"x@y.co\",\"name\":\"X\",\"role\":\"viewer\"}"; do
  M=${CALL%%:*}; REST=${CALL#*:}; P=${REST%%:*}; D=${REST#*:}
  S=$(curl -s -o /dev/null -w '%{http_code}' -b "$V" -X "$M" -H "X-CSRF-Token: $VT" -H 'Content-Type: application/json' -d "$D" "$B$P")
  chk "viewer $M $P -> 403" "$S" "403"
done
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$V" "$B/audit"); chk "viewer GET /audit -> 403" "$S" "403"
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$V" "$B/auth/me"); chk "viewer GET /auth/me -> 200" "$S" "200"
NAME=$(curl -s -b "$A" "$B/workspace" | python3 -c 'import sys,json;print(json.load(sys.stdin)["name"])')
chk "the refused PATCH wrote nothing" "$NAME" "Research Workspace"

echo "── 6. disabling a user kills their session ─────────────────────────────"
O=$(mktemp); OT=$(csrf "$O")
curl -s -o /dev/null -c "$O" -b "$O" -H "X-CSRF-Token: $OT" -H 'Content-Type: application/json' \
     -d "{\"email\":\"$OPERATOR_EMAIL\",\"password\":\"quarry-lantern-98-fog\"}" "$B/auth/login"
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$O" "$B/auth/me"); chk "operator signed in -> 200" "$S" "200"
OID=$(curl -s -b "$A" "$B/users" | python3 -c "import sys,json;print(next(u['id'] for u in json.load(sys.stdin)['users'] if u['email']=='$OPERATOR_EMAIL'))")
AT=$(grep -E '\scsrf\s' "$A" | awk '{print $7}')
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$A" -X PATCH -H "X-CSRF-Token: $AT" -H 'Content-Type: application/json' \
     -d '{"status":"disabled"}' "$B/users/$OID")
chk "admin disables the operator -> 200" "$S" "200"
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$O" "$B/auth/me"); chk "their next request -> 401" "$S" "401"

echo "── 7. the last active admin is protected ───────────────────────────────"
AID=$(curl -s -b "$A" "$B/auth/me" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
# Demote the admin we invited in step 4 first, so only one remains.
for E in $(curl -s -b "$A" "$B/users" | python3 -c 'import sys,json;[print(u["id"]) for u in json.load(sys.stdin)["users"] if u["role"]=="admin" and u["id"]!="'"$AID"'"]'); do
  AT=$(grep -E '\scsrf\s' "$A" | awk '{print $7}')
  curl -s -o /dev/null -b "$A" -X PATCH -H "X-CSRF-Token: $AT" -H 'Content-Type: application/json' -d '{"role":"viewer"}' "$B/users/$E"
done
AT=$(grep -E '\scsrf\s' "$A" | awk '{print $7}')
S=$(curl -s -o /dev/null -w '%{http_code}' -b "$A" -X PATCH -H "X-CSRF-Token: $AT" -H 'Content-Type: application/json' \
     -d '{"role":"operator"}' "$B/users/$AID")
chk "demoting the last active admin -> 409" "$S" "409"
STILL=$(curl -s -b "$A" "$B/auth/me" | python3 -c 'import sys,json;print(json.load(sys.stdin)["role"])')
chk "the row is unchanged" "$STILL" "admin"

echo "── 8. lockout ──────────────────────────────────────────────────────────"
L=$(mktemp); LT=$(csrf "$L"); LOCK_EMAIL="lockme-$RANDOM-$RANDOM@example.com"
for i in 1 2 3 4 5; do
  S=$(curl -s -o /dev/null -w '%{http_code}' -b "$L" -H "X-CSRF-Token: $LT" -H 'Content-Type: application/json' \
       -d "{\"email\":\"$LOCK_EMAIL\",\"password\":\"wrong-password-here\"}" "$B/auth/login")
  [ "$S" = "401" ] || no "attempt $i -> 401" "got $S"
done
RESP=$(curl -s -w '\n%{http_code}' -b "$L" -H "X-CSRF-Token: $LT" -H 'Content-Type: application/json' \
       -d "{\"email\":\"$LOCK_EMAIL\",\"password\":\"wrong-password-here\"}" "$B/auth/login")
S=$(printf '%s' "$RESP" | tail -1)
chk "the 6th attempt -> 429" "$S" "429"
printf '%s' "$RESP" | sed '$d' | grep -q 'rate-limited' && ok "problem+json names the lockout" || no "problem+json names the lockout"

echo "── 9. security headers ─────────────────────────────────────────────────"
H=$(curl -s -D- -o /dev/null "$B/health")
for HDR in "x-content-type-options: nosniff" "x-frame-options: DENY" "referrer-policy: no-referrer"; do
  printf '%s' "$H" | grep -qi "$HDR" && ok "$HDR" || no "$HDR"
done

echo "── 10. no secret ever reaches a response ───────────────────────────────"
ALL=$(curl -s -b "$A" "$B/auth/me"; curl -s -b "$A" "$B/users"; curl -s -b "$A" "$B/auth/sessions"; curl -s -b "$A" "$B/audit")
for BAD in 'password_hash' '$argon2' 'token_hash' 'change-me-at-least-12-chars' 'quarry-lantern-98-fog'; do
  printf '%s' "$ALL" | grep -qF "$BAD" && no "response contains $BAD" || ok "no '$BAD' in any response"
done

rm -f "$C" "$A" "$V" "$O" "$L"
echo
printf "═══ %d passed, %d failed ═══\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
