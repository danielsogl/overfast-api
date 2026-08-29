#!/bin/bash
# OverFast API health check — cron, every 5 minutes.
#
# Detects an unreachable API or an unhealthy container, logs it, deduplicates so
# one outage does not spam, and posts to ALERT_WEBHOOK_URL if .env sets one.
#
# The webhook is deliberately generic rather than tied to a provider: this host
# has no MTA, cron's non-zero exit goes nowhere, and picking a channel is not
# this script's call. Unset the variable and the behaviour is what it always
# was — log only. See scripts/vps/README.md.
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

ALERT_WEBHOOK_URL="$(grep -oE '^ALERT_WEBHOOK_URL=.+' "$REPO_PATH/.env" 2>/dev/null | cut -d= -f2-)"

log() {
    echo "[$(date "+%Y-%m-%d %H:%M:%S")] $1" | tee -a "$LOG_FILE"
}

notify() {
    [ -n "${ALERT_WEBHOOK_URL:-}" ] || return 0

    # Both keys on purpose: Slack reads "text", Discord reads "content". Anything
    # else at least receives the message as JSON rather than nothing at all.
    # No JSON escaping: every message here is a literal composed below, and a
    # hostname cannot contain a quote or a backslash.
    if ! curl -sf -m 10 -X POST -H "Content-Type: application/json" \
        -d "{\"text\":\"$1\",\"content\":\"$1\"}" "$ALERT_WEBHOOK_URL" -o /dev/null; then
        # A dead webhook must not mask the outage it was meant to report.
        log "WARNING: alert webhook POST failed"
    fi
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
        # Whoever got the outage message needs the all-clear too, otherwise a
        # five-minute blip is indistinguishable from an ongoing outage.
        notify "OverFast API recovered on $(hostname) — $API_URL is answering again."
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
        notify "OverFast API is DOWN on $(hostname) — $API_URL not answering or a container is unhealthy."
    fi

    exit 1
fi
