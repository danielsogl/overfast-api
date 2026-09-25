"""API-layer enums for OverFast API"""

from enum import StrEnum


class RouteTag(StrEnum):
    """Tags used to classify API routes"""

    HEROES = "🦸 Heroes"
    GAMEMODES = "🎲 Gamemodes"
    MAPS = "🗺️ Maps"
    PLAYERS = "🎮 Players"
    PATCH_NOTES = "📝 Patch Notes"
    PUSH = "🔔 Push"
