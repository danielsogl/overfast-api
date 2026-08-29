"""Stateless builders for player snapshot history.

A snapshot is the small, differenceable part of a parsed profile: competitive
ranks, endorsement level, and the three *cumulative* per-hero counters Blizzard
exposes on the career page. Everything else on that page is either a static
asset URL or an average, and an average cannot be differenced meaningfully —
subtracting two "eliminations per 10 minutes" values says nothing about what
happened in between.

Rows are stored once per profile version and kept for a year, so the payload is
deliberately narrow: icons, labels and averages are all left out.
"""

from app.domain.enums import CareerHeroesComparisonsCategory

# The only heroes_comparisons categories that accumulate. Ordering is irrelevant;
# the set is what the two functions below agree on.
SNAPSHOT_HERO_CATEGORIES = (
    CareerHeroesComparisonsCategory.TIME_PLAYED.value,
    CareerHeroesComparisonsCategory.GAMES_WON.value,
    CareerHeroesComparisonsCategory.WIN_PERCENTAGE.value,
)

# Cumulative counters we report a delta for. ``win_percentage`` is excluded on
# purpose: it is a ratio, so its difference is not a quantity.
_DELTA_KEYS = (
    CareerHeroesComparisonsCategory.TIME_PLAYED.value,
    CareerHeroesComparisonsCategory.GAMES_WON.value,
)

_WIN_PERCENTAGE = CareerHeroesComparisonsCategory.WIN_PERCENTAGE.value


def build_player_snapshot(parsed_profile: dict) -> dict | None:
    """Reduce a parsed profile to the payload stored in ``player_snapshots``.

    ``parsed_profile`` is the dict returned by ``parse_player_profile_html``:
    ``{"summary": ..., "stats": ...}``.

    Returns ``None`` when the profile carries neither ranks nor stats — a
    private profile has nothing to record, and storing an empty row would only
    add a meaningless point to the series.
    """
    summary = parsed_profile.get("summary") or {}
    competitive = _build_competitive(summary.get("competitive"))
    heroes = _build_heroes(parsed_profile.get("stats"))

    if not competitive and not heroes:
        return None

    endorsement = summary.get("endorsement") or {}
    return {
        "endorsement": endorsement.get("level"),
        "competitive": competitive,
        "heroes": heroes,
    }


def _build_competitive(competitive: dict | None) -> dict:
    """Keep ``{division, tier}`` per platform per role, dropping every icon URL.

    The three icon fields are static CDN links that never differ between two
    snapshots of the same rank, so storing them would roughly triple the row for
    no readable information.
    """
    if not competitive:
        return {}

    ranks: dict[str, dict] = {}
    for platform, platform_ranks in competitive.items():
        if not platform_ranks:
            continue
        platform_entry = {
            role: {"division": rank["division"], "tier": rank["tier"]}
            # "season" sits alongside the roles in this dict and is an int, not
            # a rank; unranked roles are None.
            for role, rank in platform_ranks.items()
            if isinstance(rank, dict) and "division" in rank and "tier" in rank
        }
        if platform_entry:
            ranks[platform] = platform_entry

    return ranks


def _build_heroes(stats: dict | None) -> dict:
    """Extract the cumulative per-hero counters, keyed platform → gamemode → hero."""
    if not stats:
        return {}

    heroes: dict[str, dict] = {}
    for platform, platform_stats in stats.items():
        if not platform_stats:
            continue

        platform_entry: dict[str, dict] = {}
        for gamemode, gamemode_stats in platform_stats.items():
            if not gamemode_stats:
                continue
            gamemode_entry = _build_gamemode_heroes(
                gamemode_stats.get("heroes_comparisons") or {}
            )
            if gamemode_entry:
                platform_entry[gamemode] = gamemode_entry

        if platform_entry:
            heroes[platform] = platform_entry

    return heroes


def _build_gamemode_heroes(heroes_comparisons: dict) -> dict:
    """Pivot ``{category: {values: [{hero, value}]}}`` into ``{hero: {category: value}}``."""
    per_hero: dict[str, dict] = {}
    for category in SNAPSHOT_HERO_CATEGORIES:
        entry = heroes_comparisons.get(category)
        if not entry:
            continue
        for row in entry.get("values") or []:
            per_hero.setdefault(row["hero"], {})[category] = row["value"]

    return per_hero


def diff_player_snapshots(snapshots: list[dict]) -> dict:
    """Compare the oldest and newest of *snapshots* (newest first).

    Fewer than two snapshots is a normal state, not an error: a player whose
    profile we only just started recording has no interval to report. The result
    then carries the count and empty deltas so a client can say "no data yet"
    without special-casing a failure.

    Only entries that actually moved are returned — a rank that did not change
    and a hero that was not played carry no information a caller does not
    already have from the live endpoints.
    """
    empty: dict = {
        "snapshots_compared": len(snapshots),
        "compared_from": None,
        "compared_to": None,
        "ranks": [],
        "heroes": [],
        "totals": dict.fromkeys(_DELTA_KEYS, 0),
    }
    if len(snapshots) < 2:  # noqa: PLR2004
        return empty

    newest, oldest = snapshots[0], snapshots[-1]
    before, after = oldest.get("data") or {}, newest.get("data") or {}

    heroes = _diff_heroes(before.get("heroes") or {}, after.get("heroes") or {})
    return {
        **empty,
        "compared_from": oldest["taken_at"],
        "compared_to": newest["taken_at"],
        "ranks": _diff_ranks(
            before.get("competitive") or {}, after.get("competitive") or {}
        ),
        "heroes": heroes,
        "totals": {
            key: round(sum(hero[key] for hero in heroes), 2) for key in _DELTA_KEYS
        },
    }


def _diff_ranks(before: dict, after: dict) -> list[dict]:
    """Report every platform/role whose rank differs between the two snapshots."""
    movements = []
    for platform in sorted(before.keys() | after.keys()):
        platform_before = before.get(platform) or {}
        platform_after = after.get(platform) or {}
        for role in sorted(platform_before.keys() | platform_after.keys()):
            rank_before = platform_before.get(role)
            rank_after = platform_after.get(role)
            if rank_before != rank_after:
                movements.append(
                    {
                        "platform": platform,
                        "role": role,
                        "before": rank_before,
                        "after": rank_after,
                    }
                )

    return movements


def _diff_heroes(before: dict, after: dict) -> list[dict]:
    """Report every platform/gamemode/hero whose cumulative counters moved."""
    diffs = []
    for platform in sorted(before.keys() | after.keys()):
        platform_before = before.get(platform) or {}
        platform_after = after.get(platform) or {}
        for gamemode in sorted(platform_before.keys() | platform_after.keys()):
            gamemode_before = platform_before.get(gamemode) or {}
            gamemode_after = platform_after.get(gamemode) or {}
            for hero in sorted(gamemode_before.keys() | gamemode_after.keys()):
                diff = _diff_hero(
                    gamemode_before.get(hero) or {}, gamemode_after.get(hero) or {}
                )
                if diff is not None:
                    diffs.append(
                        {
                            "platform": platform,
                            "gamemode": gamemode,
                            "hero": hero,
                            **diff,
                        }
                    )

    return diffs


def _diff_hero(before: dict, after: dict) -> dict | None:
    """Return one hero's deltas, or None when nothing accumulated."""
    deltas = {
        key: round((after.get(key) or 0) - (before.get(key) or 0), 2)
        for key in _DELTA_KEYS
    }
    if not any(deltas.values()):
        return None

    return {
        **deltas,
        "win_percentage_before": before.get(_WIN_PERCENTAGE),
        "win_percentage_after": after.get(_WIN_PERCENTAGE),
    }
