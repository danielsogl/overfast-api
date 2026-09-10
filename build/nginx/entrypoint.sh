#!/bin/sh

# Fail fast on errors and unset variables; enable pipefail where supported
set -eu
set -o pipefail 2>/dev/null || true

# `set -a` covers every default below, and it is load-bearing rather than
# stylistic: envsubst reads the *environment*, so a variable defaulted here but
# not exported is substituted as the empty string and the default silently does
# nothing. NGINX_WORKER_CONNECTIONS was the visible case — without it in .env
# nginx died on `worker_connections ;` — but RETRY_AFTER_HEADER,
# CONDITIONAL_GET_HEADER, UNKNOWN_PLAYER_COOLDOWN_KEY_PREFIX and
# UNKNOWN_PLAYERS_CACHE_ENABLED had the same hole, the last one blanking a
# truthiness check in the Lua handler rather than failing loudly.
#
# In production every one of these comes from .env, so the gap only showed up
# outside it. That is exactly what makes it worth closing: the defaults exist
# so the image runs without a full .env, and until now they never did.
set -a

# Set defaults for nginx tuning variables if not provided
: "${NGINX_WORKER_PROCESSES:=0}"
: "${NGINX_WORKER_CONNECTIONS:=1024}"
: "${NGINX_MULTI_ACCEPT:=true}"

# Set defaults for rate limiting variables if not provided
# Default to the RFC1918 ranges Docker allocates bridge networks from.
# docker-compose.yml publishes nginx on 127.0.0.1 only, so the sole path in is
# Caddy on the host, and every request therefore arrives from the bridge
# gateway. Without this, $binary_remote_addr is that one gateway address for
# every client and the limit_req/limit_conn zones collapse into a single global
# bucket instead of being per IP.
#
# Safe because a public client can never originate from an RFC1918 address, and
# Caddy appends the real peer to X-Forwarded-For — with real_ip_recursive on,
# nginx walks from the right and takes the first untrusted entry, so a
# client-supplied prefix is ignored. Override in .env if nginx is ever exposed
# directly, where trusting nothing is the correct default.
: "${TRUSTED_PROXY_CIDRS:=172.16.0.0/12,192.168.0.0/16,10.0.0.0/8}"
: "${RETRY_AFTER_HEADER:=Retry-After}"
: "${CONDITIONAL_GET_HEADER:=X-Conditional-Get}"
: "${UNKNOWN_PLAYER_COOLDOWN_KEY_PREFIX:=unknown-player:cooldown}"
: "${UNKNOWN_PLAYERS_CACHE_ENABLED:=true}"

set +a

# Convert NGINX_WORKER_PROCESSES: 0 → "auto" (nginx auto-detect syntax)
if [ "$NGINX_WORKER_PROCESSES" = "0" ]; then
  NGINX_WORKER_PROCESSES_VALUE="auto"
else
  NGINX_WORKER_PROCESSES_VALUE="$NGINX_WORKER_PROCESSES"
fi

export NGINX_WORKER_PROCESSES_VALUE

# Convert NGINX_MULTI_ACCEPT boolean to nginx syntax (on/off)
if [ "$NGINX_MULTI_ACCEPT" = "true" ] || [ "$NGINX_MULTI_ACCEPT" = "True" ] || [ "$NGINX_MULTI_ACCEPT" = "1" ]; then
  NGINX_MULTI_ACCEPT_VALUE="on"
else
  NGINX_MULTI_ACCEPT_VALUE="off"
fi

export NGINX_MULTI_ACCEPT_VALUE

# Build real_ip config from the comma-separated list of trusted proxy CIDRs.
# Set TRUSTED_PROXY_CIDRS= (empty) in .env to disable: $remote_addr then stays
# the TCP peer and X-Forwarded-For is ignored, which is correct only when nginx
# is exposed directly rather than behind the host's Caddy.
if [ -n "$TRUSTED_PROXY_CIDRS" ]; then
  REAL_IP_CONFIG=$(printf '%s\n' "$TRUSTED_PROXY_CIDRS" | tr ',' '\n' | while read -r cidr; do
    if [ -n "$cidr" ]; then
      printf 'set_real_ip_from %s;\n' "$cidr"
    fi
  done)
  REAL_IP_CONFIG="${REAL_IP_CONFIG}
real_ip_header X-Forwarded-For;
real_ip_recursive on;"
else
  REAL_IP_CONFIG=''
fi

export REAL_IP_CONFIG

# Generate main nginx.conf from template
envsubst '${NGINX_WORKER_PROCESSES_VALUE} ${NGINX_WORKER_CONNECTIONS} ${NGINX_MULTI_ACCEPT_VALUE}' < /etc/nginx/nginx.conf.template > /usr/local/openresty/nginx/conf/nginx.conf

# Replace placeholders and generate config and lua script from templates
envsubst '${REAL_IP_CONFIG} ${RATE_LIMIT_PER_SECOND_PER_IP} ${RATE_LIMIT_PER_IP_BURST} ${MAX_CONNECTIONS_PER_IP} ${RETRY_AFTER_HEADER}' < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf
envsubst '${VALKEY_HOST} ${VALKEY_PORT} ${CACHE_TTL_HEADER} ${RETRY_AFTER_HEADER} ${CONDITIONAL_GET_HEADER} ${UNKNOWN_PLAYER_COOLDOWN_KEY_PREFIX} ${UNKNOWN_PLAYERS_CACHE_ENABLED}' < /usr/local/openresty/lualib/valkey_handler.lua.template > /usr/local/openresty/lualib/valkey_handler.lua

# Check OpenResty config before starting
openresty -t

# Start OpenResty
openresty -g "daemon off;"