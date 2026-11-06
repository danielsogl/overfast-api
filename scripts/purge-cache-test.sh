#!/bin/bash
# scripts/purge-static-cache.sh must drop exactly the CSV-backed API entries.
#
# The entries are created by real requests through nginx and the app, not
# seeded by hand: a test that writes keys in the format it expects proves only
# that the pattern matches that format.
#
# Expects the compose stack to be up (e.g. after scripts/smoke-test.sh).
set -euo pipefail

APP_PORT="${APP_PORT:-8080}"
BASE_URL="${BASE_URL:-http://localhost:$APP_PORT}"

vcli() { docker compose exec -T valkey valkey-cli "$@" | tr -d '\r'; }

echo "=== Static cache purge on keys written by the app ==="
DROP=("/maps" "/maps?gamemode=push" "/gamemodes" "/heroes")
KEEP=("/roles")
for path in "${DROP[@]}" "${KEEP[@]}"; do
    curl -sf -o /dev/null "$BASE_URL$path"
done

echo "  api-cache keys before the purge:"
vcli --scan --pattern 'api-cache:*' | sort | sed 's/^/    /'

ERRORS=0
for path in "${DROP[@]}" "${KEEP[@]}"; do
    [ "$(vcli EXISTS "api-cache:$path")" = "1" ] \
        || { echo "  FAIL: no cache entry api-cache:$path after a request"; ERRORS=$((ERRORS + 1)); }
done

bash scripts/purge-static-cache.sh

for path in "${DROP[@]}"; do
    [ "$(vcli EXISTS "api-cache:$path")" = "0" ] \
        || { echo "  FAIL: api-cache:$path was kept"; ERRORS=$((ERRORS + 1)); }
done
for path in "${KEEP[@]}"; do
    [ "$(vcli EXISTS "api-cache:$path")" = "1" ] \
        || { echo "  FAIL: api-cache:$path was dropped"; ERRORS=$((ERRORS + 1)); }
done

if [ "$ERRORS" -gt 0 ]; then
    echo "::error::$ERRORS purge error(s)"
    exit 1
fi
echo "Static cache purge verified on keys written by the app."
