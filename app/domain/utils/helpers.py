"""Domain helper utilities"""

from __future__ import annotations

import unicodedata
from functools import cache
from typing import TYPE_CHECKING, Any

from app.domain.utils.csv_reader import read_csv_file

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@cache
def get_hero_name(hero_key: str) -> str:
    """Return the display name for a hero key, falling back to the key itself."""
    heroes_data = read_csv_file("heroes")
    return next(
        (
            hero_data["name"]
            for hero_data in heroes_data
            if hero_data["key"] == hero_key
        ),
        hero_key,
    )


@cache
def get_hero_key(hero_name: str) -> str | None:
    """Return the hero key for a display name, or ``None`` when it doesn't resolve.

    Callers must handle ``None``: Blizzard names a hero in its patch notes on
    release day, before heroes.csv has the row, and the display names are
    localised while the CSV is English-only.
    """
    heroes_data = read_csv_file("heroes")
    normalized_name = hero_name.casefold().strip()
    return next(
        (
            hero_data["key"]
            for hero_data in heroes_data
            if hero_data["name"].casefold() == normalized_name
        ),
        None,
    )


@cache
def normalize_hero_name(hero_name: str) -> str:
    """Normalise a hero display name so two spellings of it compare equal.

    Casefold, drop combining accents and collapse whitespace — enough to survive
    the casing and accent differences between Blizzard's own pages ("Écho" and
    "ECHO" both become "echo"). Deliberately no further cleverness: punctuation
    stays, so "Soldat : 76" never collapses onto another name. A wrong hero
    mapping is worse than no mapping at all.
    """
    decomposed = unicodedata.normalize("NFKD", hero_name)
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(unaccented.casefold().split())


def build_hero_key_index(heroes: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Build a normalised display-name → hero key index from a heroes list.

    The heroes list is Blizzard's own, scraped per locale, so the index resolves
    localised names ("Chacal") that the English-only heroes.csv cannot.
    """
    return {
        normalize_hero_name(hero["name"]): hero["key"]
        for hero in heroes
        if hero.get("name") and hero.get("key")
    }


@cache
def key_to_label(key: str) -> str:
    """Transform a snake_case key into a human-readable Title Case label."""
    return " ".join(s.capitalize() for s in key.split("_"))
