"""Stateless parser for player profile data from Blizzard career page

This module handles parsing of player profile HTML including:
- Player summary (username, avatar, title, endorsement, competitive ranks)
- Player stats across platforms, gamemodes, and heroes
- Heroes comparisons (top heroes by category)
- Career stats (detailed statistics per hero)
"""

from http import HTTPStatus
from typing import TYPE_CHECKING

from app.config import settings
from app.domain.exceptions import ParserBlizzardError, ParserParsingError
from app.domain.parsers.utils import (
    build_blizzard_url,
    extract_blizzard_id_from_url,
    parse_html_root,
    validate_response_status,
)

if TYPE_CHECKING:
    from selectolax.lexbor import LexborNode

    from app.domain.ports import BlizzardClientPort

from app.domain.enums import (
    CareerHeroesComparisonsCategory,
    CareerStatCategory,
    CompetitiveRole,
    HeroKey,
    PlayerGamemode,
    PlayerPlatform,
)
from app.domain.parsers.player_helpers import (
    CAREER_COMPARISON_CATEGORY_IDS,
    get_computed_stat_value,
    get_division_from_icon,
    get_endorsement_value_from_frame,
    get_hero_keyname,
    get_player_title,
    get_plural_stat_key,
    get_real_category_name,
    get_role_key_from_icon,
    get_stats_hero_class,
    get_tier_from_icon,
    normalize_career_stat_category_name,
    string_to_snakecase,
)
from app.infrastructure.logger import logger

# Platform/gamemode CSS class mappings
PLATFORMS_DIV_MAPPING = {
    PlayerPlatform.PC: "mouseKeyboard-view",
    PlayerPlatform.CONSOLE: "controller-view",
}
GAMEMODES_DIV_MAPPING = {
    PlayerGamemode.QUICKPLAY: "quickPlay-view",
    PlayerGamemode.COMPETITIVE: "competitive-view",
}


async def fetch_player_html(
    client: BlizzardClientPort, player_id: str
) -> tuple[str, str | None]:
    """
    Fetch player profile HTML from Blizzard and extract Blizzard ID from redirect.

    Blizzard redirects BattleTag URLs to canonical Blizzard ID URLs:
    - Input:  /career/Kindness-11556/
    - Redirect: 302 → /career/df51a381fe20caf8baa7|0bf3b4c47cbebe84b8db9c676a4e9c1f/
    - Returns: (HTML content, Blizzard ID)

    Args:
        client: Blizzard HTTP client
        player_id: Player ID (BattleTag or Blizzard ID format)

    Returns:
        Tuple of (HTML content, Blizzard ID extracted from redirect URL)

    Raises:
        ParserBlizzardError: If player not found (404)
    """

    url = build_blizzard_url(settings.career_path, player_id)

    response = await client.get(url)
    validate_response_status(response, valid_codes=[200, 404])

    # Extract Blizzard ID from final URL (after redirect)
    blizzard_id = extract_blizzard_id_from_url(str(response.url))

    return response.text, blizzard_id


def extract_name_from_profile_html(html: str) -> str | None:
    """
    Extract player display name from profile HTML.

    The name is found in the <h1 class="Profile-player--name"> tag.
    Note: This is ONLY the display name (e.g., "TeKrop"), NOT the full
    BattleTag with discriminator (e.g., "TeKrop-2217").

    Args:
        html: Raw HTML from player profile page

    Returns:
        Player display name if found, None otherwise

    Example:
        >>> html = '<h1 class="Profile-player--name">TeKrop</h1>'
        >>> extract_name_from_profile_html(html)
        'TeKrop'
    """
    root_tag = parse_html_root(html)
    name_tag = root_tag.css_first("h1.Profile-player--name")

    return name_tag.text().strip() if name_tag and name_tag.text() else None


def parse_player_profile_html(
    html: str,
    player_summary: dict | None = None,
) -> dict:
    """
    Parse player profile HTML into summary and stats

    Args:
        html: Raw HTML from player profile page
        player_summary: Optional player summary data from search endpoint
            (provides avatar, namecard, title, lastUpdated)

    Returns:
        Dict with "summary" and "stats" keys

    Raises:
        ParserBlizzardError: If player not found (profile section missing)

    Note:
        ``PlayerService.parse_stored_profile`` memoises this result across the
        five player endpoints, so **consumers must treat the returned structure
        as read-only** — several of them hand nested parts of it straight to the
        response layer.  Build new containers instead of mutating in place.
    """
    root_tag = parse_html_root(html)

    # Check if player exists
    if not root_tag.css_first("blz-section.Profile-masthead"):
        raise ParserBlizzardError(
            status_code=HTTPStatus.NOT_FOUND.value,
            message="Player not found",
        )

    return {
        "summary": _parse_summary(root_tag, player_summary),
        "stats": _parse_stats(root_tag),
    }


def _parse_summary(root_tag: LexborNode, player_summary: dict | None) -> dict:
    """Parse player summary section (username, avatar, endorsement, ranks)"""
    player_summary = player_summary or {}
    error_msg_prefix = "Failed to parse player summary"

    try:
        profile_div = root_tag.css_first(
            "blz-section.Profile-masthead > div.Profile-player"
        )
        summary_div = profile_div.css_first("div.Profile-player--summaryWrapper")
        progression_div = profile_div.css_first("div.Profile-player--info")

        return {
            "username": summary_div.css_first("h1.Profile-player--name").text(),
            "avatar": (
                player_summary.get("avatar")
                or summary_div.css_first("img.Profile-player--portrait").attributes.get(
                    "src"
                )
            ),
            "namecard": player_summary.get("namecard"),
            "title": get_player_title(
                player_summary.get("title") or _get_title(profile_div)
            ),
            "endorsement": _get_endorsement(progression_div),
            "competitive": _get_competitive_ranks(root_tag, progression_div),
            "last_updated_at": (
                player_summary.get("lastUpdated") or _get_last_updated(root_tag)
            ),
        }
    except (AttributeError, KeyError, IndexError, TypeError) as error:
        error_msg = f"{error_msg_prefix}: {error!r}"
        raise ParserParsingError(error_msg) from error


def _get_last_updated(root_tag: LexborNode) -> int | None:
    """Read the profile's own last-update timestamp from the masthead.

    ``lastUpdated`` normally comes from the search endpoint, but that payload is
    ``{}`` for Blizzard-ID lookups and whenever no blizzard_id resolves, leaving
    the field null for those requests. The career page carries the same value as
    ``data-lastUpdate`` on the section this parser already selects, so the
    fallback costs one attribute read and no extra fetch.
    """
    masthead = root_tag.css_first("blz-section.Profile-masthead")
    if not masthead:
        return None

    # lexbor lowercases attribute names, so data-lastUpdate reads back as
    # data-lastupdate.
    raw = masthead.attributes.get("data-lastupdate")
    return int(raw) if raw and raw.isdigit() else None


def _get_title(profile_div: LexborNode) -> str | None:
    """Extract player title from profile div"""
    if not (title_tag := profile_div.css_first("h2.Profile-player--title")):
        return None

    # Special case: "no title" means there is no title
    return title_tag.text() or None


def _get_endorsement(progression_div: LexborNode) -> dict | None:
    """Extract endorsement level and frame"""
    endorsement_span = progression_div.css_first(
        "span.Profile-player--endorsementWrapper"
    )
    if not endorsement_span:
        return None

    endorsement_frame_url = (
        endorsement_span.css_first("img.Profile-playerSummary--endorsement").attributes[
            "src"
        ]
        or ""
    )

    return {
        "level": get_endorsement_value_from_frame(endorsement_frame_url),
        "frame": endorsement_frame_url,
    }


def _get_competitive_ranks(
    root_tag: LexborNode,
    progression_div: LexborNode,
) -> dict | None:
    """Extract competitive ranks for all platforms"""
    competitive_ranks = {
        platform.value: _get_platform_competitive_ranks(
            root_tag,
            progression_div,
            platform_class,
        )
        for platform, platform_class in PLATFORMS_DIV_MAPPING.items()
    }

    # If no data for any platform, return None
    return None if not any(competitive_ranks.values()) else competitive_ranks


def _get_platform_competitive_ranks(
    root_tag: LexborNode,
    progression_div: LexborNode,
    platform_class: str,
) -> dict | None:
    """Extract competitive ranks for a specific platform"""
    last_season_played = _get_last_season_played(root_tag, platform_class)

    role_wrappers = progression_div.css(
        f"div.Profile-playerSummary--rankWrapper.{platform_class} > div.Profile-playerSummary--roleWrapper",
    )
    if not role_wrappers and not last_season_played:
        return None

    competitive_ranks = {}

    for role_wrapper in role_wrappers:
        role_icon = _get_role_icon(role_wrapper)
        role = get_role_key_from_icon(role_icon)
        if role is None:
            # A competitive role we cannot name yet. Dropping this one role
            # still returns the player's other ranks.
            logger.warning(
                "[Player] Unknown competitive role in icon {} — skipping the role",
                role_icon,
            )
            continue
        role_key = role.value

        rank_tier_icons = role_wrapper.css("img.Profile-playerSummary--rank")
        rank_icon, tier_icon = (
            rank_tier_icons[0].attributes["src"] or "",
            rank_tier_icons[1].attributes["src"] or "",
        )

        try:
            division = get_division_from_icon(rank_icon).value
        except ValueError:
            # Blizzard ships a new division before we can add it to the enum
            # (emerald did exactly that). The ValueError is not in the set
            # _parse_summary catches, so it used to surface as a 500 for every
            # player in that tier. Degrade the role to "no rank" instead — the
            # loop below already represents an unranked role as None.
            logger.warning(
                "[Player] Unknown competitive division in rank icon {} — "
                "reporting role {} as unranked",
                rank_icon,
                role_key,
            )
            continue

        tier = get_tier_from_icon(tier_icon)
        if tier == 0:
            # get_tier_from_icon yields 0 for any icon without a "_", but the
            # response model declares tier as ge=1 — so a rename of
            # TierDivision_N.<hash>.png would 500 every ranked player rather
            # than costing one field. Report the role as unranked instead.
            logger.warning(
                "[Player] Could not read a tier from icon {} — "
                "reporting role {} as unranked",
                tier_icon,
                role_key,
            )
            continue

        competitive_ranks[role_key] = {
            "division": division,
            "tier": tier,
            "role_icon": role_icon,
            "rank_icon": rank_icon,
            "tier_icon": tier_icon,
        }

    for role in CompetitiveRole:
        if role.value not in competitive_ranks:
            competitive_ranks[role.value] = None

    competitive_ranks["season"] = last_season_played

    return competitive_ranks


def _get_last_season_played(root_tag: LexborNode, platform_class: str) -> int | None:
    """Extract last competitive season played for a platform"""
    if not (profile_section := _get_profile_view_section(root_tag, platform_class)):
        return None

    statistics_section = profile_section.css_first("blz-section.stats.competitive-view")
    if not statistics_section:
        return None

    last_season_played = statistics_section.attributes.get(
        "data-latestherostatrankseasonow2"
    )
    return int(last_season_played) if last_season_played else None


def _get_profile_view_section(root_tag: LexborNode, platform_class: str) -> LexborNode:
    """Get profile view section for a platform"""
    return root_tag.css_first(f"div.Profile-view.{platform_class}")


def _get_role_icon(role_wrapper: LexborNode) -> str:
    """Extract role icon (format differs between PC and console)"""
    # PC: img tag, Console: svg tag
    if role_div := role_wrapper.css_first("div.Profile-playerSummary--role"):
        return role_div.css_first("img").attributes["src"] or ""

    role_svg = role_wrapper.css_first("svg.Profile-playerSummary--role")
    return role_svg.css_first("use").attributes["xlink:href"] or ""


def _parse_stats(root_tag: LexborNode) -> dict | None:
    """Parse stats for all platforms"""
    stats = {
        platform.value: _parse_platform_stats(root_tag, platform_class)
        for platform, platform_class in PLATFORMS_DIV_MAPPING.items()
    }

    # If no data for any platform, return None
    return None if not any(stats.values()) else stats


def _parse_platform_stats(
    root_tag: LexborNode,
    platform_class: str,
) -> dict | None:
    """Parse stats for a specific platform"""
    statistics_section = _get_profile_view_section(root_tag, platform_class)
    gamemodes_infos = {
        gamemode.value: _parse_gamemode_stats(statistics_section, gamemode)
        for gamemode in PlayerGamemode
    }

    # If no data for any gamemode, return None
    return None if not any(gamemodes_infos.values()) else gamemodes_infos


def _parse_gamemode_stats(
    statistics_section: LexborNode,
    gamemode: PlayerGamemode,
) -> dict | None:
    """Parse stats for a specific gamemode"""
    if not statistics_section or not statistics_section.first_child:
        return None

    top_heroes_section = statistics_section.first_child.css_first(
        f"div.{GAMEMODES_DIV_MAPPING[gamemode]}"
    )

    # Check if we have a select element (indicates data exists)
    if not top_heroes_section or not top_heroes_section.css_first("select"):
        return None

    career_stats_section = statistics_section.css_first(
        f"blz-section.{GAMEMODES_DIV_MAPPING[gamemode]}"
    )
    return {
        "heroes_comparisons": _parse_heroes_comparisons(top_heroes_section),
        "career_stats": _parse_career_stats(career_stats_section),
    }


def _hero_comparison_entry(container: LexborNode) -> dict | None:
    """Build one ``{hero, value}`` row, or None if it cannot be represented.

    Both fields can carry something ``HeroStat`` rejects, and a rejected field
    is a 500 for the whole request rather than one missing row:

    - ``hero`` is typed ``HeroKey``, generated from heroes.csv. When Blizzard
      omits ``data-hero-id`` — which the fallback below exists for, "a hero
      newly available for testing" — the fallback yields a display name like
      "wrecking ball", and a display name is *never* a valid key. So the
      mitigation for a new hero was itself the crash trigger.
    - ``value`` is typed ``StrictInt | StrictFloat``, but
      ``get_computed_stat_value`` returns the raw string for any format it does
      not recognise ("1.2K", a non-breaking space inside a number, "∞").

    Dropping the row loses one hero from one category. Raising loses the whole
    profile, and every other profile that has played that hero.
    """
    name_node = container.first_child
    value_node = container.last_child
    if name_node is None or value_node is None:
        return None
    title_node, amount_node = value_node.first_child, value_node.last_child
    if title_node is None or amount_node is None:
        return None

    hero = name_node.attributes.get("data-hero-id") or title_node.text().lower()
    if hero not in HeroKey:
        logger.warning(
            "[Player] Unknown hero {!r} in heroes comparisons — skipping the row",
            hero,
        )
        return None

    value = get_computed_stat_value(amount_node.text())
    if not isinstance(value, int | float) or isinstance(value, bool):
        logger.warning(
            "[Player] Unreadable value {!r} for hero {} in heroes comparisons — "
            "skipping the row",
            value,
            hero,
        )
        return None

    return {"hero": hero, "value": value}


def _parse_heroes_comparisons(top_heroes_section: LexborNode) -> dict:
    """Parse heroes comparisons (top heroes by category)"""
    categories = _get_heroes_options(top_heroes_section)

    heroes_comparisons: dict[str, dict | None] = {}
    for category in top_heroes_section.iter():
        # .get(): most nodes under this section carry neither attribute, and
        # __getitem__ raises for an absent one.
        css_class = category.attributes.get("class")
        category_id = category.attributes.get("data-category-id")
        if (
            css_class is None
            or "Profile-progressBars" not in css_class
            or category_id not in categories
        ):
            continue

        label = get_real_category_name(categories[category_id])
        # Blizzard's stat ID is stable across the label rewordings we have
        # actually seen; fall back to the label for a category we don't know yet.
        category_key = CAREER_COMPARISON_CATEGORY_IDS.get(
            category_id
        ) or string_to_snakecase(label)

        heroes_comparisons[category_key] = {
            "label": label,
            "values": [
                entry
                for progress_bar in category.iter()
                for progress_bar_container in progress_bar.iter()
                if progress_bar_container.tag == "div"
                and (entry := _hero_comparison_entry(progress_bar_container))
                is not None
            ],
        }

    for category in CareerHeroesComparisonsCategory:
        # Sometimes, Blizzard exposes the categories without any value
        # In that case, we must assume we have no data at all
        entry = heroes_comparisons.get(category.value)
        if not entry or not entry["values"]:
            heroes_comparisons[category.value] = None

    return heroes_comparisons


def _parse_stat_row(stat_row: LexborNode) -> dict | None:
    """Parse a single stat row and return stat dict or None if invalid."""
    if not stat_row.first_child or not stat_row.last_child:
        logger.warning("Missing stat name or value in {}", stat_row)
        return None

    stat_name = stat_row.first_child.text()
    value = get_computed_stat_value(stat_row.last_child.text())
    if not isinstance(value, int | float) or isinstance(value, bool):
        # SingleCareerStat.value is StrictInt | StrictFloat, and
        # get_computed_stat_value hands back the raw string for any format it
        # does not recognise. One unreadable number would 500 the whole profile.
        logger.warning(
            "Unreadable value {!r} for stat {!r}, skipping the row",
            value,
            stat_name,
        )
        return None

    return {
        "key": get_plural_stat_key(string_to_snakecase(stat_name)),
        "label": stat_name,
        "value": value,
    }


def _parse_category_stats(content_div: LexborNode) -> list[dict]:
    """Parse all stat rows for a given category."""
    stats = []
    for stat_row in content_div.iter():
        stat_row_class = stat_row.attributes["class"] or ""
        if "stat-item" not in stat_row_class:
            continue

        stat = _parse_stat_row(stat_row)
        if stat:
            stats.append(stat)

    return stats


def _career_stat_category(
    content_div: LexborNode, hero_key: str
) -> tuple[str, str] | None:
    """Return ``(category_key, english_label)`` for a stat card, or None to skip it.

    Every branch here is a shape Blizzard has actually served at some point;
    the last one is the only one that is about our own types rather than theirs.
    """
    # Label should be the first div within content ("header" class)
    header = content_div.first_child
    if header is None or header.first_child is None:
        logger.warning("Missing category header for hero {}, skipping", hero_key)
        return None

    category_label = header.first_child.text()
    if not category_label or not category_label.strip():
        logger.warning("Empty category label for hero {}, skipping", hero_key)
        return None

    # Normalize localized category names to English
    normalized = normalize_career_stat_category_name(category_label)
    if not normalized or not normalized.strip():
        logger.warning(
            "Category label normalized to empty for hero {} (original: {!r}), skipping",
            hero_key,
            category_label,
        )
        return None

    category_key = string_to_snakecase(normalized)
    if not category_key:
        logger.warning(
            "Category key is empty after snake_case conversion for hero {}"
            " (normalized label: {!r}, original: {!r}), skipping",
            hero_key,
            normalized,
            category_label,
        )
        return None

    if category_key not in CareerStatCategory:
        # HeroCareerStats.category is typed CareerStatCategory, so a category
        # Blizzard adds 500s /players/{id} and /players/{id}/stats. The
        # /stats/career route already drops it silently (its model is built per
        # known key), so the same input produced two different failures. Drop it
        # consistently.
        logger.warning(
            "Unknown career stat category {!r} for hero {}, skipping",
            category_key,
            hero_key,
        )
        return None

    return category_key, normalized


def _parse_career_stats(career_stats_section: LexborNode) -> dict:
    """Parse detailed career stats per hero"""
    heroes_options = _get_heroes_options(career_stats_section, key_prefix="option-")

    career_stats = {}

    for hero_container in career_stats_section.iter():
        # Hero container should be span with "stats-container" class
        if hero_container.tag != "span":
            continue

        stats_hero_class = get_stats_hero_class(hero_container.attributes["class"])

        # Sometimes, Blizzard makes some weird things and options don't
        # have any label, so we can't know for sure which hero it is about.
        # So we have to skip this field
        if stats_hero_class not in heroes_options:
            continue

        hero_key = get_hero_keyname(heroes_options[stats_hero_class])

        career_stats[hero_key] = []
        # Hero container children are div with "category" class
        for card_stat in hero_container.iter():
            # Content div should be the only child ("content" class)
            content_div = card_stat.first_child

            # Ensure we have everything we need
            if (
                not content_div
                or not content_div.first_child
                or not content_div.first_child.first_child
            ):
                logger.warning("Missing content div for hero {}", hero_key)
                continue

            category = _career_stat_category(content_div, hero_key)
            if category is None:
                continue
            category_key, category_label = category

            # Parse all stats for this category
            stats = _parse_category_stats(content_div)

            career_stats[hero_key].append(
                {
                    "category": category_key,
                    "label": category_label,
                    "stats": stats,
                },
            )

        # For a reason, sometimes the hero is in the dropdown but there
        # is no stat to show. In this case, remove it as if there was
        # no stat at all
        if len(career_stats[hero_key]) == 0:
            del career_stats[hero_key]

    return career_stats


def _get_heroes_options(
    parent_section: LexborNode,
    key_prefix: str = "",
) -> dict[str, str]:
    """Extract hero options from dropdown select element"""
    # Sometimes, pages are not rendered correctly and select can be empty
    if not (
        options := parent_section.css_first("div.Profile-heroSummary--header > select")
    ):
        return {}

    return {
        f"{key_prefix}{option.attributes['value']}": str(option.attributes["option-id"])
        for option in options.iter()
        if option.attributes.get("option-id")
    }


# Filtering functions for API queries


def filter_stats_by_query(
    stats: dict | None,
    gamemode: PlayerGamemode | str,
    platform: PlayerPlatform | str | None = None,
    hero: str | None = None,
) -> dict:
    """
    Filter career stats by query parameters

    Args:
        stats: Raw stats dict from parser (keys are strings: platform.value, gamemode.value)
        gamemode: Mandatory gamemode filter (enum or string)
        platform: Optional platform filter (enum or string)
        hero: Optional hero filter

    Returns:
        Filtered dict of career stats
    """
    filtered_data = stats or {}

    # Normalize platform to string key
    if platform:
        platform_key = str(platform.value if hasattr(platform, "value") else platform)
    else:
        # Determine platform if not specified
        possible_platforms = [
            pk
            for pk, platform_data in filtered_data.items()
            if platform_data is not None
        ]
        if possible_platforms:
            # Take the first one of the list, usually there will be only one.
            # If there are two, the PC stats should come first
            platform_key = str(possible_platforms[0])
        else:
            return {}

    filtered_data = filtered_data.get(platform_key) or {}
    if not filtered_data:
        return {}

    # Normalize gamemode to string key
    gamemode_key = str(gamemode.value) if hasattr(gamemode, "value") else gamemode
    filtered_data = filtered_data.get(gamemode_key) or {}
    if not filtered_data:
        return {}

    filtered_data = filtered_data.get("career_stats") or {}

    return {
        hero_key: statistics
        for hero_key, statistics in filtered_data.items()
        if not hero or hero == hero_key
    }


def filter_all_stats_data(
    stats: dict | None,
    platform: PlayerPlatform | str | None = None,
    gamemode: PlayerGamemode | str | None = None,
) -> dict | None:
    """
    Filter all stats data by platform and/or gamemode

    Args:
        stats: Raw stats dict from parser (keys are strings: platform.value, gamemode.value)
        platform: Optional platform filter (enum or string)
        gamemode: Optional gamemode filter (enum or string)

    Returns:
        Filtered stats dict (may set platforms/gamemodes to None if not matching),
        or None if no stats data exists
    """
    # Return None if no stats data
    if stats is None:
        return None

    # Check if stats dict is empty or all values are None
    if not stats or all(v is None for v in stats.values()):
        return None

    stats_data = stats

    # Return early if no filters (ensure both platform keys exist)
    if not platform and not gamemode:
        return {
            PlayerPlatform.PC.value: stats_data.get(PlayerPlatform.PC.value),
            PlayerPlatform.CONSOLE.value: stats_data.get(PlayerPlatform.CONSOLE.value),
        }

    # Normalize filters to string keys
    platform_filter: str | None = None
    if platform:
        platform_filter = (
            str(platform.value) if hasattr(platform, "value") else platform
        )

    gamemode_filter: str | None = None
    if gamemode:
        gamemode_filter = (
            str(gamemode.value) if hasattr(gamemode, "value") else gamemode
        )

    # Ensure both platform keys exist in output
    filtered_data = {}
    for platform_enum in PlayerPlatform:
        platform_key = platform_enum.value
        platform_data = stats_data.get(platform_key)

        if platform_filter and platform_key != platform_filter:
            filtered_data[platform_key] = None
            continue

        if platform_data is None:
            filtered_data[platform_key] = None
            continue

        if gamemode_filter is None:
            filtered_data[platform_key] = platform_data
            continue

        filtered_data[platform_key] = {
            gamemode_key: (gamemode_data if gamemode_key == gamemode_filter else None)
            for gamemode_key, gamemode_data in platform_data.items()
        }

    return filtered_data
