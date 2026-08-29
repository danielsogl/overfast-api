#!/bin/bash
# OverFast API health check — cron, every 5 minutes.
#
# Detects an unreachable API or an unhealthy container, logs it, and
# deduplicates so one outage does not spam. It then does NOTHING: the
# notification call below is an empty placeholder, and this host has no MTA, so
# cron's non-zero exit goes nowhere either. See scripts/vps/README.md.
set -uo pipefail

REPO_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$REPO_PATH/docker-compose.yml"

# APP_PORT is what docker-compose.yml binds nginx to on the loopback.
APP_PORT="$(grep -oE '^APP_PORT=[0-9]+' "$REPO_PATH/.env" 2>/dev/null | cut -d= -f2)"
API_URL="http://localhost:${APP_PORT:-8080}"
LOG_FILE="/var/log/overfast-health.log"
# Survives a reboot, unlike /tmp — otherwise a restart silently re-arms the
# "alert once per outage" guard.
ALERT_FILE="/var/lib/misc/overfast-alert-sent"

log() {
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] $1" | tee -a "$LOG_FILE"
}

check_api() {
    # Check if API responds
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/" --max-time 10)

    if [ "$HTTP_CODE" = "200" ]; then
        return 0
    else
        return 1
    fi
}

check_containers() {
    # Check if all containers are healthy
    UNHEALTHY=$(docker compose -f "$COMPOSE_FILE" ps 2>/dev/null | grep -c "unhealthy" || true)

    if [ "$UNHEALTHY" -gt 0 ]; then
        return 1
    fi
    return 0
}

# Main health check
if check_api && check_containers; then
    log "Health check PASSED"
    # Remove alert file if exists (service recovered)
    if [ -f "$ALERT_FILE" ]; then
        rm "$ALERT_FILE"
        log "Service recovered - alert cleared"
    fi
    exit 0
else
    log "Health check FAILED"

    # Log container status
    docker compose -f "$COMPOSE_FILE" ps >> "$LOG_FILE" 2>&1

    # Only alert once per failure period
    if [ ! -f "$ALERT_FILE" ]; then
        touch "$ALERT_FILE"
        log "ALERT: OverFast API is down!"
        # Add alerting logic here (email, webhook, etc.)
        # Example: curl -X POST "https://your-webhook-url" -d "OverFast API is down"
    fi

    exit 1
fi
