"""Storage port protocol for persistent data storage"""

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import date


class StaticDataCategory(StrEnum):
    """Category of static data stored in persistent storage."""

    HEROES = "heroes"
    HERO = "hero"
    GAMEMODES = "gamemodes"
    MAPS = "maps"
    ROLES = "roles"
    PATCH_NOTES = "patch_notes"


class StoragePort(Protocol):
    """Protocol for persistent storage operations.

    Defines the contract for storage adapters to implement persistent
    caching of static data and player profiles.
    """

    async def initialize(self) -> None:
        """Initialize storage (create tables, setup schema)"""
        ...

    async def get_static_data(self, key: str) -> dict | None:
        """Get static data (heroes, maps, gamemodes, roles) by key.

        Returns dict with 'data' (str — raw HTML or JSON), 'category' (str),
        'updated_at' (int Unix ts), 'data_version' (int) or None if not found.
        """
        ...

    async def set_static_data(
        self,
        key: str,
        data: str,
        category: StaticDataCategory,
        data_version: int = 1,
    ) -> None:
        """Store static data. ``data`` is a raw string (HTML or JSON)."""
        ...

    async def get_player_profile(self, player_id: str) -> dict | None:
        """
        Get player profile HTML and parsed summary.

        Returns dict with 'html', 'summary' (dict), 'battletag', 'name',
        'last_updated_blizzard', 'updated_at' (int Unix ts), 'data_version'
        or None if not found.
        """
        ...

    async def get_player_id_by_battletag(self, battletag: str) -> str | None:
        """
        Get the canonical player ID for a given battletag.

        Enables lookup optimisation: when a battletag has been seen before,
        the stored player ID can be used directly without an extra resolution step.

        Returns:
            Player ID if found, None otherwise
        """
        ...

    async def set_player_profile(
        self,
        player_id: str,
        html: str,
        summary: dict | None = None,
        battletag: str | None = None,
        name: str | None = None,
        last_updated_blizzard: int | None = None,
        data_version: int = 1,
    ) -> None:
        """Store player profile HTML and parsed summary with optional metadata"""
        ...

    async def add_player_snapshot(
        self,
        player_id: str,
        last_updated_blizzard: int,
        data: dict,
    ) -> None:
        """Append one snapshot of a player's profile version.

        Idempotent: ``(player_id, last_updated_blizzard)`` is the primary key, so
        re-serving a profile version we already recorded stores nothing.
        """
        ...

    async def get_player_snapshots(
        self,
        player_id: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return a player's snapshots, newest first.

        ``since`` is an optional Unix timestamp filtering on ``taken_at``.

        Each item is ``{'taken_at' (int Unix ts), 'last_updated_blizzard' (int),
        'data' (dict)}``.
        """
        ...

    async def add_hero_stats_snapshot(
        self,
        taken_on: date,
        platform: str,
        gamemode: str,
        region: str,
        data: list[dict],
    ) -> None:
        """Record one day's hero stats reading for one filter combination.

        Idempotent: ``(taken_on, platform, gamemode, region)`` is the primary
        key, so running the daily job twice stores nothing the second time.
        """
        ...

    async def get_hero_stats_snapshots(
        self,
        platform: str,
        gamemode: str,
        region: str,
        since: int | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """Return recorded hero stats readings, newest first.

        ``since`` is an optional Unix timestamp; only readings taken on or after
        that day are returned.

        Each item is ``{'taken_on' (datetime.date), 'data' (list[dict])}``.
        """
        ...

    async def delete_old_player_profiles(self, max_age_seconds: int) -> int:
        """
        Delete player profiles not updated within max_age_seconds.

        Returns:
            Number of deleted rows
        """
        ...

    async def delete_old_player_snapshots(self, max_age_seconds: int) -> int:
        """
        Delete snapshots taken longer than max_age_seconds ago.

        Returns:
            Number of deleted rows
        """
        ...

    async def delete_old_hero_stats_snapshots(self, max_age_seconds: int) -> int:
        """
        Delete hero stats readings taken longer than max_age_seconds ago.

        Returns:
            Number of deleted rows
        """
        ...

    async def clear_all_data(self) -> None:
        """Clear all data including static data (for testing)"""
        ...

    async def close(self) -> None:
        """Close storage connections"""
        ...
