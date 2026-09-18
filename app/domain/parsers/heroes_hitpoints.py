"""Stateless parser functions for heroes hitpoints data (HP, armor, shields) from CSV"""

from app.domain.utils.csv_reader import read_csv_file

HITPOINTS_KEYS = {"health", "armor", "shields"}


def parse_heroes_hitpoints() -> dict[str, dict]:
    """Parse heroes hitpoints (health/armor/shields) from the heroes CSV file.

    Returns:
        Dict mapping hero key to hitpoints data, 5v5 and 6v6.
        Example: {"ana": {"hitpoints": {"health": 200, ...}, "hitpoints_6v6": {...}}}
    """
    csv_data = read_csv_file("heroes")

    return {
        row["key"]: {
            "hitpoints": _get_hitpoints(row),
            "hitpoints_6v6": _get_hitpoints(row, suffix="_6v6"),
        }
        for row in csv_data
    }


def _get_hitpoints(row: dict, suffix: str = "") -> dict:
    """Extract hitpoints data from a hero CSV row, for the mode ``suffix`` names."""
    hitpoints = {hp_key: int(row[hp_key + suffix]) for hp_key in HITPOINTS_KEYS}
    hitpoints["total"] = sum(hitpoints.values())
    return hitpoints
