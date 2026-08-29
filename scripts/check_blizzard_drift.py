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
    PYTHONPATH=. POSTGRES_PASSWORD=x uv run python scripts/check_blizzard_drift.py
"""

import csv
import re
import sys
from datetime import UTC, datetime
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
        row["key"] for row in read_csv_file("heroes") if int(row["health"] or 0) == 0
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


# ── Hitpoints ────────────────────────────────────────────────────────────────
#
# Hitpoints appear nowhere on overwatch.blizzard.com — grep a hero page for
# health/armor/shield markup and there is nothing — so heroes.csv is
# hand-maintained and rots silently after balance patches. Two rows were stale
# for months before anyone noticed.
#
# But while the *values* are unpublished, the *changes* are: patch notes carry
# them as explicit deltas in a stable phrasing, e.g.
#
#     "Health reduced from 300 to 275."
#     "Shield health reduced from 275 to 250."
#
# which is enough. The "from" value is exactly what a stale row still says.

PATCH_NOTES_PATH = "/news/patch-notes/live"
PATCH_NOTE_MONTHS = 3

# Only these three map onto columns we store. Anything else in the notes —
# barrier health, a summoned turret, an ability's temporary shields — is a
# different number and must not be matched.
_HITPOINT_FIELDS = {
    "health": "health",
    "armor": "armor",
    "armor health": "armor",
    "shields": "shields",
    "shield health": "shields",
}
_DELTA_PATTERN = re.compile(
    r"\b(?P<field>Shield health|Armor health|Health|Armor|Shields)\s+"
    r"(?:was\s+)?(?:reduced|increased|lowered|raised)\s+from\s+"
    r"(?P<before>\d+)\s+to\s+(?P<after>\d+)",
    re.IGNORECASE,
)


def _fetch_patch_notes(months: int) -> str:
    """Return the flattened text of the last *months* patch-note pages."""
    now = datetime.now(UTC)
    pages = []
    for offset in range(months):
        year, month = divmod((now.year * 12 + now.month - 1) - offset, 12)
        path = f"{PATCH_NOTES_PATH}/{year}/{month + 1:02d}/"
        try:
            pages.append(fetch(path))
        except httpx2.HTTPError:
            # A month with no notes 404s; that is not drift.
            continue
    text = re.sub(r"<[^>]+>", " ", "\n".join(pages))
    return re.sub(r"\s+", " ", text)


def hitpoint_findings(
    text: str, rows: dict[str, dict[str, str]]
) -> list[tuple[str, str]]:
    """Classify every published hitpoint delta against heroes.csv.

    Returns ``(level, message)`` pairs so this stays pure and testable; the
    caller dispatches them to fail()/warn().

    Deltas are attributed to the most recent hero name preceding them: Blizzard
    lays the notes out as "<Hero> <rationale> <change>", so the nearest
    preceding name is the subject.
    """
    findings: list[tuple[str, str]] = []

    name_positions = sorted(
        (m.start(), name)
        for name in rows
        for m in re.finditer(rf"\b{re.escape(name)}\b", text)
    )
    if not name_positions:
        return [("warn", "no hero names found in the patch notes")]

    # Self-calibrating plausibility bound. "Health reduced from 1500 to 1100" is
    # Reinhardt's barrier, not Reinhardt; "from 25 to 15" is some ability's
    # temporary shields. Neither is a number we store, so they are noise.
    # Deriving the range from our own columns avoids magic numbers, and it
    # cannot weaken detection: a mismatch requires our value to equal the
    # "from" value, which is in range by construction.
    plausible = {
        column: {
            int(row[column])
            for row in rows.values()
            if row[column] and int(row[column]) > 0
        }
        for column in ("health", "armor", "shields")
    }

    for match in _DELTA_PATTERN.finditer(text):
        column = _HITPOINT_FIELDS[match["field"].lower()]
        before, after = match["before"], match["after"]

        known = plausible[column]
        if known and not (min(known) <= int(before) <= max(known)):
            continue

        preceding = [name for pos, name in name_positions if pos < match.start()]
        if not preceding:
            continue
        hero = preceding[-1]
        ours = rows[hero][column]

        if ours == after:
            continue
        if ours == before:
            # Unambiguous: we still hold the pre-patch value.
            findings.append(
                (
                    "fail",
                    (
                        f"{hero} {column} is {ours}, but Blizzard changed it to "
                        f"{after} (patch note: {column} from {before} to {after})"
                    ),
                )
            )
        else:
            # Either the delta belongs to something else on the page, or the
            # value was wrong before the patch too. Worth a look, not a red run.
            findings.append(
                (
                    "warn",
                    (
                        f"{hero} {column} is {ours}; a patch note says {before} "
                        f"-> {after}. Check whether the note refers to this "
                        f"hero's own hitpoints."
                    ),
                )
            )

    return findings


def check_hitpoints_against_patch_notes() -> None:
    """Compare heroes.csv hitpoints against the deltas Blizzard published."""
    print("=== hitpoints vs patch notes ===")

    rows = {row["name"]: row for row in read_csv_file("heroes")}
    text = _fetch_patch_notes(PATCH_NOTE_MONTHS)
    if not text.strip():
        warn("no patch notes could be fetched — hitpoints not verified")
        return

    findings = hitpoint_findings(text, rows)
    for level, message in findings:
        (fail if level == "fail" else warn)(message)

    print(f"  checked {len(_DELTA_PATTERN.findall(text))} published change(s)")


# ── Maps ─────────────────────────────────────────────────────────────────────
#
# Blizzard publishes no map list: /en-us/maps/ is an 8KB shell with zero map
# names, /en-us/maps/data/ 404s, and the rates JSON only echoes the map you
# asked for. Patch notes announce new maps, but in twelve different phrasings
# across three years — and four maps (Arena Victoriae, Gogadoro, Place Lacroix,
# Redwood Dam) were never named in any patch note at all, so a regex over them
# would miss a third of releases while inventing false positives from lines like
# "New Holiday decorations have been added to the following maps".
#
# The one structured source is the map dropdown on the hero-rates page, which is
# server-rendered *only when query parameters are present* — the bare URL serves
# the shell. Its option values are byte-identical to our key column and the
# enclosing optgroup gives the gamemode.
#
# It covers the live rotation only (30 of our 58): no arcade, retired, workshop
# or Stadium-exclusive maps. That is a real limit, not an oversight — those have
# no Blizzard-published source and stay manual.

RATES_PATH = "/rates/?input=PC&map=all-maps&role=All&region=europe&tier=All"
_MAP_SELECT = re.compile(r'<select[^>]*id="filter-map-select".*?</select>', re.DOTALL)
_OPTGROUP = re.compile(
    r'<optgroup[^>]*label="([^"]+)"(.*?)(?=<optgroup|</select>)', re.DOTALL
)
_OPTION = re.compile(r'<option[^>]*data-title="([^"]*)"[^>]*value="([a-z0-9-]+)"')


def _normalise(name: str) -> str:
    """Blizzard writes King's Row with an ASCII apostrophe, we use a typographic
    one. That is a typography choice, not drift."""
    return name.replace("\u2019", "'").casefold().strip()


def parse_rotation_maps(html: str) -> dict[str, tuple[str, str]]:
    """Return ``{key: (name, gamemode)}`` from the rates page map dropdown."""
    select = _MAP_SELECT.search(html)
    if not select:
        return {}

    maps = {}
    for group in _OPTGROUP.finditer(select.group(0)):
        gamemode = group.group(1).strip().casefold().replace(" ", "-")
        for option in _OPTION.finditer(group.group(2)):
            maps[option.group(2)] = (option.group(1), gamemode)
    return maps


def check_maps_in_rotation() -> None:
    """Every map Blizzard currently rotates must exist in maps.csv."""
    print("=== maps in live rotation ===")

    live = parse_rotation_maps(fetch(RATES_PATH))
    if not live:
        warn("could not read the map dropdown — the rates page layout may have changed")
        return

    ours = {row["key"]: row for row in read_csv_file("maps")}

    for key, (name, gamemode) in sorted(live.items()):
        row = ours.get(key)
        if row is None:
            fail(
                f"map {key!r} ({name}, {gamemode}) is in rotation but missing from maps.csv"
            )
            continue
        if gamemode not in row["gamemodes"].split(","):
            fail(
                f"map {key!r} is {gamemode} on Blizzard but "
                f"{row['gamemodes']!r} in maps.csv"
            )
        elif _normalise(name) != _normalise(row["name"]):
            warn(f"map {key!r} is named {name!r} on Blizzard, {row['name']!r} here")

    print(f"  checked {len(live)} maps in rotation against {len(ours)} known")


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
        check_hitpoints_against_patch_notes()
        check_maps_in_rotation()
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
