"""Deciding which change is worth a notification.

The app shows one rank per player -- the best across tank, damage and support,
PC preferred over console -- and its local alert fires when that string
changes. This mirrors that decision server-side so a pushed notification and a
locally-composed one can never disagree about what "your rank" means.

The comparison is between the two most recent snapshots, not between the ends
of a window: a rank that moved and moved back is not news.

The hero half is simpler: a patch says which heroes changed, the snapshot
series says which of those the player actually plays.

The weekly recap is a third, opt-in kind: not "what changed", but "what
happened this week" for one player, built from ``diff_games`` and
``diff_player_snapshots`` over a 7-day window rather than two adjacent
snapshots.
"""

from __future__ import annotations

from collections import Counter

from app.domain.enums import CompetitiveDivision
from app.domain.parsers.player_snapshot import diff_games, diff_player_snapshots

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
    return _rank_move(before, after)


def _rank_move(before: dict | None, after: dict | None) -> str | None:
    """*after* formatted, when it differs from *before* and both are ranked."""
    if before is None or after is None:
        return None
    if (before["division"], before["tier"]) == (after["division"], after["tier"]):
        return None
    return format_rank(after)


def changed_heroes(patch: dict) -> set[str]:
    """Hero keys a patch touched, from one parsed ``/patch-notes`` entry.

    Mirrors the app's own `heroChanges` join exactly: only entries the parser
    resolved to a key (a hero shipped the same day is `None`), and only those
    carrying actual text — a map update is a pair of screenshots with no
    details, and must not badge or announce anything.
    """
    return {
        entry["hero"]
        for section in patch.get("sections") or []
        for entry in section.get("entries") or []
        if entry.get("hero") and (entry.get("details") or entry.get("abilities"))
    }


def hero_alert(changed: set[str], snapshots: list[dict], limit: int = 3) -> list[str]:
    """The changed heroes a device's watched players play most, most first.

    *snapshots* is the newest snapshot of each watched player; their playtime
    is summed, so one device following three players gets one ranked list.

    No minimum playtime: sorting by time played already means "their mains",
    and a hero with a single game only ever surfaces when nothing else did.
    """
    played: Counter[str] = Counter()
    for snapshot in snapshots:
        heroes = ((snapshot.get("data") or {}).get("heroes")) or {}
        for platform in heroes.values():
            for gamemode in platform.values():
                for hero, stats in gamemode.items():
                    if hero in changed:
                        played[hero] += stats.get("time_played") or 0

    return [hero for hero, _ in played.most_common(limit)]


def weekly_recap(snapshots: list[dict]) -> dict | None:
    """The numbers for one player's weekly recap, or None to stay silent.

    *snapshots* is one player's snapshots from the last 7 days, newest first,
    followed by the last one from before the window when there is one.
    Silent when there is no week to compare (fewer than two snapshots) or the
    week held no games — "you didn't play" is not a notification anyone asked
    for, mirroring ``rank_alert``'s reasoning for staying quiet on the
    unranked cases.
    """
    if len(snapshots) < 2:  # noqa: PLR2004
        return None

    newest, oldest = snapshots[0], snapshots[-1]
    before = oldest.get("data") or {}
    after = newest.get("data") or {}

    games = diff_games(before, after)
    if games["games_played"] == 0:
        return None

    played: Counter[str] = Counter()
    for hero in diff_player_snapshots(snapshots)["heroes"]:
        played[hero["hero"]] += hero["time_played"]
    top_hero = played.most_common(1)[0][0] if played else None

    rank = _rank_move(
        highest_rank(before.get("competitive") or {}),
        highest_rank(after.get("competitive") or {}),
    )

    return {
        "games_played": games["games_played"],
        "games_won": games["games_won"],
        "hero": top_hero,
        "rank": rank,
    }
