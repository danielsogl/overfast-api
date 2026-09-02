"""Unit tests for PostgresStorage adapter"""

from __future__ import annotations

import datetime
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.storage.postgres_storage import PostgresStorage
from app.domain.ports.storage import StaticDataCategory


def _make_connection(fetchrow_result=None, fetch_result=None):
    """Create a mock asyncpg connection."""
    conn = AsyncMock()
    conn.set_type_codec = AsyncMock()
    conn.execute = AsyncMock(return_value="DELETE 3")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    return conn


def _make_pool(conn=None):
    """Create a mock asyncpg pool with acquire() context manager."""
    if conn is None:
        conn = _make_connection()
    pool = AsyncMock()
    pool.close = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool, conn


def _make_storage(pool=None) -> PostgresStorage:
    """Create a PostgresStorage with an injected mock pool."""
    storage = PostgresStorage()
    if pool is not None:
        storage._pool = pool
        storage._initialized = True
    return storage


# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------


class TestCompressionHelpers:
    def test_compress_decompress_roundtrip(self):
        text = "Hello, World! " * 100
        compressed = PostgresStorage._compress(text)

        assert isinstance(compressed, bytes)
        assert PostgresStorage._decompress(compressed) == text

    def test_compress_produces_smaller_output(self):
        text = "a" * 10000
        compressed = PostgresStorage._compress(text)

        assert len(compressed) < len(text)


# ---------------------------------------------------------------------------
# lifecycle — initialize and close
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initializes_pool_and_schema(self):
        pool, conn = _make_pool()
        with (
            patch(
                "app.adapters.storage.postgres_storage.asyncpg.create_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch("app.adapters.storage.postgres_storage.settings") as s,
        ):
            s.postgres_dsn = "postgresql://localhost/test"
            s.postgres_pool_min_size = 1
            s.postgres_pool_max_size = 5
            s.prometheus_enabled = False
            storage = PostgresStorage()
            await storage.initialize()

        assert storage._initialized is True
        conn.execute.assert_awaited()  # schema creation

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self):
        """Calling initialize twice is a no-op (lock + _initialized guard)."""
        pool, _conn = _make_pool()
        with patch(
            "app.adapters.storage.postgres_storage.asyncpg.create_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ) as mock_create:
            with patch("app.adapters.storage.postgres_storage.settings") as s:
                s.postgres_dsn = "postgresql://localhost/test"
                s.postgres_pool_min_size = 1
                s.postgres_pool_max_size = 5
                s.prometheus_enabled = False
                storage = PostgresStorage()
                await storage.initialize()
                await storage.initialize()
            mock_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_initialize_pool_creation_failure_raises(self):
        """Pool creation failure after max retries raises the exception."""
        with (
            patch(
                "app.adapters.storage.postgres_storage.asyncpg.create_pool",
                new_callable=AsyncMock,
                side_effect=OSError("connection refused"),
            ),
            patch(
                "app.adapters.storage.postgres_storage.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("app.adapters.storage.postgres_storage.settings") as s,
        ):
            s.postgres_dsn = "postgresql://localhost/test"
            s.postgres_pool_min_size = 1
            s.postgres_pool_max_size = 5
            s.prometheus_enabled = False
            storage = PostgresStorage()
            with pytest.raises(OSError, match="connection refused"):
                await storage.initialize()

    @pytest.mark.asyncio
    async def test_close_clears_pool(self):
        pool, _ = _make_pool()
        storage = _make_storage(pool=pool)
        await storage.close()

        pool.close.assert_awaited_once()
        assert storage._initialized is False


# ---------------------------------------------------------------------------
# get_static_data
# ---------------------------------------------------------------------------


class TestGetStaticData:
    @pytest.mark.asyncio
    async def test_returns_none_when_row_missing(self):
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        storage = _make_storage(pool=pool)
        result = await storage.get_static_data("heroes")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_decompressed_data(self):
        payload = json.dumps({"heroes": []})
        compressed = PostgresStorage._compress(payload)
        updated_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
        row = {
            "data": compressed,
            "parsed": None,
            "category": "heroes",
            "updated_at": updated_at,
            "data_version": 1,
        }
        conn = _make_connection()
        conn.fetchrow = AsyncMock(return_value=row)
        pool, _ = _make_pool(conn=conn)
        storage = _make_storage(pool=pool)
        result = await storage.get_static_data("heroes")

        assert result is not None
        assert result["data"] == payload
        assert result["category"] == "heroes"
        assert result["data_version"] == 1
        assert result["parsed"] is None
        assert isinstance(result["updated_at"], int)

    @pytest.mark.asyncio
    async def test_returns_the_parsed_payload_when_present(self):
        parsed = [{"key": "ana"}]
        row = {
            "data": PostgresStorage._compress("<html/>"),
            "parsed": parsed,
            "category": "heroes",
            "updated_at": datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC),
            "data_version": 3,
        }
        conn = _make_connection()
        conn.fetchrow = AsyncMock(return_value=row)
        pool, _ = _make_pool(conn=conn)
        storage = _make_storage(pool=pool)
        result = await storage.get_static_data("heroes")

        assert result is not None
        assert result["parsed"] == parsed
        assert result["data_version"] == 3  # noqa: PLR2004


# ---------------------------------------------------------------------------
# set_static_data
# ---------------------------------------------------------------------------


class TestSetStaticData:
    @pytest.mark.asyncio
    async def test_compresses_and_upserts(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        await storage.set_static_data(
            key="heroes",
            data='{"heroes": []}',
            category=StaticDataCategory.HEROES,
            data_version=2,
        )
        conn.execute.assert_awaited_once()
        args = conn.execute.call_args[0]

        # Second arg should be compressed bytes
        assert isinstance(args[2], bytes)


# ---------------------------------------------------------------------------
# get_player_profile
# ---------------------------------------------------------------------------


class TestGetPlayerProfile:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        storage = _make_storage(pool=pool)
        result = await storage.get_player_profile("nobody-0000")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_profile_with_summary(self):
        html = "<html>player</html>"
        compressed = PostgresStorage._compress(html)
        summary = {"url": "abc123", "lastUpdated": 1700000000}
        updated_at = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
        row = {
            "html_compressed": compressed,
            "parsed": None,
            "battletag": "TeKrop-2217",
            "name": "TeKrop",
            "summary": summary,
            "last_updated_blizzard": 1700000000,
            "updated_at": updated_at,
            "data_version": 1,
        }
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=row)
        storage = _make_storage(pool=pool)
        result = await storage.get_player_profile("abc123")

        assert result is not None
        assert result["html"] == html
        assert result["summary"] == summary
        assert result["battletag"] == "TeKrop-2217"

    @pytest.mark.asyncio
    async def test_builds_summary_when_none(self):
        """When row summary is None, builds a minimal summary dict."""
        html = "<html>player</html>"
        compressed = PostgresStorage._compress(html)
        updated_at = datetime.datetime(2025, 6, 1, tzinfo=datetime.UTC)
        row = {
            "html_compressed": compressed,
            "parsed": None,
            "battletag": None,
            "name": None,
            "summary": None,
            "last_updated_blizzard": 12345,
            "updated_at": updated_at,
            "data_version": 1,
        }
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=row)
        storage = _make_storage(pool=pool)
        result = await storage.get_player_profile("abc123")

        assert result is not None
        assert result["summary"]["url"] == "abc123"
        assert result["summary"]["lastUpdated"] == 12345  # noqa: PLR2004


# ---------------------------------------------------------------------------
# get_player_id_by_battletag
# ---------------------------------------------------------------------------


class TestGetPlayerIdByBattletag:
    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value=None)
        storage = _make_storage(pool=pool)
        result = await storage.get_player_id_by_battletag("Unknown-9999")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_player_id(self):
        pool, conn = _make_pool()
        conn.fetchrow = AsyncMock(return_value={"player_id": "abc123|def456"})
        storage = _make_storage(pool=pool)
        result = await storage.get_player_id_by_battletag("TeKrop-2217")

        assert result == "abc123|def456"


# ---------------------------------------------------------------------------
# set_player_profile
# ---------------------------------------------------------------------------


class TestSetPlayerProfile:
    @pytest.mark.asyncio
    async def test_compresses_html_and_upserts(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        await storage.set_player_profile(
            player_id="abc123",
            html="<html>player</html>",
            summary={"lastUpdated": 123},
            battletag="TeKrop-2217",
            name="TeKrop",
        )
        conn.execute.assert_awaited_once()
        args = conn.execute.call_args[0]

        # html_compressed is 4th positional arg
        assert isinstance(args[4], bytes)

    @pytest.mark.asyncio
    async def test_extracts_last_updated_from_summary(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        await storage.set_player_profile(
            player_id="abc123",
            html="<html/>",
            summary={"lastUpdated": 9999},
        )
        args = conn.execute.call_args[0]

        # Bind order: player_id, battletag, name, html, parsed, summary,
        # last_updated_blizzard, data_version — args[0] being the SQL itself.
        assert args[7] == 9999  # noqa: PLR2004


# ---------------------------------------------------------------------------
# delete_old_player_profiles
# ---------------------------------------------------------------------------


class TestDeleteOldPlayerProfiles:
    @pytest.mark.asyncio
    async def test_returns_deleted_count(self):
        pool, conn = _make_pool()
        conn.execute = AsyncMock(return_value="DELETE 5")
        storage = _make_storage(pool=pool)
        result = await storage.delete_old_player_profiles(86400)

        assert result == 5  # noqa: PLR2004


# ---------------------------------------------------------------------------
# player snapshots
# ---------------------------------------------------------------------------


class TestPlayerSnapshots:
    @pytest.mark.asyncio
    async def test_insert_ignores_a_known_version(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.add_player_snapshot("abc123", 1700000000, {"v": 1})

        sql, *args = conn.execute.call_args[0]
        assert "ON CONFLICT (player_id, last_updated_blizzard) DO NOTHING" in sql
        assert args == ["abc123", 1700000000, {"v": 1}]

    @pytest.mark.asyncio
    async def test_get_maps_rows_and_converts_taken_at(self):
        taken_at = datetime.datetime(2026, 8, 29, 12, 0, tzinfo=datetime.UTC)
        pool, conn = _make_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "taken_at": taken_at,
                    "last_updated_blizzard": 1700000000,
                    "data": {"v": 1},
                }
            ]
        )
        storage = _make_storage(pool=pool)

        result = await storage.get_player_snapshots("abc123")

        assert result == [
            {
                "taken_at": int(taken_at.timestamp()),
                "last_updated_blizzard": 1700000000,
                "data": {"v": 1},
            }
        ]

    @pytest.mark.asyncio
    async def test_get_passes_since_as_a_nullable_timestamp(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.get_player_snapshots("abc123", since=1700000000, limit=5)

        sql, *args = conn.fetch.call_args[0]
        assert "ORDER BY taken_at DESC" in sql
        assert args == ["abc123", 1700000000.0, 5]

    @pytest.mark.asyncio
    async def test_get_without_since_passes_null(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.get_player_snapshots("abc123")

        args = conn.fetch.call_args[0]
        assert args[2] is None

    @pytest.mark.asyncio
    async def test_delete_old_returns_deleted_count(self):
        pool, conn = _make_pool()
        conn.execute = AsyncMock(return_value="DELETE 7")
        storage = _make_storage(pool=pool)

        result = await storage.delete_old_player_snapshots(31536000)

        assert result == 7  # noqa: PLR2004


# ---------------------------------------------------------------------------
# hero stats snapshots
# ---------------------------------------------------------------------------


class TestHeroStatsSnapshots:
    _SLICE = ("pc", "competitive", "europe")

    @pytest.mark.asyncio
    async def test_insert_ignores_a_day_already_recorded(self):
        taken_on = datetime.date(2026, 8, 29)
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.add_hero_stats_snapshot(taken_on, *self._SLICE, [{"v": 1}])

        sql, *args = conn.execute.call_args[0]
        assert "ON CONFLICT (taken_on, platform, gamemode, region)" in sql
        assert "DO NOTHING" in sql
        assert args == [taken_on, "pc", "competitive", "europe", [{"v": 1}]]

    @pytest.mark.asyncio
    async def test_get_maps_rows_newest_first(self):
        taken_on = datetime.date(2026, 8, 29)
        pool, conn = _make_pool()
        conn.fetch = AsyncMock(
            return_value=[{"taken_on": taken_on, "data": [{"v": 1}]}]
        )
        storage = _make_storage(pool=pool)

        result = await storage.get_hero_stats_snapshots(*self._SLICE)

        sql = conn.fetch.call_args[0][0]
        assert "ORDER BY taken_on DESC" in sql
        assert result == [{"taken_on": taken_on, "data": [{"v": 1}]}]

    @pytest.mark.asyncio
    async def test_get_passes_since_as_a_nullable_timestamp(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.get_hero_stats_snapshots(*self._SLICE, since=1700000000, limit=5)

        args = conn.fetch.call_args[0][1:]
        assert args == ("pc", "competitive", "europe", 1700000000.0, 5)

    @pytest.mark.asyncio
    async def test_get_without_since_passes_null(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.get_hero_stats_snapshots(*self._SLICE)

        assert conn.fetch.call_args[0][4] is None

    @pytest.mark.asyncio
    async def test_delete_old_returns_deleted_count(self):
        pool, conn = _make_pool()
        conn.execute = AsyncMock(return_value="DELETE 4")
        storage = _make_storage(pool=pool)

        result = await storage.delete_old_hero_stats_snapshots(63072000)

        assert result == 4  # noqa: PLR2004


# ---------------------------------------------------------------------------
# clear_all_data
# ---------------------------------------------------------------------------


class TestClearAllData:
    @pytest.mark.asyncio
    async def test_executes_truncate(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        await storage.clear_all_data()

        conn.execute.assert_awaited_once()
        sql = conn.execute.call_args[0][0]
        assert "TRUNCATE" in sql


# ---------------------------------------------------------------------------
# _init_connection
# ---------------------------------------------------------------------------


class TestInitConnection:
    @pytest.mark.asyncio
    async def test_registers_jsonb_codec(self):
        conn = AsyncMock()
        await PostgresStorage._init_connection(conn)

        conn.set_type_codec.assert_awaited_once_with(
            "jsonb",
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


# ---------------------------------------------------------------------------
# parsed write-back — set_static_data_parsed / set_player_profile_parsed
# ---------------------------------------------------------------------------


class TestParsedWriteBack:
    """The lazy re-parse path must update the payload without ageing the row.

    ``updated_at`` is the age of the *Blizzard* data: the SWR layer derives
    staleness from it and nginx turns it into the ``Age`` header. Bumping it
    when all we did was re-parse bytes we already had would make a day-old row
    report an age of zero and silently suppress its background refresh.
    """

    @pytest.mark.asyncio
    async def test_static_write_back_leaves_updated_at_alone(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.set_static_data_parsed("heroes", [{"key": "ana"}], 2)

        sql = conn.execute.call_args[0][0]
        assert "UPDATE static_data" in sql
        assert "updated_at" not in sql

    @pytest.mark.asyncio
    async def test_player_write_back_leaves_updated_at_alone(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)

        await storage.set_player_profile_parsed("abc123", {"summary": {}}, 2)

        sql = conn.execute.call_args[0][0]
        assert "UPDATE player_profiles" in sql
        assert "updated_at" not in sql

    @pytest.mark.asyncio
    async def test_static_write_back_binds_payload_and_version(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        parsed = [{"key": "ana"}]

        await storage.set_static_data_parsed("heroes", parsed, 7)

        args = conn.execute.call_args[0]
        assert args[1:] == ("heroes", parsed, 7)

    @pytest.mark.asyncio
    async def test_player_write_back_binds_payload_and_version(self):
        pool, conn = _make_pool()
        storage = _make_storage(pool=pool)
        parsed = {"summary": {"username": "TeKrop"}}

        await storage.set_player_profile_parsed("abc123", parsed, 7)

        args = conn.execute.call_args[0]
        assert args[1:] == ("abc123", parsed, 7)
