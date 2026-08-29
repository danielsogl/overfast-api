"""Daily canary: run the real parsers against the live Blizzard site.

The test suite parses captured fixtures in tests/fixtures/, so it stays green
when Blizzard changes its markup or ships content this repo does not know
about. Nothing else notices until a user reports wrong data — the worker's old
check_new_hero task could only send a Discord message, and it runs on the VPS
with no access to this repository.

This closes that gap using the project's own parsers, so there is no second
copy of the scraping logic to keep in sync. It deliberately does *not* run on
pull requests: Blizzard being slow or down must never block a merge.

Exits non-zero when it finds drift, which turns the scheduled workflow red.

Run locally with:
    POSTGRES_PASSWORD=x uv run python scripts/check_blizzard_drift.py
"""

import csv
import sys
from pathlib import Path

import httpx2

from app.config import settings
from app.domain.enums import HeroGamemode, HeroKey, Role
from app.domain.parsers.hero import parse_hero_html
from app.domain.parsers.heroes import parse_heroes_html
from app.domain.parsers.roles import parse_roles_html
from app.domain.utils.csv_reader import read_csv_file

LOCALE = "en-us"
TIMEOUT = 30

# --fix writes the mechanical part of a new hero into heroes.csv instead of only
# reporting it. Used by the scheduled workflow, which opens a PR with the result.
FIX = "--fix" in sys.argv

failures: list[str] = []
warnings: list[str] = []


def fail(message: str) -> None:
    print(f"  FAIL: {message}")
    failures.append(message)


def warn(message: str) -> None:
    print(f"  WARN: {message}")
    warnings.append(message)


def fetch(path: str) -> str:
    """Fetch a Blizzard page, mirroring what the app's client asks for."""
    url = f"{settings.blizzard_host}/{LOCALE}{path}"
    response = httpx2.get(
        url,
        headers={"Accept": "text/html"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def add_heroes_to_csv(new_heroes: list[dict]) -> None:
    """Insert new heroes into heroes.csv, alphabetically, with zeroed hitpoints.

    Only key/name/role can be filled from the Blizzard page. Hitpoints appear
    nowhere on the site, so they land as 0 and the check above keeps failing
    until someone supplies them — the point is to remove the mechanical part of
    the edit, not to pretend the data is complete.

    Existing rows are left exactly as they are; the file is only nearly sorted
    and reordering it would bury the real change in noise.
    """
    path = (
        Path(__file__).parent.parent
        / "app"
        / "domain"
        / "utils"
        / "data"
        / "heroes.csv"
    )
    with path.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    for hero in sorted(new_heroes, key=lambda h: h["key"]):
        row = {
            "key": hero["key"],
            "name": hero["name"],
            "role": hero["role"],
            "health": "0",
            "armor": "0",
            "shields": "0",
        }
        position = next(
            (i for i, existing in enumerate(rows) if existing["key"] > hero["key"]),
            len(rows),
        )
        rows.insert(position, row)
        print(f"  added {hero['key']!r} to heroes.csv (hitpoints left at 0)")

    with path.open("w", encoding="utf-8", newline="") as csv_file:
        # csv defaults to CRLF, which rewrites every line of a LF file and buries
        # a one-hero change in a whole-file diff.
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    # The cached reader would otherwise hand back the pre-write rows.
    read_csv_file.cache_clear()


def check_heroes() -> list[dict]:
    """Parse the live heroes index and compare its keys with the HeroKey enum."""
    print("=== heroes index ===")
    heroes = parse_heroes_html(fetch(settings.heroes_path))

    if not heroes:
        fail("the heroes page parsed to an empty list — markup likely changed")
        return []
    print(f"  parsed {len(heroes)} heroes")

    live_keys = {hero["key"] for hero in heroes}
    known_keys = {key.value for key in HeroKey}

    # Blizzard has heroes we don't: our data is incomplete and /heroes is wrong.
    if missing := sorted(live_keys - known_keys):
        fail(
            f"new hero(es) on Blizzard missing from heroes.csv: {', '.join(missing)}. "
            "Add key/name/role; health/armor/shields are not published by Blizzard "
            "and must be filled in by hand."
        )
        if FIX:
            add_heroes_to_csv([h for h in heroes if h["key"] in set(missing)])

    # An auto-added row carries zeroed hitpoints, which no real hero has. Fail
    # until a human fills them in, so an unfinished row cannot sit in main
    # quietly serving wrong data.
    if unfilled := sorted(
        row["key"]
        for row in read_csv_file("heroes")
        if int(row["health"] or 0) == 0  # ty: ignore[invalid-argument-type]
    ):
        fail(
            f"hero(es) in heroes.csv with health=0: {', '.join(unfilled)}. "
            "These rows were added automatically — fill in health/armor/shields "
            "from the in-game hero screen."
        )

    # We have heroes Blizzard doesn't. Legitimate for an unreleased hero added
    # ahead of time, so report without failing.
    if extra := sorted(known_keys - live_keys):
        warn(f"in heroes.csv but not on the live page: {', '.join(extra)}")

    for field in ("key", "name", "portrait", "role"):
        if any(not hero.get(field) for hero in heroes):
            fail(f"some heroes parsed with an empty {field!r}")

    invalid_roles = {h["role"] for h in heroes} - {r.value for r in Role}
    if invalid_roles:
        fail(f"unknown role(s) on the heroes page: {sorted(invalid_roles)}")

    known_gamemodes = {g.value for g in HeroGamemode}
    live_gamemodes = {gm for hero in heroes for gm in hero.get("gamemodes") or []}
    if unknown := sorted(live_gamemodes - known_gamemodes):
        fail(f"unknown gamemode(s) on the heroes page: {', '.join(unknown)}")

    return heroes


def check_hero_detail(hero_key: str) -> None:
    """Parse one hero page — the richest parser, and the most fragile."""
    print(f"=== hero detail ({hero_key}) ===")
    hero = parse_hero_html(fetch(f"{settings.heroes_path}{hero_key}/"))

    for field in ("name", "description", "role", "abilities"):
        if not hero.get(field):
            fail(f"hero {hero_key!r} parsed with an empty {field!r}")

    if abilities := hero.get("abilities"):
        print(f"  parsed {len(abilities)} abilities")
        if any(not a.get("name") or not a.get("description") for a in abilities):
            fail(f"hero {hero_key!r} has abilities missing a name or description")


def check_roles() -> None:
    """Parse the roles block off the home page."""
    print("=== roles ===")
    roles = parse_roles_html(fetch(settings.home_path))

    expected = {r.value for r in Role}
    live = {role["key"] for role in roles}
    if live != expected:
        fail(f"roles mismatch: expected {sorted(expected)}, parsed {sorted(live)}")
    else:
        print(f"  parsed {len(roles)} roles")

    if any(not role.get("description") or not role.get("icon") for role in roles):
        fail("some roles parsed without a description or icon")


def main() -> int:
    print(f"Checking {settings.blizzard_host} against the parsers in this repo.\n")

    try:
        heroes = check_heroes()
        # Pick a hero Blizzard is currently serving, so a rename cannot make
        # this check fail for the wrong reason.
        if heroes:
            check_hero_detail(min(h["key"] for h in heroes))
        check_roles()
    except httpx2.HTTPError as exc:
        # Network trouble is not drift; say so rather than reporting a false
        # positive, but still exit non-zero so the run is not silently green.
        print(f"\n::error::Could not reach Blizzard: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n::error::A parser raised {type(exc).__name__}: {exc}")
        print("This usually means Blizzard changed its markup.")
        return 1

    print()
    if warnings:
        print(f"{len(warnings)} warning(s) — no action strictly required.")
    if failures:
        print(f"::error::{len(failures)} drift finding(s) against live Blizzard data.")
        return 1

    print("No drift: the live site still matches what this repo expects.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
