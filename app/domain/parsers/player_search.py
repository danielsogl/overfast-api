"""Stateless parser for player search endpoint"""

from typing import TYPE_CHECKING

from app.config import settings
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.player_helpers import get_player_title
from app.domain.parsers.utils import build_blizzard_url, validate_response_status
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.ports import BlizzardClientPort


async def fetch_player_search_json(client: BlizzardClientPort, name: str) -> list[dict]:
    """
    Fetch player search results from Blizzard

    Args:
        client: Blizzard HTTP client
        name: Player name to search (may include discriminator)

    Returns:
        List of player dicts from Blizzard search
    """
    search_name = name.split("-", 1)[0]
    url = build_blizzard_url(settings.search_account_path, search_name)

    response = await client.get(url)
    validate_response_status(response)

    return response.json()


def filter_players_by_name(json_data: list[dict], name: str) -> list[dict]:
    """
    Filter players by case-insensitive name match (public only)

    Names are compared with casefold() only, without Unicode normalization,
    so CJK and fullwidth names must match as typed. Blizzard's search payload
    carries no discriminator, so a discriminator in the input can't be checked
    here and is ignored.

    Args:
        json_data: List of players from Blizzard
        name: Player name to filter (discriminator is ignored)

    Returns:
        Filtered list of matching players

    Raises:
        ParserParsingError: If unexpected payload structure
    """
    try:
        search_name = name.split("-", 1)[0].casefold()
        return [
            player
            for player in json_data
            if player["name"].casefold() == search_name and player["isPublic"] is True
        ]
    except (KeyError, TypeError, AttributeError) as error:
        msg = f"Unexpected Blizzard search payload structure: {error}"
        raise ParserParsingError(msg) from error


def transform_player_search_results(players: list[dict]) -> list[dict]:
    """
    Transform Blizzard player data to OverFast API format

    player_id is always Blizzard's own ID (the "url" field), never the
    unverified search input.

    Args:
        players: Filtered list of players

    Returns:
        List of transformed player dicts

    Raises:
        ParserParsingError: If missing required fields in payload
    """
    try:
        transformed_players = []

        for player in players:
            player_id = player["url"]

            # Normalize optional fields for regional consistency
            if player.get("portrait"):
                player["avatar"] = None
                player["namecard"] = None
                player["title"] = None

            transformed_players.append(
                {
                    "player_id": player_id,
                    "name": player["name"],
                    "avatar": player["avatar"],
                    "namecard": player.get("namecard"),
                    "title": get_player_title(player.get("title")),
                    "career_url": f"{settings.app_base_url}/players/{player_id}",
                    "blizzard_id": player["url"],
                    "last_updated_at": player["lastUpdated"],
                    "is_public": player["isPublic"],
                },
            )

    except (KeyError, TypeError, IndexError) as error:
        msg = f"Missing required field in Blizzard player data: {error}"
        raise ParserParsingError(msg) from error
    else:
        return transformed_players


def apply_ordering(players: list[dict], order_by: str) -> list[dict]:
    """
    Sort players by specified field and direction

    Args:
        players: List of player dicts
        order_by: Ordering string (e.g., "name:asc", "last_updated_at:desc")

    Returns:
        Sorted list of players

    Raises:
        ParserParsingError: If invalid order_by format or field
    """
    try:
        order_field, order_arrangement = order_by.split(":")
        players.sort(
            key=lambda player: player[order_field],
            reverse=(order_arrangement == "desc"),
        )

    except (ValueError, KeyError) as error:
        msg = f"Invalid ordering parameter '{order_by}': {error}"
        raise ParserParsingError(msg) from error
    else:
        return players


def prioritize_exact_matches(players: list[dict], name: str) -> list[dict]:
    """
    Move players whose name matches the search with the same casing to the
    front, keeping the existing order within both groups (stable sort)

    Args:
        players: List of player dicts
        name: Player name searched (discriminator is ignored)

    Returns:
        Sorted list of players
    """
    search_name = name.split("-", 1)[0]
    players.sort(key=lambda player: player["name"] != search_name)
    return players


def apply_pagination(players: list[dict], offset: int, limit: int) -> dict:
    """
    Apply pagination to player results

    Args:
        players: List of all matching players
        offset: Starting index
        limit: Maximum results to return

    Returns:
        Dict with total count and paginated results
    """
    return {
        "total": len(players),
        "results": players[offset : offset + limit],
    }


async def parse_player_search(
    client: BlizzardClientPort,
    name: str,
    order_by: str = "name:asc",
    offset: int = 0,
    limit: int = 10,
) -> dict:
    """
    High-level function to search for players

    Args:
        client: Blizzard HTTP client
        name: Player name to search
        order_by: Ordering (default: "name:asc")
        offset: Pagination offset (default: 0)
        limit: Pagination limit (default: 10)

    Returns:
        Dict with total count and paginated player results

    Raises:
        ParserParsingError: If unexpected payload structure
    """
    logger.info("Fetching player search data from Blizzard...")
    json_data = await fetch_player_search_json(client, name)

    logger.info("Filtering players by name...")
    players = filter_players_by_name(json_data, name)

    logger.info("Applying transformation...")
    players = transform_player_search_results(players)

    logger.info("Applying ordering...")
    players = apply_ordering(players, order_by)
    players = prioritize_exact_matches(players, name)

    logger.info("Done! Returning players list...")
    return apply_pagination(players, offset, limit)
