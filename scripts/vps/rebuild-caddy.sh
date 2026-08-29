#!/usr/bin/env bash
# Re-build Caddy with the cbrotli plugin and swap the binary in place.
#
# Usage: /opt/rebuild-caddy.sh [VERSION]
#   default VERSION = whatever 'caddy version' currently reports
#
# Required because apt's caddy package ships a vanilla binary without
# the cbrotli plugin we want for compression. The package is held via
# 'apt-mark hold caddy' to prevent apt-get upgrade from clobbering us.
set -euo pipefail

VERSION=${1:-$(caddy version | awk '{print $1}')}
echo "building caddy $VERSION with cbrotli..."

cd /tmp
CGO_ENABLED=1 xcaddy build "$VERSION" --with github.com/dunglas/caddy-cbrotli

if ! ./caddy list-modules | grep -qx 'http.encoders.br'; then
    echo 'ERROR: brotli encoder missing in built binary' >&2
    exit 1
fi

cp /usr/bin/caddy "/usr/bin/caddy.bak.$(date +%Y%m%d-%H%M%S)"
install -m 0755 -o root -g root /tmp/caddy /usr/bin/caddy
rm /tmp/caddy
caddy version

systemctl restart caddy
sleep 2
systemctl is-active caddy
echo 'done.'
