"""Invariants over the hand-maintained CSVs and their assets.

These are the only data checks that need no external source at all, which makes
them the ones that cannot rot: hitpoints depend on Blizzard's patch notes and
map rosters on Blizzard announcing them, but nothing outside this repo decides
whether a row has its screenshot or whether the files agree with each other.
"""

import csv
from pathlib import Path

import pytest

DATA_DIR = Path("app/domain/utils/data")
STATIC_DIR = Path("static")


def _rows(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / f"{name}.csv").open(encoding="utf-8") as file:
        return list(csv.DictReader(file))


@pytest.mark.parametrize("name", ["heroes", "maps", "gamemodes"])
def test_keys_are_unique(name: str):
    keys = [row["key"] for row in _rows(name)]

    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("name", ["heroes", "maps", "gamemodes"])
def test_rows_are_alphabetical(name: str):
    """scripts/check_blizzard_drift.py --fix inserts new rows by alphabetical
    position, so an unsorted file makes it drop them somewhere arbitrary."""
    keys = [row["key"] for row in _rows(name)]

    assert keys == sorted(keys)


# country_code is legitimately blank where there is no country: Horizon Lunar
# Colony is on the moon, Numbani is fictional, the Workshop maps are abstract
# spaces. The response model types it `str | None` for exactly this reason.
_NULLABLE = {("maps", "country_code")}

# maps.py declares country_code with min_length=2, max_length=2.
_COUNTRY_CODE_LENGTH = 2


@pytest.mark.parametrize("name", ["heroes", "maps", "gamemodes"])
def test_no_row_has_an_unexpectedly_empty_field(name: str):
    empty = [
        f"{row['key']}.{field}"
        for row in _rows(name)
        for field, value in row.items()
        if not (value or "").strip() and (name, field) not in _NULLABLE
    ]

    assert empty == []


def test_country_codes_are_two_letters_when_present():
    """The response model declares min_length=2, max_length=2, so anything else
    in the CSV is a 500 waiting for the first request to that map."""
    bad = [
        f"{row['key']}={row['country_code']!r}"
        for row in _rows("maps")
        if row["country_code"] and len(row["country_code"]) != _COUNTRY_CODE_LENGTH
    ]

    assert bad == []


class TestCrossFileReferences:
    def test_every_gamemode_a_map_claims_is_defined(self):
        """maps.csv's gamemodes column is the value space for MapGamemode, which
        is generated from gamemodes.csv — a typo here becomes a broken enum."""
        defined = {row["key"] for row in _rows("gamemodes")}
        used = {
            gamemode
            for row in _rows("maps")
            for gamemode in row["gamemodes"].split(",")
        }

        assert used - defined == set()

    def test_every_defined_gamemode_is_used_by_a_map(self):
        """A gamemode no map serves is either a missing map or a dead row."""
        defined = {row["key"] for row in _rows("gamemodes")}
        used = {
            gamemode
            for row in _rows("maps")
            for gamemode in row["gamemodes"].split(",")
        }

        assert defined - used == set()


class TestAssets:
    """The route tests assert every row has its file. These assert the reverse —
    a file with no row is a renamed key that left its old asset behind, and
    nothing else would notice."""

    def test_no_orphan_map_screenshots(self):
        expected = {f"{row['key']}.jpg" for row in _rows("maps")}
        present = {path.name for path in (STATIC_DIR / "maps").iterdir()}

        assert present - expected == set()

    def test_no_orphan_gamemode_assets(self):
        expected = {f"{row['key']}-icon.svg" for row in _rows("gamemodes")} | {
            f"{row['key']}.avif" for row in _rows("gamemodes")
        }
        present = {path.name for path in (STATIC_DIR / "gamemodes").iterdir()}

        assert present - expected == set()
