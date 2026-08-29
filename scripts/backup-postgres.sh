#!/usr/bin/env bash
# Nightly pg_dump of the OverFast postgres cache.
#
# Both tables hold regenerable Blizzard content, so this is not disaster
# recovery — losing them costs refetch time, not data. What it actually buys is
# a fast recovery from the one failure mode the volume fix does not cover:
# somebody drops the volume (`docker compose down -v` is a single flag away) or
# the filesystem corrupts it. Refilling 654 profiles and 576 static rows from
# Blizzard happens lazily and behind the throttle, so a restore is minutes
# instead of days of slow cold starts.
#
# Deliberately local-only. Shipping it off-box would guard against losing the
# whole VPS, but at that point the API is being rebuilt from scratch anyway and
# the cache is the least of it.
#
# Install (as root on the VPS):
#   0 4 * * * /opt/overfast-api/scripts/backup-postgres.sh >>/var/log/overfast-backup.log 2>&1
#
# Restore (verified end to end: both tables truncated, then restored intact):
#   docker compose -f /opt/overfast-api/docker-compose.yml exec -T postgres \
#       sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
#              --clean --if-exists --no-owner' \
#       < /var/backups/overfast/overfast-<stamp>.dump
#
# The "does not exist, skipping" notices from --clean --if-exists on a fresh
# database are expected. No downtime is needed: the app tolerates an empty
# cache, it just refetches.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/overfast-api/docker-compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/overfast}"
KEEP_DAYS="${KEEP_DAYS:-7}"
# Both cache tables must appear in the archive's table of contents. Checking the
# dump is *readable and complete* beats checking it is big: a byte-count floor
# has to be guessed, and it rejects a legitimately small database — exactly the
# state right after the volume loss this backup exists for.
REQUIRED_TABLES="${REQUIRED_TABLES:-player_profiles static_data}"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

mkdir -p "$BACKUP_DIR"

STAMP=$(date -u '+%Y%m%dT%H%M%SZ')
TARGET="${BACKUP_DIR}/overfast-${STAMP}.dump"

# Written to .part first so a crashed or half-written dump is never mistaken for
# a usable backup by the rotation below.
log "Dumping to ${TARGET}..."
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"${TARGET}.part"

# pg_restore --list parses the archive header and TOC, so it fails on a
# truncated, corrupt or non-archive file — including the case where pg_dump
# wrote an error message to stdout instead of a dump.
if ! TOC=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_restore --list 2>/dev/null <"${TARGET}.part"); then
    rm -f "${TARGET}.part"
    log "ERROR: dump is not a readable pg_dump archive — discarded."
    exit 1
fi

for TABLE in $REQUIRED_TABLES; do
    if ! printf '%s\n' "$TOC" | grep -q "TABLE DATA public ${TABLE} "; then
        rm -f "${TARGET}.part"
        log "ERROR: dump has no data for '${TABLE}' — discarded."
        exit 1
    fi
done

SIZE=$(wc -c <"${TARGET}.part" | tr -d ' ')
mv "${TARGET}.part" "$TARGET"
log "Wrote ${TARGET} (${SIZE} bytes, archive verified)."

# Rotate. Only completed dumps are considered, and only after a good one landed
# above — so a run of failures can never leave us with nothing.
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -name 'overfast-*.dump' -type f \
    -mtime "+${KEEP_DAYS}" -print -delete | wc -l | tr -d ' ')
log "Rotation: removed ${DELETED} dump(s) older than ${KEEP_DAYS} days."

KEPT=$(find "$BACKUP_DIR" -maxdepth 1 -name 'overfast-*.dump' -type f | wc -l | tr -d ' ')
log "Done. ${KEPT} backup(s) on disk."
