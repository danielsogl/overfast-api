#!/usr/bin/env bash
# Re-build Caddy with the cbrotli plugin and swap the binary in place.
#
# NOT CURRENTLY APPLIED, and deliberately so. Kept because this directory is
# the canonical copy of host procedures, not because the setup is in force.
#
# State as of 2026-09-10: the running binary is the vanilla apt package
# (2.11.4, `caddy list-modules` has no http.encoders.br), there is no
# `apt-mark hold caddy`, and the Caddyfile encodes `zstd gzip` without ever
# listing `br`. The custom build was lost to some earlier apt upgrade because
# the hold was never actually set — nothing noticed, because nothing used it.
#
# Do not "restore" this without measuring first. The reason to want brotli
# looks compelling and is not: 65% of requests come from the iOS app, which
# sends `gzip, deflate, br` and no zstd, so it is served gzip today. But these
# payloads are 1-13 KB, small enough that the window advantages of br/zstd
# barely apply:
#
#   /heroes      identity 13156 B  ->  gzip 3168 B  ->  zstd 3121 B
#   /gamemodes                         gzip 1148 B  ->  zstd 1203 B
#
# zstd wins 1.5% on one route and loses on the other. Brotli lands in the same
# band. That is not worth a hand-built binary on the edge proxy plus an
# apt-mark hold that would freeze Caddy's security updates.
#
# Revisit if response sizes grow by an order of magnitude, then re-measure.
#
# Usage: /opt/rebuild-caddy.sh [VERSION]
#   default VERSION = whatever 'caddy version' currently reports
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
