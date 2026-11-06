#!/bin/bash
# scripts/evict-rendered-cache.sh must drop the API responses the app wrote and
# keep every other key.
#
# The responses are created by real requests through nginx and the app, not
# seeded by hand: a test that writes keys in the format it expects proves only
# that the pattern matches that format.
#
# Expects the compose stack to be up (e.g. after scripts/smoke-test.sh).
set -euo pipefail

APP_PORT="${APP_PORT:-8080}"
BASE_URL="${BASE_URL:-http://localhost:$APP_PORT}"

vcli() { docker compose exec -T valkey valkey-cli "$@" | tr -d '\r'; }

echo "=== Rendered-response eviction on keys written by the app ==="
PATHS=("/maps" "/maps?gamemode=push" "/gamemodes" "/heroes" "/roles")
for path in "${PATHS[@]}"; do
    curl -sf -o /dev/null "$BASE_URL$path"
done
# Not rendered output: the eviction must leave it alone.
vcli SET "throttle:evict-test" "1" EX 300 >/dev/null

echo "  api-cache keys before the eviction:"
vcli --scan --pattern 'api-cache:*' | sort | sed 's/^/    /'

ERRORS=0
for path in "${PATHS[@]}"; do
    [ "$(vcli EXISTS "api-cache:$path")" = "1" ] \
        || { echo "  FAIL: no cache entry api-cache:$path after a request"; ERRORS=$((ERRORS + 1)); }
done

bash scripts/evict-rendered-cache.sh

for path in "${PATHS[@]}"; do
    [ "$(vcli EXISTS "api-cache:$path")" = "0" ] \
        || { echo "  FAIL: api-cache:$path was kept"; ERRORS=$((ERRORS + 1)); }
done
[ "$(vcli EXISTS "throttle:evict-test")" = "1" ] \
    || { echo "  FAIL: throttle:evict-test was dropped"; ERRORS=$((ERRORS + 1)); }
vcli DEL "throttle:evict-test" >/dev/null

if [ "$ERRORS" -gt 0 ]; then
    echo "::error::$ERRORS eviction error(s)"
    exit 1
fi
echo "Rendered-response eviction verified on keys written by the app."
