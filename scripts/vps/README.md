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

## Git access: why the repo is fetched over SSH

`origin` on the host is `git@github.com:...`, not the HTTPS URL you would get
from a plain `git clone`. That is deliberate.

On 2026-09-02 a deploy failed at its very first step:

```
[12:13:19] Fetching latest changes...
fatal: could not read Username for 'https://github.com': No such device or address
```

The repo is public and the URL was correct. GitHub was answering *anonymous*
git-over-HTTPS from this datacenter IP with `401 www-authenticate: Basic
realm="GitHub"` — for every repository, `git/git` included, while plain `curl`
to the same endpoint still returned 200. It cleared on its own within the hour,
which is the problem: it is throttling, it is invisible until a deploy dies, and
it will come back.

Authenticated access is not subject to that throttle, so the host uses a
**read-only deploy key**:

```bash
ssh-keygen -t ed25519 -N "" -C "overfast-api-prod deploy (read-only)" \
    -f /root/.ssh/id_ed25519_overfast_deploy
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts
# verify against GitHub's published fingerprint before trusting it:
#   SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
gh repo deploy-key add /root/.ssh/id_ed25519_overfast_deploy.pub \
    -R danielsogl/overfast-api -t "overfast-api-prod (read-only)"
git -C /opt/overfast-api remote set-url origin git@github.com:danielsogl/overfast-api.git
```

`/root/.ssh/config` pins that key to `github.com` with `IdentitiesOnly yes`, so
ssh does not offer anything else first.

Read-only is not a formality — the host never pushes, and a key that cannot
write is one that cannot rewrite history if the machine is compromised. Verify
it stayed that way with `git push --dry-run`, which must be rejected.

`upstream` stays on HTTPS. A deploy key is scoped to one repository, so it
cannot reach TeKrop's, and nothing automated fetches upstream anyway — it is
there for manual cherry-picks.

## Health check: alerting

`healthcheck.sh` detects an unreachable API or an unhealthy container, logs it,
and deduplicates so a single outage does not spam. It sends one message when the
outage starts and one when it clears — a down alert with no all-clear is
indistinguishable from an ongoing outage.

Where it sends them is `ALERT_WEBHOOK_URL` in `.env`:

```
ALERT_WEBHOOK_URL=https://hooks.slack.com/services/...
```

The POST body carries the message under both `text` and `content`, which Slack
and Discord read respectively; anything else accepting a JSON POST works too.
The URL is deliberately generic — the box has no MTA, cron's non-zero exit goes
nowhere, and choosing a channel is not this script's job.

**Leave it empty and nothing is sent.** That is the shipped default and a valid
choice: an outage at 03:00 is then visible only in
`/var/log/overfast-health.log`. A failing webhook is logged as a warning and
never masks the outage it was meant to report.
