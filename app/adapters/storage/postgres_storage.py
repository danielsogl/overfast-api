"""PostgreSQL storage adapter with zstd compression for player profiles"""

from __future__ import annotations

import asyncio
import json
import time
from compression import zstd
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from app.config import settings
from app.infrastructure.logger import logger
from app.infrastructure.metaclasses import Singleton

if TYPE_CHECKING:
    from datetime import date

    from app.domain.ports.storage import StaticDataCategory

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


class PostgresStorage(metaclass=Singleton):
    """
    PostgreSQL storage adapter for persistent data.

    Provides persistent storage for:
    - Static data (heroes, maps, gamemodes, roles) as JSONB
    - Player profiles with zstd-compressed HTML

    Uses Singleton pattern to ensure a single connection pool across the application.
    """

    def __init__(self) -> None:
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        """Register JSON codec so JSONB columns accept/return Python dicts/lists."""
        await conn.set_type_codec(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    _MAX_POOL_CREATION_ATTEMPTS = 3

    async def initialize(self) -> None:
        """Create the connection pool and ensure schema exists."""
        async with self._init_lock:
            if self._initialized:
                return

            for attempt in range(1, self._MAX_POOL_CREATION_ATTEMPTS + 1):
                try:
                    self._pool: asyncpg.Pool = await asyncpg.create_pool(
                        dsn=settings.postgres_dsn,
                        min_size=settings.postgres_pool_min_size,
                        max_size=settings.postgres_pool_max_size,
                        # Without this a query that never returns holds its
                        # connection forever. The pool caps at 10, so ten such
                        # queries exhaust it and every later request waits on
                        # acquire() — the API stops answering while postgres
                        # itself looks perfectly healthy to the container check.
                        command_timeout=settings.postgres_command_timeout,
                        init=self._init_connection,
                    )
                    break
                except Exception as exc:
                    if attempt == self._MAX_POOL_CREATION_ATTEMPTS:
                        logger.error("Failed to create PostgreSQL pool: {}", exc)
                        raise
                    logger.warning(
                        "PostgreSQL pool creation attempt {}/{} failed: {}. Retrying in 2s…",
                        attempt,
                        self._MAX_POOL_CREATION_ATTEMPTS,
                        exc,
                    )
                    await asyncio.sleep(2)

            await self._create_schema()
            self._initialized = True
            logger.info("PostgreSQL storage initialized")

    async def _create_schema(self) -> None:
        """Create enum type and tables if they don't exist."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(_SCHEMA_SQL)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
        self._initialized = False

    # ------------------------------------------------------------------ #
    # Compression helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compress(data: str) -> bytes:
        return zstd.compress(data.encode("utf-8"))

    @staticmethod
    def _decompress(data: bytes) -> str:
        return zstd.decompress(data).decode("utf-8")

    # ------------------------------------------------------------------ #
    # Static data
    # ------------------------------------------------------------------ #

    async def get_static_data(self, key: str) -> dict | None:
        """Get static data by key. Returns dict with 'data' (decompressed str),
        'category', 'updated_at' (Unix int), 'data_version' or None."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                """SELECT data, category, updated_at, data_version
                   FROM static_data WHERE key = $1""",
                key,
            )
        if row is None:
            return None

        decompressed_data = self._decompress(row["data"])
        return {
            "data": decompressed_data,
            "category": row["category"],
            "updated_at": int(row["updated_at"].timestamp()),
            "data_version": row["data_version"],
        }

    async def set_static_data(
        self,
        key: str,
        data: str,
        category: StaticDataCategory,
        data_version: int = 1,
    ) -> None:
        """Upsert static data. ``data`` is a raw string (HTML or JSON) compressed with zstd."""
        compressed = self._compress(data)
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO static_data (key, data, category, data_version, updated_at)
                   VALUES ($1, $2, $3::static_data_category, $4, NOW())
                   ON CONFLICT (key) DO UPDATE
                   SET data = EXCLUDED.data,
                       category = EXCLUDED.category,
                       data_version = EXCLUDED.data_version,
                       updated_at = NOW()""",
                key,
                compressed,
                category.value,
                data_version,
            )

    # ------------------------------------------------------------------ #
    # Player profiles
    # ------------------------------------------------------------------ #

    async def get_player_profile(self, player_id: str) -> dict | None:
        """Get player profile by player_id.

        Returns dict with 'html', 'summary' (dict), 'battletag', 'name',
        'last_updated_blizzard', 'updated_at' (Unix int), 'data_version'
        or None if not found.
        """
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                """SELECT battletag, name, html_compressed, summary,
                          last_updated_blizzard, updated_at, data_version
                   FROM player_profiles WHERE player_id = $1""",
                player_id,
            )
        if row is None:
            return None

        summary = row["summary"] if row["summary"] is not None else {}
        if not summary:
            summary = {"url": player_id, "lastUpdated": row["last_updated_blizzard"]}

        return {
            "html": self._decompress(row["html_compressed"]),
            "battletag": row["battletag"],
            "name": row["name"],
            "summary": summary,
            "last_updated_blizzard": row["last_updated_blizzard"],
            "updated_at": int(row["updated_at"].timestamp()),
            "data_version": row["data_version"],
        }

    async def get_player_id_by_battletag(self, battletag: str) -> str | None:
        """Get Blizzard ID (player_id) for a given BattleTag."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(
                "SELECT player_id FROM player_profiles WHERE battletag = $1",
                battletag,
            )
        return row["player_id"] if row else None

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
        """Upsert player profile. HTML is zstd-compressed before storage."""
        if summary and last_updated_blizzard is None:
            last_updated_blizzard = summary.get("lastUpdated")

        compressed = self._compress(html)

        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO player_profiles
                       (player_id, battletag, name, html_compressed, summary,
                        last_updated_blizzard, data_version, updated_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, NOW())
                   ON CONFLICT (player_id) DO UPDATE
                   SET battletag = COALESCE(EXCLUDED.battletag, player_profiles.battletag),
                       name = COALESCE(EXCLUDED.name, player_profiles.name),
                       html_compressed = EXCLUDED.html_compressed,
                       summary = EXCLUDED.summary,
                       last_updated_blizzard = EXCLUDED.last_updated_blizzard,
                       data_version = EXCLUDED.data_version,
                       updated_at = NOW()""",
                player_id,
                battletag,
                name,
                compressed,
                summary,
                last_updated_blizzard,
                data_version,
            )

    # ------------------------------------------------------------------ #
    # Player snapshots
    # ------------------------------------------------------------------ #

    async def add_player_snapshot(
        self,
        player_id: str,
        last_updated_blizzard: int,
        data: dict,
    ) -> None:
        """Append one snapshot, ignoring a version already recorded."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO player_snapshots
                       (player_id, last_updated_blizzard, data)
                   VALUES ($1, $2, $3::jsonb)
                   ON CONFLICT (player_id, last_updated_blizzard) DO NOTHING""",
                player_id,
                last_updated_blizzard,
                data,
            )

    async def get_player_snapshots(
        self,
        player_id: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Return a player's snapshots, newest first."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(
                """SELECT taken_at, last_updated_blizzard, data
                   FROM player_snapshots
                   WHERE player_id = $1
                     AND ($2::double precision IS NULL
                          OR taken_at >= TO_TIMESTAMP($2))
                   ORDER BY taken_at DESC, last_updated_blizzard DESC
                   LIMIT $3""",
                player_id,
                None if since is None else float(since),
                limit,
            )

        return [
            {
                "taken_at": int(row["taken_at"].timestamp()),
                "last_updated_blizzard": row["last_updated_blizzard"],
                "data": row["data"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Hero stats snapshots
    # ------------------------------------------------------------------ #

    async def add_hero_stats_snapshot(
        self,
        taken_on: date,
        platform: str,
        gamemode: str,
        region: str,
        data: list[dict],
    ) -> None:
        """Record one day's reading, ignoring a day already recorded."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                """INSERT INTO hero_stats_snapshots
                       (taken_on, platform, gamemode, region, data)
                   VALUES ($1, $2, $3, $4, $5::jsonb)
                   ON CONFLICT (taken_on, platform, gamemode, region)
                   DO NOTHING""",
                taken_on,
                platform,
                gamemode,
                region,
                data,
            )

    async def get_hero_stats_snapshots(
        self,
        platform: str,
        gamemode: str,
        region: str,
        since: int | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """Return recorded hero stats readings, newest first."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(
                """SELECT taken_on, data
                   FROM hero_stats_snapshots
                   WHERE platform = $1 AND gamemode = $2 AND region = $3
                     AND ($4::double precision IS NULL
                          OR taken_on >= TO_TIMESTAMP($4)::date)
                   ORDER BY taken_on DESC
                   LIMIT $5""",
                platform,
                gamemode,
                region,
                None if since is None else float(since),
                limit,
            )

        return [{"taken_on": row["taken_on"], "data": row["data"]} for row in rows]

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    async def delete_old_player_profiles(self, max_age_seconds: int) -> int:
        """Delete player profiles not updated within max_age_seconds.

        Returns:
            Number of deleted rows.
        """
        cutoff = time.time() - max_age_seconds
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(
                "DELETE FROM player_profiles WHERE updated_at < TO_TIMESTAMP($1)",
                cutoff,
            )
        deleted = int(result.split()[-1])
        logger.info(
            "Deleted {} old player profiles (max_age={}s)", deleted, max_age_seconds
        )
        return deleted

    async def delete_old_player_snapshots(self, max_age_seconds: int) -> int:
        """Delete snapshots taken longer than max_age_seconds ago.

        Returns:
            Number of deleted rows.
        """
        cutoff = time.time() - max_age_seconds
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(
                "DELETE FROM player_snapshots WHERE taken_at < TO_TIMESTAMP($1)",
                cutoff,
            )
        deleted = int(result.split()[-1])
        logger.info(
            "Deleted {} old player snapshots (max_age={}s)", deleted, max_age_seconds
        )
        return deleted

    async def delete_old_hero_stats_snapshots(self, max_age_seconds: int) -> int:
        """Delete hero stats readings taken longer than max_age_seconds ago.

        Returns:
            Number of deleted rows.
        """
        cutoff = time.time() - max_age_seconds
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(
                "DELETE FROM hero_stats_snapshots WHERE taken_on < TO_TIMESTAMP($1)::date",
                cutoff,
            )
        deleted = int(result.split()[-1])
        logger.info(
            "Deleted {} old hero stats snapshots (max_age={}s)",
            deleted,
            max_age_seconds,
        )
        return deleted

    async def clear_all_data(self) -> None:
        """Truncate all tables (for testing)."""
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await conn.execute(
                "TRUNCATE static_data, player_profiles, player_snapshots, "
                "hero_stats_snapshots"
            )
