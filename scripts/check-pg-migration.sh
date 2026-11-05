#!/bin/bash
# Deploy gate: exits non-zero while the postgres:17 cluster has not been migrated.
#
# docker-compose.yml mounts a new volume, so without this a deploy would start
# postgres:18 on an empty cluster and the app would carry on against it, looking
# healthy with all history missing. Fails closed: a check that cannot run stops
# the deploy too. A host without the old volume is a fresh one and passes.
#
# The marker is written by scripts/migrate-postgres-18.sh, and only after the
# restored row counts matched.
set -euo pipefail

OLD_VOLUME="overfast-api_pg-data"
NEW_VOLUME="overfast-api_postgres-data"
MARKER=".migrated-from-pg17"

IMAGE=$(docker compose config --images postgres | head -1)
[ -n "$IMAGE" ] || { echo "ERROR: could not read the postgres image from compose." >&2; exit 1; }

volume_has_file() {
    docker volume inspect "$1" >/dev/null 2>&1 || return 1
    local rc=0
    docker run --rm -v "$1":/v:ro --entrypoint sh "$IMAGE" -c "[ -f '/v/$2' ]" || rc=$?
    if [ "$rc" -gt 1 ]; then
        echo "ERROR: could not inspect volume $1 (docker exit $rc)." >&2
        exit 1
    fi
    return "$rc"
}

if volume_has_file "$OLD_VOLUME" PG_VERSION && ! volume_has_file "$NEW_VOLUME" "$MARKER"; then
    cat >&2 <<EOF
ERROR: $OLD_VOLUME holds a postgres:17 cluster that has not been migrated.
Deploy stopped before touching anything; the running stack keeps serving.
Run scripts/migrate-postgres-18.sh on this host, then deploy again.
EOF
    exit 1
fi

echo "Postgres migration check passed."
