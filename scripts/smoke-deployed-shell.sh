#!/usr/bin/env bash
# Verify the public, non-activating health and sign-in shell after a deployment.

set -euo pipefail

origin="${1:?usage: scripts/smoke-deployed-shell.sh https://SERVICE.run.app}"
origin="${origin%/}"
: "${SMOKE_EMAIL:?set SMOKE_EMAIL to an allowlisted founder inbox}"
: "${SMOKE_PREVIEW_ACCESS_SECRET:?set SMOKE_PREVIEW_ACCESS_SECRET securely}"
cookie_jar="$(mktemp)"
response_file="$(mktemp)"
trap 'rm -f "$cookie_jar" "$response_file"' EXIT

curl --fail --silent --show-error "$origin/live" | grep -Fx '{"status":"live"}'
curl --fail --silent --show-error "$origin/ready" | grep -Fx '{"status":"ready"}'

page="$(curl --fail --silent --show-error --cookie-jar "$cookie_jar" "$origin/sign-in")"
csrf_token="$(printf '%s' "$page" | sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p')"
test -n "$csrf_token"

rejected_status="$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
  --cookie "$cookie_jar" --cookie-jar "$cookie_jar" \
  --data-urlencode "csrf_token=$csrf_token" \
  --data-urlencode "email=$SMOKE_EMAIL" \
  "$origin/auth/sign-in")"
test "$rejected_status" = "202"
grep -Fq "Check your email" "$response_file"

page="$(curl --fail --silent --show-error --cookie-jar "$cookie_jar" "$origin/sign-in")"
csrf_token="$(printf '%s' "$page" | sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p')"
test -n "$csrf_token"

admitted_status="$(printf '%s' "$SMOKE_PREVIEW_ACCESS_SECRET" | \
  curl --silent --show-error --output "$response_file" --write-out '%{http_code}' \
    --cookie "$cookie_jar" --cookie-jar "$cookie_jar" \
    --data-urlencode "csrf_token=$csrf_token" \
    --data-urlencode "email=$SMOKE_EMAIL" \
    --data-urlencode preview_access_secret@- \
    "$origin/auth/sign-in")"
test "$admitted_status" = "202"
grep -Fq "Check your email" "$response_file"

printf 'Health and generic rejected/admitted sign-in responses verified.\n'
printf 'Confirm exactly one new message arrived at the allowlisted founder inbox.\n'
