"""Stateless parser for player summary data from Blizzard search endpoint"""

from typing import TYPE_CHECKING, cast

from app.config import settings
from app.domain.exceptions import ParserParsingError
from app.domain.parsers.utils import (
    build_blizzard_url,
    match_player_by_blizzard_id,
    validate_response_status,
)
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.models.player import BlizzardSearchPlayer
    from app.domain.ports import BlizzardClientPort


async def fetch_player_summary_json(
    client: BlizzardClientPort, player_id: str
) -> list[dict]:
    """
    Fetch player summary data from Blizzard search endpoint

    Args:
        client: Blizzard HTTP client
        player_id: Player ID (name-discriminator format)

    Returns:
        Raw JSON response from Blizzard (list of player dicts)
    """
    player_name = player_id.split("-", 1)[0]
    url = build_blizzard_url(settings.search_account_path, player_name)

    response = await client.get(url)
    validate_response_status(response)

    return response.json()


def parse_player_summary_json(
    json_data: list[dict], player_id: str, blizzard_id: str | None = None
) -> BlizzardSearchPlayer:
    """
    Parse player summary from search endpoint JSON

    Args:
        json_data: List of player data from Blizzard search
        player_id: Player ID to find (BattleTag format)
        blizzard_id: Optional Blizzard ID from profile redirect to resolve ambiguity

    Returns:
        Player summary dict, or empty dict if not found

    Raises:
        ParserParsingError: If unexpected payload structure
    """

    if not json_data:
        return {}

    try:
        player_name = player_id.split("-", 1)[0]

        # Find matching players (exact name match, case-sensitive, public only)
        matching_players = [
            player
            for player in json_data
            if player["name"] == player_name and player["isPublic"] is True
        ]

        if blizzard_id:
            # Blizzard ID provided: always use it to verify the match, regardless
            # of how many name-matching players were found. This prevents accepting
            # a wrong player even when there is only one name match.
            if len(matching_players) > 1:
                logger.info(
                    "Multiple players found for {}, using Blizzard ID to resolve: {}",
                    player_id,
                    blizzard_id,
                )
            player_data = match_player_by_blizzard_id(matching_players, blizzard_id)
            if not player_data:
                logger.warning(
                    "Blizzard ID {} not found in search results for {}",
                    blizzard_id,
                    player_id,
                )
                return {}
        else:
            # Without a Blizzard ID we cannot safely identify the player:
            # Blizzard always returns a Blizzard ID in the URL field, so the
            # discriminator cannot be verified from search results alone.
            logger.warning(
                "Player {} not found in search results ({} matching players)",
                player_id,
                len(matching_players),
            )
            return {}

        # Normalize optional fields for regional consistency
        # Some regions still use "portrait" instead of "avatar", "namecard", "title"
        if player_data.get("portrait"):
            player_data["avatar"] = None
            player_data["namecard"] = None
            player_data["title"] = None

    except (KeyError, TypeError) as error:
        msg = f"Unexpected Blizzard search payload structure: {error}"
        raise ParserParsingError(msg) from error
    else:
        # player_data comes from raw Blizzard JSON (list[dict]) — trusted at
        # this boundary the same way the rest of this module already treats
        # Blizzard's payload as authoritative.
        return cast("BlizzardSearchPlayer", player_data)
