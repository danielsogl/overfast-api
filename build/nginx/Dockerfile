# Build arguments
ARG OPENRESTY_VERSION=1.31.1.1-2

# Use the OpenResty Alpine base image
FROM openresty/openresty:${OPENRESTY_VERSION}-alpine-fat

# Environment variables
ARG OPENRESTY_VERSION
ENV OPENRESTY_VERSION=${OPENRESTY_VERSION}

# For envsubst command in entrypoint
RUN apk add --no-cache gettext git zstd-dev

# Install lua-zstd and lua-resty-prometheus.
# Retried: every deploy rebuilds this layer and fetches both rocks from
# luarocks.org, so a hiccup there fails the deploy outright. That happened
# once already — `fetch.lua:144: attempt to concatenate local 'name' (a nil
# value)` while resolving the download URL — and the identical build had
# succeeded in CI minutes before and on a plain rerun after.
RUN for attempt in 1 2 3; do \
        if luarocks install lua-zstd && luarocks install nginx-lua-prometheus; then \
            break; \
        fi; \
        [ "$attempt" -lt 3 ] || exit 1; \
        echo ">>> luarocks attempt ${attempt}/3 failed, retrying in $((attempt * 5))s..." >&2; \
        sleep $((attempt * 5)); \
    done

# Copy main Nginx configuration template
COPY nginx.conf.template /etc/nginx/nginx.conf.template

# Copy Nginx configuration files
COPY overfast-api.conf.template /etc/nginx/conf.d/default.conf.template
# Copy Prometheus templates to a separate directory (not conf.d to avoid auto-loading)
COPY prometheus_shared_dict.conf /etc/nginx/prometheus-templates/prometheus_shared_dict.conf
COPY lua/prometheus_init_worker.conf /etc/nginx/prometheus-templates/prometheus_init_worker.conf
COPY lua/prometheus_log.conf /etc/nginx/prometheus-templates/prometheus_log.conf
COPY prometheus_metrics_server.conf /etc/nginx/prometheus-templates/prometheus_metrics_server.conf

# Copy Lua scripts
COPY lua/valkey_handler.lua.template /usr/local/openresty/lualib/valkey_handler.lua.template

# Add an entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
