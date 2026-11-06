#!/bin/bash
# Drop the Valkey API cache entries of the routes built from the bundled CSVs:
# /heroes, /heroes/{key}, /maps and /gamemodes. Their entries live 24h, so a
# heroes.csv edit would otherwise keep being served from Valkey for up to a day
# after the deploy that shipped it.
#
# Run by scripts/deploy.sh after app, worker and scheduler run the new code, so
# no old process can write the old values back. /heroes/stats* are kept: those
# are live Blizzard numbers that do not come from a CSV, and dropping them would
# only cost Blizzard requests. Everything dropped here is rebuilt from postgres
# on the next request, without asking Blizzard.
#
# Prints one line per pattern and the number of keys removed.
set -euo pipefail

docker compose exec -T valkey sh -c '
    total=0
    for pattern in "api-cache:/heroes" "api-cache:/heroes\\?*" "api-cache:/heroes/*" \
                   "api-cache:/maps*" "api-cache:/gamemodes*"; do
        keys=$(valkey-cli --scan --pattern "$pattern" | grep -v "^api-cache:/heroes/stats" || true)
        count=0
        if [ -n "$keys" ]; then
            count=$(printf "%s\n" "$keys" | tr "\\n" "\\0" | xargs -0 valkey-cli UNLINK | awk "{s+=\$1} END {print s+0}")
        fi
        echo "  $pattern: $count key(s) removed"
        total=$((total + count))
    done
    echo "  static API cache purge: $total key(s) removed"
'
