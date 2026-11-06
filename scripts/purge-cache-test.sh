#!/bin/bash
# scripts/purge-static-cache.sh must drop exactly the CSV-backed API entries.
# Expects the compose stack to be up (e.g. after scripts/smoke-test.sh).
set -euo pipefail

vcli() { docker compose exec -T valkey valkey-cli "$@"; }

echo "=== Static cache purge ==="
DROP=("/heroes" "/heroes?locale=de-de" "/heroes/mauga?locale=en-us" "/maps" "/maps?gamemode=push" "/gamemodes")
KEEP=("/heroes/stats?platform=pc" "/heroes/stats/history?hero=ana" "/players/TeKrop-2217/summary" "/roles")
for key in "${DROP[@]}" "${KEEP[@]}"; do
    vcli SET "api-cache:$key" "purge-test" EX 300 >/dev/null
done

bash scripts/purge-static-cache.sh

ERRORS=0
for key in "${DROP[@]}"; do
    [ "$(vcli EXISTS "api-cache:$key" | tr -d '\r')" = "0" ] || { echo "  FAIL: $key was kept"; ERRORS=$((ERRORS + 1)); }
done
for key in "${KEEP[@]}"; do
    [ "$(vcli EXISTS "api-cache:$key" | tr -d '\r')" = "1" ] || { echo "  FAIL: $key was dropped"; ERRORS=$((ERRORS + 1)); }
    vcli DEL "api-cache:$key" >/dev/null
done

if [ "$ERRORS" -gt 0 ]; then
    echo "::error::$ERRORS purge error(s)"
    exit 1
fi
echo "Static cache purge verified."
