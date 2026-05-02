#!/bin/bash
# Zero-downtime deploy script for OverFast API.
# Called by /opt/deploy-overfast.sh after git reset and .env sync.
#
# Order:
#   build (--pull) → postgres up → valkey up → app+worker+scheduler up
#   → nginx (reload-or-recreate). Nginx never goes through a hard
#   restart unless its image changed.
set -euo pipefail

LOG_FILE="/var/log/overfast-deploy.log"
COMPOSE_PROJECT="overfast-api"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Poll a service until its health status is "healthy".
# Usage: wait_healthy <service> [max_wait_seconds]
wait_healthy() {
    local service=$1 max_wait=${2:-90} waited=0
    log "Waiting for '$service' to become healthy (timeout ${max_wait}s)..."
    while [ "$waited" -lt "$max_wait" ]; do
        local id health
        id=$(docker compose ps -q "$service" 2>/dev/null | head -1)
        if [ -n "$id" ]; then
            health=$(docker inspect "$id" --format '{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
            if [ "$health" = "healthy" ]; then
                log "  '$service' is healthy after ${waited}s."
                return 0
            fi
        fi
        sleep 3
        waited=$((waited + 3))
    done
    log "ERROR: '$service' did not become healthy within ${max_wait}s."
    docker compose ps "$service" 2>&1 | tee -a "$LOG_FILE" || true
    return 1
}

# Capture the image ID a service's running container was launched from.
# Used to detect whether `docker compose build` produced a new image
# that the running container is not yet using. Empty string if the
# service is not running.
running_image_id() {
    local service=$1 cid
    cid=$(docker compose ps -q "$service" 2>/dev/null | head -1 || true)
    [ -z "$cid" ] && return 0
    docker inspect "$cid" --format '{{.Image}}' 2>/dev/null || true
}

# ── Step 1: Snapshot running image IDs before we rebuild ─────────────────────
log "Capturing current image IDs..."
NGINX_IMAGE_BEFORE=$(running_image_id nginx)
VALKEY_IMAGE_BEFORE=$(running_image_id valkey)
log "  nginx  : ${NGINX_IMAGE_BEFORE:-<none>}"
log "  valkey : ${VALKEY_IMAGE_BEFORE:-<none>}"

# ── Step 2: Build all images, pulling fresh base layers ──────────────────────
# --pull: re-fetch FROM bases (postgres:17-alpine, valkey/valkey:9-alpine,
# python, openresty, ...). Without it we silently skip security/feature
# updates whenever the registry tag is unchanged but its content moved.
log "Building Docker images (--pull)..."
docker compose build --pull 2>&1 | tee -a "$LOG_FILE"
log "Build complete."

# ── Step 3: Ensure postgres is running and healthy ───────────────────────────
# `docker compose up -d --no-deps` is idempotent: only recreates the
# container when image or config changed.
log "Ensuring postgres is running..."
docker compose up -d --no-deps postgres 2>&1 | tee -a "$LOG_FILE"
wait_healthy postgres 120

# ── Step 4: Recreate valkey if its image changed ─────────────────────────────
# valkey is a dependency of app/worker/scheduler — must be on the new
# image before they restart, otherwise app keeps talking to the old
# valkey while a new one is built but never started.
log "Reconciling valkey container with built image..."
docker compose up -d --no-deps valkey 2>&1 | tee -a "$LOG_FILE"
VALKEY_IMAGE_AFTER=$(running_image_id valkey)
if [ "$VALKEY_IMAGE_BEFORE" != "$VALKEY_IMAGE_AFTER" ]; then
    log "  valkey image changed: ${VALKEY_IMAGE_BEFORE:-<none>} -> $VALKEY_IMAGE_AFTER"
fi
wait_healthy valkey 60

# ── Step 5: Rolling restart — app + worker + scheduler ───────────────────────
# scheduler runs taskiq cron tasks (e.g. check_new_hero). Must be in the
# rolling restart or it stays missing entirely after a fresh deploy.
# --remove-orphans: clean up containers from services that were removed
# from the compose file or moved behind a profile (e.g. reverse-proxy).
log "Rolling restart: app + worker + scheduler (nginx stays up)..."
docker compose up -d --no-deps --remove-orphans app worker scheduler 2>&1 | tee -a "$LOG_FILE"

# ── Step 6: Wait for app + scheduler to be healthy ───────────────────────────
wait_healthy app 90
wait_healthy scheduler 60

# ── Step 7: Decide how to handle nginx ───────────────────────────────────────
# Image-change detection: when the build produced a new nginx image we
# recreate the container (sub-second gap). When unchanged we send the
# nginx process a SIGHUP for a true zero-downtime config reload.
NGINX_IMAGE_AFTER=$(docker compose images nginx --format json 2>/dev/null \
    | python3 -c "import sys,json; imgs=json.load(sys.stdin); print(imgs[0]['ID'] if imgs else '')" 2>/dev/null \
    || docker images "${COMPOSE_PROJECT}-nginx" --format '{{.ID}}' | head -1 \
    || true)

log "  Nginx image before: ${NGINX_IMAGE_BEFORE:-<none>}"
log "  Nginx image after : ${NGINX_IMAGE_AFTER:-<none>}"

if [ -z "$NGINX_IMAGE_BEFORE" ]; then
    log "nginx was not running. Starting nginx..."
    docker compose up -d --no-deps nginx 2>&1 | tee -a "$LOG_FILE"
elif [ -n "$NGINX_IMAGE_AFTER" ] && [ "$NGINX_IMAGE_BEFORE" != "$NGINX_IMAGE_AFTER" ]; then
    log "nginx image changed. Recreating nginx container..."
    docker compose up -d --no-deps nginx 2>&1 | tee -a "$LOG_FILE"
else
    log "nginx image unchanged. Reloading nginx config in-place..."
    docker compose exec -T nginx nginx -s reload 2>&1 | tee -a "$LOG_FILE"
fi

# ── Step 8: Final health assertion ───────────────────────────────────────────
log "Verifying all containers are healthy..."
sleep 5
if docker compose ps | grep -E "unhealthy|Exit [^0]"; then
    log "ERROR: One or more containers are unhealthy after deploy!"
    docker compose ps 2>&1 | tee -a "$LOG_FILE"
    exit 1
fi

log "Deployment completed successfully!"
docker compose ps 2>&1 | tee -a "$LOG_FILE"
