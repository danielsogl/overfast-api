#!/bin/bash
# Concurrent schema creation on an empty database: app, worker and scheduler all
# run schema.sql at startup, and on a fresh database two of them used to collide
# on CREATE TYPE (UniqueViolationError on pg_type), which killed the app
# container. Starts several separate processes against a throwaway database at
# once; every one of them must initialise cleanly.
#
# Expects the compose stack to be up (e.g. after scripts/smoke-test.sh).
set -euo pipefail

PG_USER="${POSTGRES_USER:-overfast}"
PG_DB="${POSTGRES_DB:-overfast}"
RACE_DB="schema_race"
WORKERS=8

psql_admin() {
    docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$1" >/dev/null
}

echo "=== Concurrent schema creation on an empty database ==="
psql_admin "DROP DATABASE IF EXISTS $RACE_DB"
psql_admin "CREATE DATABASE $RACE_DB"

# Separate processes, because PostgresStorage is a per-process singleton with
# its own lock: only the database can serialise across containers.
INIT='
import asyncio
from app.adapters.storage.postgres_storage import PostgresStorage

async def main():
    storage = PostgresStorage()
    await storage.initialize()
    await storage.close()

asyncio.run(main())
'

pids=()
for i in $(seq 1 "$WORKERS"); do
    docker compose exec -T -e POSTGRES_DB="$RACE_DB" app \
        /code/.venv/bin/python -c "$INIT" >"/tmp/schema-race-$i.log" 2>&1 &
    pids+=("$!")
done

FAILED=0
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        FAILED=$((FAILED + 1))
        echo "  worker $((i + 1)) failed:"
        tail -5 "/tmp/schema-race-$((i + 1)).log" | sed 's/^/    /'
    fi
done

psql_admin "DROP DATABASE IF EXISTS $RACE_DB"

if [ "$FAILED" -gt 0 ]; then
    echo "::error::$FAILED of $WORKERS concurrent schema initialisations failed"
    exit 1
fi
echo "All $WORKERS concurrent schema initialisations succeeded."
