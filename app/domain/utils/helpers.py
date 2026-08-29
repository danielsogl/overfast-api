"""Domain helper utilities"""

from functools import cache

from app.domain.utils.csv_reader import read_csv_file


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
def key_to_label(key: str) -> str:
    """Transform a snake_case key into a human-readable Title Case label."""
    return " ".join(s.capitalize() for s in key.split("_"))
