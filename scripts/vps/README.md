# VPS host scripts

Everything here runs **on the VPS itself**, outside Docker. Until now these
files existed only at `/opt` on the host and nowhere else — if the machine were
lost, the deploy wrapper, the health check, the Caddy rebuild procedure and the
log rotation would have gone with it, and each is load-bearing.

This directory is the canonical copy. `/opt` holds the deployed copies.

## What is installed where

| Repo file | Installed as | Trigger |
|---|---|---|
| `deploy-wrapper.sh` | `/opt/deploy-overfast.sh` | GitHub Actions over SSH |
| `healthcheck.sh` | run from this repo path | cron, every 5 min |
| `rebuild-caddy.sh` | `/opt/rebuild-caddy.sh` | manual, after a Caddy release |
| `logrotate-overfast` | `/etc/logrotate.d/overfast` | logrotate, weekly |
| `../backup-postgres.sh` | run from this repo path | cron, 04:00 UTC |

**`deploy-wrapper.sh` cannot run from the repo.** It is what fetches and resets
the repo, so it has to exist before the repo is current. It is the one file that
must be copied to `/opt` by hand and kept in sync deliberately.

The health check and the backup run straight out of `/opt/overfast-api/scripts/`,
so a deploy updates them. That is the point: versioned *and* deployed.

## Reinstalling on a fresh host

```bash
install -m 0755 scripts/vps/deploy-wrapper.sh /opt/deploy-overfast.sh
install -m 0755 scripts/vps/rebuild-caddy.sh  /opt/rebuild-caddy.sh
install -m 0644 scripts/vps/logrotate-overfast /etc/logrotate.d/overfast

crontab -l 2>/dev/null | { cat; cat <<'CRON'; } | crontab -
*/5 * * * * /opt/overfast-api/scripts/vps/healthcheck.sh >/dev/null 2>&1
0 4 * * * /opt/overfast-api/scripts/backup-postgres.sh >>/var/log/overfast-backup.log 2>&1
CRON
```

## Health check: no alerting

`healthcheck.sh` detects an unreachable API or an unhealthy container, logs it,
and deduplicates so a single outage does not spam. It then does **nothing** —
the notification call is an empty placeholder, and the box has no MTA, so a
non-zero exit from cron goes nowhere either.

That is a deliberate open gap, not an oversight: the monitoring stack was
removed on purpose and no alert channel has been chosen. Until one is, an
outage at 03:00 is visible only in `/var/log/overfast-health.log`. Filling in
the placeholder needs one `curl` and a URL, nothing more.
