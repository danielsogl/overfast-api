#!/bin/bash
# One-time move of the production cluster from postgres:17 to postgres:18 by dump
# and restore. Run by hand on the VPS, as root, from /opt/overfast-api, after the
# PR that moved docker-compose.yml to postgres:18 has been merged.
#
# API writes stop for the duration: app, worker and scheduler are stopped first,
# so the dump cannot miss a snapshot. They come back with scripts/deploy.sh.
#
# The old volume is only ever read, so it remains the rollback. Nothing here
# deletes anything.
set -euo pipefail

OLD_VOLUME="overfast-api_pg-data"
NEW_VOLUME="overfast-api_postgres-data"
MARKER=".migrated-from-pg17"
TEMP_CONTAINER="overfast-pg17-migrate"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/overfast}"
IMAGE_17="postgres:17-alpine"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }
die() { log "ERROR: $*"; exit 1; }

env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d "\"'" || true; }
PG_USER="$(env_value POSTGRES_USER)"; PG_USER="${PG_USER:-overfast}"
PG_DB="$(env_value POSTGRES_DB)"; PG_DB="${PG_DB:-overfast}"

on_exit() {
    local rc=$?
    docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
    [ "$rc" -eq 0 ] && return
    log "FAILED. $OLD_VOLUME is unchanged and app, worker and scheduler may be stopped."
    log "Rollback: see 'Rollback' in the PR that added this script."
}
trap on_exit EXIT

# "<table> <rows>" for every table, through the exec prefix given as arguments.
table_counts() {
    local table
    for table in $("$@" psql -U "$PG_USER" -d "$PG_DB" -tAc \
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"); do
        printf '%s %s\n' "$table" \
            "$("$@" psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT count(*) FROM \"$table\"")"
    done
}

# ── Preflight: nothing has been stopped yet ──────────────────────────────────
docker volume inspect "$OLD_VOLUME" >/dev/null 2>&1 || die "$OLD_VOLUME does not exist."
docker run --rm -v "$OLD_VOLUME":/v:ro --entrypoint sh "$IMAGE_17" -c \
    '[ "$(cat /v/PG_VERSION)" = 17 ]' || die "$OLD_VOLUME holds no postgres 17 cluster."
if docker volume inspect "$NEW_VOLUME" >/dev/null 2>&1; then
    die "$NEW_VOLUME already exists. If an earlier run failed and it holds nothing of value, remove it by hand first."
fi
mkdir -p "$BACKUP_DIR"

# ── Stop everything that writes, then read the old cluster ───────────────────
log "Stopping app, worker, scheduler and postgres..."
docker compose stop app worker scheduler postgres

log "Starting $IMAGE_17 on $OLD_VOLUME..."
docker run -d --name "$TEMP_CONTAINER" -v "$OLD_VOLUME":/var/lib/postgresql/data "$IMAGE_17" >/dev/null
for _ in $(seq 1 60); do
    docker exec "$TEMP_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break
    sleep 2
done
docker exec "$TEMP_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null || die "postgres 17 did not start."

SOURCE_COUNTS="$(table_counts docker exec "$TEMP_CONTAINER")"
[ -n "$SOURCE_COUNTS" ] || die "the source database has no tables."
log "Source tables:"; echo "$SOURCE_COUNTS"

# Room for the dump and the new cluster, each at most the size of the old one.
DB_BYTES=$(docker exec "$TEMP_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT pg_database_size(current_database())")
for dir in "$BACKUP_DIR" "$(docker info --format '{{.DockerRootDir}}')"; do
    FREE_BYTES=$(df -B1 --output=avail "$dir" | tail -1 | tr -d ' ')
    [ "$FREE_BYTES" -gt $((DB_BYTES * 2)) ] || die "$dir has $FREE_BYTES bytes free, the database is $DB_BYTES."
done

DUMP="${BACKUP_DIR}/pre-pg18-$(date -u '+%Y%m%dT%H%M%SZ').dump"
log "Dumping to $DUMP..."
docker exec "$TEMP_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc >"${DUMP}.part"
docker exec -i "$TEMP_CONTAINER" pg_restore --list <"${DUMP}.part" >/dev/null || die "the dump is not readable."
mv "${DUMP}.part" "$DUMP"
docker rm -f "$TEMP_CONTAINER" >/dev/null

# ── Restore into postgres 18 on the new volume ───────────────────────────────
log "Starting postgres 18 on $NEW_VOLUME..."
docker compose up -d --no-deps postgres
# pg_isready already answers during initdb's temporary server, which is then shut
# down again. The log line marks the point after which only the real one runs.
for _ in $(seq 1 60); do
    if docker compose logs postgres 2>/dev/null | grep 'PostgreSQL init process complete' >/dev/null \
        && docker compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
docker compose exec -T postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null || die "postgres 18 did not start."

log "Restoring..."
docker compose exec -T postgres pg_restore -U "$PG_USER" -d "$PG_DB" \
    --no-owner --single-transaction --exit-on-error <"$DUMP"

TARGET_COUNTS="$(table_counts docker compose exec -T postgres)"
if [ "$SOURCE_COUNTS" != "$TARGET_COUNTS" ]; then
    log "Target tables:"; echo "$TARGET_COUNTS"
    die "row counts differ, not marking the migration as done."
fi
log "Row counts match."

docker run --rm -v "$NEW_VOLUME":/v --entrypoint touch "$IMAGE_17" "/v/$MARKER"

log "Done. Dump kept at $DUMP. Start the stack with: bash scripts/deploy.sh"
