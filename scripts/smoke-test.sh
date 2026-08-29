#!/bin/bash
# Full-stack smoke test: boots the compose stack from .env.dist defaults and
# validates the static endpoints end to end through nginx.
#
# Single source of truth for build.yml (pull requests) and release.yaml
# (deploy gate). These used to carry their own copy of this logic and drifted
# apart — the release copy was missing the POSTGRES_PASSWORD line, which let a
# credential mismatch reach the deploy gate unnoticed.
#
# Runnable locally: `bash scripts/smoke-test.sh` (overwrites .env).
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
TIMEOUT="${TIMEOUT:-150}"
INTERVAL=5
ERRORS=0

fetch() {
    curl -sf --compressed "$BASE_URL$1"
}

fail() {
    echo "  FAIL: $1"
    ERRORS=$((ERRORS + 1))
}

# ── Step 1: .env from shipped defaults ───────────────────────────────────────
# .env.dist ships POSTGRES_PASSWORD empty on purpose
# (docker-compose.yml requires it via ${VAR:?}), so supply a test value.
echo "=== Creating .env from defaults ==="
# Keep a local developer .env recoverable — this script is runnable by hand.
# Plain `[ -f .env ] && ...` would abort under `set -e` when no .env exists.
if [ -f .env ]; then
    cp .env .env.smoke-backup
    echo "  existing .env saved to .env.smoke-backup"
fi
# One non-in-place pass: `sed -i'' -e` is read by BSD sed (macOS) as a backup
# suffix of "-e", which left a credential-carrying .env-e behind on every run.
sed -e 's/^APP_PORT=.*/APP_PORT=8080/' \
    -e 's/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=ci-test-password/' \
    .env.dist > .env

# ── Step 2: Build and start ──────────────────────────────────────────────────
echo "=== Building and starting services ==="
docker compose build
docker compose up -d

# ── Step 3: Wait for the core services ───────────────────────────────────────
echo "=== Waiting for core services (app, nginx, valkey) ==="
SERVICES="app nginx valkey"
READY=false

for i in $(seq 1 $((TIMEOUT / INTERVAL))); do
    HEALTHY=0
    for SERVICE in $SERVICES; do
        STATUS=$(docker compose ps --format json | jq -rs \
            "[.[] | select(.Service == \"$SERVICE\")] | .[0].Health // \"starting\"")
        [ "$STATUS" = "healthy" ] && HEALTHY=$((HEALTHY + 1))
    done

    echo "[$((i * INTERVAL))/${TIMEOUT}s] $HEALTHY/3 services healthy"
    if [ "$HEALTHY" -eq 3 ]; then
        READY=true
        break
    fi
    sleep $INTERVAL
done

if [ "$READY" != true ]; then
    echo "::error::Services did not become healthy within ${TIMEOUT}s"
    docker compose ps
    docker compose logs --tail=200
    exit 1
fi

# ── Step 4: Validate the static endpoints ────────────────────────────────────
# Every check is non-fatal so one run reports every problem at once; the
# accumulated ERRORS count decides the exit status.
echo "=== GET / ==="
BODY=$(fetch "/") || fail "/ not reachable"
echo "$BODY" | grep -q "redoc" || fail "/ does not contain redoc HTML"

echo "=== GET /openapi.json ==="
BODY=$(fetch "/openapi.json") || fail "/openapi.json not reachable"
echo "$BODY" | jq -e '.openapi' >/dev/null || fail "missing openapi version field"
echo "$BODY" | jq -e '.paths | keys | length > 5' >/dev/null || fail "too few paths in spec"

echo "=== GET /roles ==="
BODY=$(fetch "/roles") || fail "/roles not reachable"
COUNT=$(echo "$BODY" | jq 'length')
[ "$COUNT" -eq 3 ] || fail "expected 3 roles, got $COUNT"
KEYS=$(echo "$BODY" | jq -r '.[].key' | sort | tr '\n' ',')
[ "$KEYS" = "damage,support,tank," ] || fail "role keys mismatch: $KEYS"
echo "$BODY" | jq -e 'all(has("key","name","icon","description"))' >/dev/null \
    || fail "roles missing required fields"
echo "$BODY" | jq -e 'all(.icon | startswith("http"))' >/dev/null \
    || fail "role icons are not valid URLs"
echo "  $COUNT roles"

echo "=== GET /gamemodes ==="
BODY=$(fetch "/gamemodes") || fail "/gamemodes not reachable"
COUNT=$(echo "$BODY" | jq 'length')
[ "$COUNT" -ge 10 ] || fail "expected >=10 gamemodes, got $COUNT"
echo "$BODY" | jq -e 'all(has("key","name","icon","description","screenshot"))' >/dev/null \
    || fail "gamemodes missing required fields"
echo "$BODY" | jq -e 'all(.description | length > 10)' >/dev/null \
    || fail "gamemode descriptions too short or empty"
echo "$BODY" | jq -e 'map(.key) | contains(["control","escort","push"])' >/dev/null \
    || fail "known gamemodes (control, escort, push) not found"
echo "  $COUNT gamemodes"

echo "=== GET /maps ==="
BODY=$(fetch "/maps") || fail "/maps not reachable"
COUNT=$(echo "$BODY" | jq 'length')
[ "$COUNT" -ge 40 ] || fail "expected >=40 maps, got $COUNT"
echo "$BODY" | jq -e 'all(has("key","name","screenshot","gamemodes","location"))' >/dev/null \
    || fail "maps missing required fields"
echo "$BODY" | jq -e 'all(.gamemodes | length >= 1)' >/dev/null \
    || fail "some maps have no gamemodes"
echo "$BODY" | jq -e 'all(.screenshot | startswith("http"))' >/dev/null \
    || fail "map screenshots are not valid URLs"
echo "$BODY" | jq -e 'map(.key) | contains(["ilios","dorado","kings-row"])' >/dev/null \
    || fail "known maps (ilios, dorado, kings-row) not found"
echo "  $COUNT maps"

echo "=== GET /heroes ==="
BODY=$(fetch "/heroes") || fail "/heroes not reachable"
COUNT=$(echo "$BODY" | jq 'length')
[ "$COUNT" -ge 40 ] || fail "expected >=40 heroes, got $COUNT"
echo "$BODY" | jq -e 'all(has("key","name","portrait","role"))' >/dev/null \
    || fail "heroes missing required fields"
echo "$BODY" | jq -e 'all(.role | IN("damage","support","tank"))' >/dev/null \
    || fail "heroes have invalid roles"
echo "$BODY" | jq -e 'all(.portrait | startswith("http"))' >/dev/null \
    || fail "hero portraits are not valid URLs"
echo "$BODY" | jq -e 'map(.key) | contains(["ana","tracer","reinhardt"])' >/dev/null \
    || fail "known heroes (ana, tracer, reinhardt) not found"
for ROLE in damage support tank; do
    ROLE_COUNT=$(echo "$BODY" | jq "[.[] | select(.role == \"$ROLE\")] | length")
    [ "$ROLE_COUNT" -ge 5 ] || fail "only $ROLE_COUNT $ROLE heroes (expected >=5)"
done
echo "  $COUNT heroes"

# ── Static assets, as actually served ────────────────────────────────────────
#
# The unit tests assert every CSV row has its file in the repo. Nothing checked
# that nginx serves it — and for two months it did not: `static` was a named
# volume, which Docker fills from the image only when the volume is FIRST
# created, so neon-junction.jpg 404ed in production from the day it was added
# while every test stayed green.
echo "=== Static assets served by nginx ==="
ASSET_404=0
CHECKED=0
while IFS=, read -r KEY _ || [ -n "$KEY" ]; do
    [ "$KEY" = "key" ] && continue
    CHECKED=$((CHECKED + 1))
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
        "${BASE_URL}/static/maps/${KEY}.jpg")
    if [ "$CODE" != "200" ]; then
        echo "  MISSING: /static/maps/${KEY}.jpg -> HTTP $CODE"
        ASSET_404=$((ASSET_404 + 1))
    fi
done < app/domain/utils/data/maps.csv

while IFS=, read -r KEY _ || [ -n "$KEY" ]; do
    [ "$KEY" = "key" ] && continue
    for ASSET in "${KEY}-icon.svg" "${KEY}.avif"; do
        CHECKED=$((CHECKED + 1))
        CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
            "${BASE_URL}/static/gamemodes/${ASSET}")
        if [ "$CODE" != "200" ]; then
            echo "  MISSING: /static/gamemodes/${ASSET} -> HTTP $CODE"
            ASSET_404=$((ASSET_404 + 1))
        fi
    done
done < app/domain/utils/data/gamemodes.csv

[ "$ASSET_404" -eq 0 ] || fail "$ASSET_404 static asset(s) not served"
echo "  $CHECKED assets served"

# ── Trailing-slash redirect ──────────────────────────────────────────────────
#
# The absolute form of this redirect dropped the query string, so
# /heroes/?role=damage answered with every hero instead of the filtered set —
# a wrong result rather than an error, which no status-code check would catch.
# It also emitted the container's own scheme and the client's Host header.
echo "=== Trailing-slash redirect ==="
# The raw header, not curl's %{redirect_url} — that one resolves a relative
# Location against the request URL and so always looks absolute.
LOCATION=$(curl -sI "$BASE_URL/heroes/?role=damage" \
    | tr -d '\r' | awk 'tolower($1) == "location:" { print $2 }')
echo "  Location: $LOCATION"
case "$LOCATION" in
    *"role=damage") ;;
    *) fail "redirect dropped the query string: $LOCATION" ;;
esac
case "$LOCATION" in
    http://*|https://*) fail "redirect is absolute, must be relative: $LOCATION" ;;
esac

REDIRECTED_COUNT=$(curl -sfL --compressed "$BASE_URL/heroes/?role=damage" | jq 'length')
DIRECT_COUNT=$(fetch "/heroes?role=damage" | jq 'length')
[ "$REDIRECTED_COUNT" = "$DIRECT_COUNT" ] \
    || fail "redirect changed the result: $REDIRECTED_COUNT vs $DIRECT_COUNT heroes"
echo "  $DIRECT_COUNT heroes, filter survives the redirect"

# ── Conditional requests (ETag / If-None-Match) ──────────────────────────────
#
# Two different pieces of software answer a request here, and only one of them
# is FastAPI, so both need their own assertion:
#
#   cache hit  → nginx answers from Valkey inside lua/valkey_handler.lua and
#                reads the ETag out of the cache envelope. FastAPI is never
#                reached, which is precisely why pytest cannot cover it.
#   cache miss → nginx falls through to @fallback and FastAPI's ETagMiddleware
#                hashes the body it just rendered.
#
# /heroes was fetched further up this script, so its api-cache key exists and
# the Lua handler cannot fall through: that request is served from Valkey.
#
# For the miss, undeclared query parameters are deliberately excluded from the
# cache key (app/api/helpers.py::build_cache_key), so nginx looks up a key the
# app never writes. /heroes?etag-smoke=1 is therefore a *repeatable* cache miss,
# which is the only way to send a conditional request to FastAPI twice.
echo "=== ETag / If-None-Match ==="
CACHE_TTL_HEADER=$(awk -F= '/^CACHE_TTL_HEADER=/ { print $2 }' .env)
CACHE_TTL_HEADER="${CACHE_TTL_HEADER:-X-Cache-TTL}"

check_conditional_request() {
    LABEL="$1"
    URI="$2"
    HDR_FILE=$(mktemp)
    BODY_FILE=$(mktemp)

    ETAG=$(curl -s -D - -o /dev/null "$BASE_URL$URI" \
        | tr -d '\r' | awk 'tolower($1) == "etag:" { print $2 }' || true)
    if [ -z "$ETAG" ]; then
        fail "$LABEL: no ETag on $URI"
        rm -f "$HDR_FILE" "$BODY_FILE"
        return
    fi

    CODE=$(curl -s -D "$HDR_FILE" -o "$BODY_FILE" -w "%{http_code}" \
        -H "If-None-Match: $ETAG" "$BASE_URL$URI")
    [ "$CODE" = "304" ] || fail "$LABEL: expected 304 for $URI, got $CODE"
    # An empty body is the entire point: a 304 costs no payload.
    [ ! -s "$BODY_FILE" ] || fail "$LABEL: 304 carried a body"
    # A 304 that dropped these would leave the client worse off than with no
    # ETag at all — it would have nothing left to compute freshness from.
    for HEADER in Cache-Control X-Cache-Status "$CACHE_TTL_HEADER"; do
        grep -qi "^${HEADER}:" "$HDR_FILE" || fail "$LABEL: 304 dropped $HEADER"
    done

    echo "  $LABEL: $URI -> $CODE ($ETAG)"
    rm -f "$HDR_FILE" "$BODY_FILE"
}

# Only what pytest cannot reach is asserted here. That the tag tracks the
# payload is app logic and is covered in tests/api/test_etag.py; keeping it out
# also keeps this section inside the burst allowance of the nginx rate limit.
check_conditional_request "cache hit (nginx/Lua)" "/heroes"
check_conditional_request "cache miss (FastAPI)" "/heroes?etag-smoke=1"

echo ""
if [ "$ERRORS" -gt 0 ]; then
    echo "::error::$ERRORS validation error(s)"
    docker compose logs --tail=200
    exit 1
fi
echo "All endpoint validations passed."
