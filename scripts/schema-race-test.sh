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
ROUNDS=5

psql_admin() {
    docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$1" >/dev/null
}

echo "=== Concurrent schema creation on an empty database ==="

# Separate processes, because PostgresStorage is a per-process singleton with
# its own lock: only the database can serialise across containers. Imports take
# about a second and vary, so every process waits for a shared start time before
# it initialises; without that they arrive one after another and never collide.
# Each process opens its pool first, then waits, then creates the schema.
INIT='
import asyncio, os, time
import asyncpg
from app.adapters.storage import postgres_storage
from app.config import settings

async def main():
    storage = postgres_storage.PostgresStorage()
    storage._pool = await asyncpg.create_pool(dsn=settings.postgres_dsn, min_size=1, max_size=1)
    time.sleep(max(0.0, float(os.environ["START_AT"]) - time.time()))
    await storage._create_schema()
    await storage._pool.close()

asyncio.run(main())
'

FAILED=0
for round in $(seq 1 "$ROUNDS"); do
    psql_admin "DROP DATABASE IF EXISTS $RACE_DB"
    psql_admin "CREATE DATABASE $RACE_DB"
    START_AT=$(($(date +%s) + 6))

    pids=()
    for i in $(seq 1 "$WORKERS"); do
        docker compose exec -T -e POSTGRES_DB="$RACE_DB" -e START_AT="$START_AT" app \
            /code/.venv/bin/python -c "$INIT" >"/tmp/schema-race-$i.log" 2>&1 &
        pids+=("$!")
    done

    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            FAILED=$((FAILED + 1))
            echo "  round $round, worker $((i + 1)) failed:"
            tail -3 "/tmp/schema-race-$((i + 1)).log" | sed 's/^/    /'
        fi
    done
done

psql_admin "DROP DATABASE IF EXISTS $RACE_DB"

if [ "$FAILED" -gt 0 ]; then
    echo "::error::$FAILED of $((WORKERS * ROUNDS)) concurrent schema initialisations failed"
    exit 1
fi
echo "All $((WORKERS * ROUNDS)) concurrent schema initialisations succeeded."
