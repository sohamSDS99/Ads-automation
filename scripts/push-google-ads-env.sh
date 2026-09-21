#!/usr/bin/env bash
# Push the Google Ads credentials from a local `.env` to Railway.
#
# `google-ads-oauth.py --write-env` leaves five values in `.env` (gitignored).
# The connector reads them from the environment and from nowhere else (#29),
# and it runs on *both* `api` and `worker` — the worker is what executes a
# research run, so setting them on `api` alone connects nothing.
#
# The values are read from your `.env` and handed straight to the Railway CLI.
# They are never printed, so this is safe to run with someone watching.
#
#     ./scripts/push-google-ads-env.sh
#
# One invocation per service with every `--set` at once, so each service
# redeploys once rather than six times.
set -euo pipefail

PROJECT="69f17cde-5056-43fb-ba1f-685fa2d10172"
ENVIRONMENT="production"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"

REQUIRED=(
  GOOGLE_ADS_DEVELOPER_TOKEN
  GOOGLE_ADS_CLIENT_ID
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_CUSTOMER_ID
)
# Only when the account sits under a manager account. Absent is meaningful:
# sending an empty one makes Google reject the call.
OPTIONAL=(GOOGLE_ADS_LOGIN_CUSTOMER_ID)

[ -f "$ENV_FILE" ] || { echo "no $ENV_FILE — run scripts/google-ads-oauth.py --write-env first" >&2; exit 1; }

declare -a ARGS=()
missing=()
for key in "${REQUIRED[@]}" "${OPTIONAL[@]}"; do
  # Last assignment wins, matching how the oauth script rewrites the file.
  value="$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
  if [ -z "$value" ]; then
    case " ${REQUIRED[*]} " in *" $key "*) missing+=("$key");; esac
    continue
  fi
  ARGS+=(--set "${key}=${value}")
  echo "  will set ${key} (${#value} chars)"
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "missing from $ENV_FILE: ${missing[*]}" >&2
  exit 1
fi

for service in api worker; do
  echo "→ $service"
  railway variables --project "$PROJECT" --environment "$ENVIRONMENT" --service "$service" "${ARGS[@]}"
done

echo
echo "Both services are redeploying. When they are up:"
echo "  ./scripts/verify-google-ads.sh      # proves the call against the live API"
