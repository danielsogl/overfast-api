# CLAUDE.md

Guidance for Claude Code (claude.ai/code) in this repository.

**Read [AGENTS.md](./AGENTS.md) first.** It is the detailed and current reference for the
architecture (four DDD layers, dependency direction), the `just` command set, code style, testing
and coverage rules. This file carries only what AGENTS.md does not: what makes this a *fork*, and
how it reaches production.

## This is a deliberately divergent fork

Forked from [TeKrop/overfast-api](https://github.com/TeKrop/overfast-api). Since 2026-08-29 the
divergence is intentional: upstream develops too slowly, and **`app/` is fork-owned** — fixes land
here directly rather than as upstream PRs. The nightly auto-merge workflow and its `.gitattributes`
merge drivers have been removed.

Upstream is still the fastest source of *Blizzard compatibility* — new heroes, maps and competitive
divisions usually ship there within days, and that work is time-critical (a new rank makes
`CompetitiveDivision(...)` raise; a new hero leaves `/heroes` incomplete). Track it as a
**cherry-pick source**, not a merge parent:

```bash
git remote add upstream https://github.com/TeKrop/overfast-api.git  # once
git fetch upstream --no-tags
git log --oneline HEAD..upstream/main -- app/domain/parsers/ app/domain/utils/data/
git cherry-pick <sha>
```

Roughly 20 commits a year are worth taking; the rest of upstream's traffic is dependabot noise.

## Deployment — this repo is live

- **Never push directly to `main`.** A push to `main` runs
  `.github/workflows/release.yaml`: semantic-release → smoke test → zero-downtime SSH deploy to the
  VPS. Always work on a branch and open a PR.
- To redeploy current `main` without a code change, run that workflow manually
  (`workflow_dispatch`) from the Actions tab.
- **Never deploy with `docker compose down`.** It drops the containers and with them the caches:
  valkey loses the API cache and every player profile has to be refetched from Blizzard behind the
  throttle. `scripts/deploy.sh` rolls containers instead and reloads nginx rather than restarting it.

## Verifying changes

AGENTS.md covers `just check` / `just lint` / `just test`. Two extra gates matter here, because unit
tests pass against captured fixtures and prove nothing about the live stack:

```bash
bash scripts/smoke-test.sh        # boots the compose stack, validates the static endpoints
bash scripts/persistence-test.sh  # asserts postgres survives a container lifecycle
```

Match CI exactly when checking locally — it lints and type-checks the **whole
repo**, including `scripts/`, not just `app/`:

```bash
uv run ruff check . && uv run ruff format --check .
POSTGRES_PASSWORD=x uv run ty check .
```

Both run in CI on every PR. Running the suite outside Docker needs `POSTGRES_PASSWORD` —
`app/config.py` builds `Settings` at import time, so pytest fails during collection without it.

## Things that bite

- The taskiq **worker runs the same `app/api/lifespan.py`** as the API process. Anything there that
  touches the shared cache must be guarded with `if not broker.is_worker_process`.
- **Postgres holds only cache.** `static_data` and `player_profiles` are both regenerable Blizzard
  content, so losing them costs refetch time, not data. The volume must stay mounted at the PGDATA
  path itself — `postgres:17` declares `VOLUME /var/lib/postgresql/data` and will shadow a mount on
  the parent with a throwaway anonymous volume.
- **Hitpoint columns in `heroes.csv` are hand-maintained.** Blizzard publishes them nowhere — grep a
  live hero page for health/armor/shield markup and there is nothing — so they cannot be scraped.
  Use this order of evidence, and never skip a rung:

  1. **Blizzard patch notes** are authoritative and current, but only publish *changes*
     ("Health reduced from 300 to 275"). `scripts/check_blizzard_drift.py` checks these daily and
     fails when a row still holds the pre-patch value. This is the only automated guard.
  2. **The Overwatch wiki infobox** (`overwatch.weirdgloop.org/api.php?action=parse&page=<Hero>`,
     needs a real User-Agent) is the fallback where Blizzard is silent — a new hero's launch values.
     Tanks store the *base* value there; our CSV holds base + 150 for the role-queue passive.
  3. Nothing else. Do not sync from the wiki automatically: it was measurably **stale** (still 250
     for Junkrat two months after Blizzard published 200) and its infoboxes are **incomplete**
     (Sigma, Zarya and Zenyatta carry no `shields` field at all). An auto-sync would have reverted
     a correct value and zeroed three real shield pools.

  A value that was never right *and* never changed in a patch is invisible to every gate — D.Mon
  shipped with D.Va's numbers because the row was copy-pasted. Check a new hero against rung 2.
## Data we cannot scrape

Most hero content is scraped live on every cache miss, so it self-corrects. The
hand-maintained parts do not, and they have all rotted at least once. Each one
below names its best available source and what guards it — the honest entries
are the ones that say *nothing does*.

| Data | Source | Guard |
|---|---|---|
| heroes.csv key/name/role | the live heroes page | `check_heroes`, daily |
| heroes.csv hitpoints | Blizzard patch notes (deltas only) | `check_hitpoints_against_patch_notes`, daily |
| maps.csv in live rotation (30 of 58) | the rates page map dropdown | `check_maps_in_rotation`, daily |
| maps.csv arcade / retired / workshop / Stadium (28) | wiki only | **nothing** — manual |
| maps.csv location, country_code | wiki infobox `{{flag\|xx}}` | **nothing** — manual |
| gamemodes.csv descriptions | none; Blizzard deleted the source | **nothing** — frozen |
| static/maps/*.jpg | none | the route test asserts presence only |
| CSV/asset internal consistency | ourselves | `tests/domain/test_static_data_integrity.py` |

Three things worth knowing before you go looking for a better source:

**Blizzard publishes no map list.** `/en-us/maps/` is an 8 KB shell containing
zero map names, `/en-us/maps/data/` 404s, and the rates JSON only echoes the map
you asked for. The rates *page* is the exception — and only when query
parameters are present, otherwise it serves the same shell.

**Patch notes are not a usable map source.** They announce new maps in twelve
different phrasings across three years, and four maps (Arena Victoriae,
Gogadoro, Place Lacroix, Redwood Dam) were never named in any patch note at all.
A regex over them would miss a third of releases while matching lines like "New
Holiday decorations have been added to the following maps". Hitpoints are
different: there the phrasing is stable *and* the check only fails when our
value equals the published pre-patch one, which no false positive can satisfy.

**The gamemode descriptions came from Blizzard and no longer can.** Until commit
`28ffc84` they were scraped from a carousel on the Overwatch homepage; Blizzard
removed it around December 2024. Nine of the thirteen are still Blizzard's
wording, four never had any. They are ours now — edit them as prose, and do not
go looking for the page.

- The **domain layer must stay framework-agnostic** — `fastapi` is a banned import there, enforced
  by ruff's `TID251`.
