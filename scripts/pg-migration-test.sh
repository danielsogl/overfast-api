#!/bin/bash
# End-to-end check of the postgres 17 to 18 move: the deploy gate blocks before
# it, the migration keeps the data, and the gate passes after it.
#
# Expects a .env, as left behind by scripts/smoke-test.sh. CI only, because it
# deletes the stack's volumes.
set -euo pipefail

[ "${CI:-}" = "true" ] || { echo "Refusing to run outside CI: this deletes volumes." >&2; exit 1; }

OLD_VOLUME="overfast-api_pg-data"
PG_USER="${POSTGRES_USER:-overfast}"
PG_DB="${POSTGRES_DB:-overfast}"

docker compose down -v --remove-orphans

echo "=== Seeding a postgres 17 cluster in $OLD_VOLUME ==="
docker volume create "$OLD_VOLUME" >/dev/null
docker run -d --name pg17-seed -e POSTGRES_USER="$PG_USER" -e POSTGRES_DB="$PG_DB" \
    -e POSTGRES_PASSWORD=seed -v "$OLD_VOLUME":/var/lib/postgresql/data postgres:17-alpine >/dev/null
for _ in $(seq 1 60); do
    docker logs pg17-seed 2>&1 | grep 'PostgreSQL init process complete' >/dev/null \
        && docker exec pg17-seed pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break
    sleep 2
done
docker exec pg17-seed psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE player_snapshots (id INT PRIMARY KEY, data JSONB NOT NULL);
     INSERT INTO player_snapshots SELECT i, jsonb_build_object('i', i) FROM generate_series(1, 500) i;"
docker rm -f pg17-seed >/dev/null

echo "=== The deploy gate must block an unmigrated host ==="
if bash scripts/check-pg-migration.sh; then
    echo "::error::the deploy gate let an unmigrated cluster through"
    exit 1
fi

echo "=== Migrating ==="
BACKUP_DIR="$(mktemp -d)" bash scripts/migrate-postgres-18.sh

echo "=== The deploy gate must pass now, and the data must be there ==="
bash scripts/check-pg-migration.sh

psql_q() { docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "$1"; }
ROWS="$(psql_q 'SELECT count(*) FROM player_snapshots')"
VERSION="$(psql_q 'SHOW server_version_num')"
[ "$ROWS" = "500" ] || { echo "::error::expected 500 rows, got $ROWS"; exit 1; }
[ "${VERSION:0:2}" = "18" ] || { echo "::error::expected postgres 18, got $VERSION"; exit 1; }

docker run --rm -v "$OLD_VOLUME":/v:ro --entrypoint sh postgres:17-alpine -c '[ -f /v/PG_VERSION ]' \
    || { echo "::error::the old volume no longer holds its cluster"; exit 1; }

echo "Postgres migration verified."
