#!/bin/bash
# VPS wrapper for OverFast API deployments.
# Handles: git reset, .env sync.
# All Docker logic is in scripts/deploy.sh (repo-versioned).
set -euo pipefail

REPO_PATH="/opt/overfast-api"
LOG_FILE="/var/log/overfast-deploy.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Starting deployment ==="
cd "$REPO_PATH"

log "Fetching latest changes..."
git fetch origin main

log "Resetting to latest origin/main..."
git reset --hard origin/main

# Merge new .env.dist variables into .env (preserves existing values)
log "Syncing .env with .env.dist defaults..."
if [ -f .env.dist ] && [ -f .env ]; then
    ADDED=0
    while IFS= read -r line; do
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        VAR_NAME="${line%%=*}"
        if ! grep -q "^${VAR_NAME}=" .env; then
            echo "$line" >> .env
            log "  Added new variable: $VAR_NAME"
            ADDED=$((ADDED + 1))
        fi
    done < .env.dist
    [ "$ADDED" -gt 0 ] && log "  $ADDED new variable(s) added" || log "  .env is up to date"
else
    log "  WARNING: .env or .env.dist missing, skipping env sync"
fi

# Delegate all Docker logic to the repo-versioned script
log "Delegating to scripts/deploy.sh..."
bash "$REPO_PATH/scripts/deploy.sh"
