"""Deciding which rank change is worth a notification.

The app shows one rank per player -- the best across tank, damage and support,
PC preferred over console -- and its local alert fires when that string
changes. This mirrors that decision server-side so a pushed notification and a
locally-composed one can never disagree about what "your rank" means.

The comparison is between the two most recent snapshots, not between the ends
of a window: a rank that moved and moved back is not news.
"""

from __future__ import annotations

from app.domain.enums import CompetitiveDivision

# Declaration order is ascending, so the index is the division's strength.
_DIVISION_STRENGTH = {
    division.value: index for index, division in enumerate(CompetitiveDivision)
}

# Tier 1 is the top of a division and tier 5 the bottom, so it is inverted. The
# multiplier only has to exceed the tier span; 10 matches the app's own formula.
_TIER_SPAN = 10


def _rank_strength(rank: dict) -> int:
    return _DIVISION_STRENGTH[rank["division"]] * _TIER_SPAN + (6 - rank["tier"])


def format_rank(rank: dict) -> str:
    """``{"division": "diamond", "tier": 2}`` -> ``"Diamond 2"``.

    Untranslated on purpose: the app renders the same English string in every
    language, and a notification that disagreed with the card it links to
    would read like a different player.
    """
    return f"{rank['division'].capitalize()} {rank['tier']}"


def highest_rank(competitive: dict) -> dict | None:
    """The best rank across roles, PC preferred, or None when unranked."""
    platform = competitive.get("pc") or competitive.get("console") or {}
    ranks = [
        rank
        for rank in platform.values()
        if isinstance(rank, dict) and "division" in rank and "tier" in rank
    ]
    if not ranks:
        return None
    return max(ranks, key=_rank_strength)


def rank_alert(snapshots: list[dict]) -> str | None:
    """The rank to announce for *snapshots* (newest first), or None.

    Returns None when there is nothing to say: fewer than two snapshots, a
    player who is or was unranked, or a highest rank that did not move. The
    unranked cases stay silent deliberately -- "you no longer have a rank" is
    not a notification anyone asked for, and the first rank a player ever earns
    arrives while they are looking at the game, not at us.
    """
    if len(snapshots) < 2:  # noqa: PLR2004
        return None

    after = highest_rank((snapshots[0].get("data") or {}).get("competitive") or {})
    before = highest_rank((snapshots[1].get("data") or {}).get("competitive") or {})
    if before is None or after is None:
        return None
    if (before["division"], before["tier"]) == (after["division"], after["tier"]):
        return None

    return format_rank(after)
