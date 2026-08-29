"""Tests for app/adapters/tasks/worker.py — task functions and helpers"""

import contextlib
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.tasks.worker import (
    _run_refresh_task,
    cleanup_stale_players,
    refresh_gamemodes,
    refresh_hero,
    refresh_heroes,
    refresh_maps,
    refresh_player_profile,
    refresh_roles,
)
from app.domain.enums import HeroKey, Locale

# ── _run_refresh_task ─────────────────────────────────────────────────────────


class TestRunRefreshTask:
    @pytest.mark.asyncio
    async def test_release_job_called_on_success(self):
        """release_job is called with entity_id after a successful refresh."""
        mock_queue = AsyncMock()

        async with _run_refresh_task("Player-1234", mock_queue):
            pass

        mock_queue.release_job.assert_awaited_once_with("Player-1234")

    @pytest.mark.asyncio
    async def test_release_job_called_on_failure(self):
        """release_job is called with entity_id even when the refresh raises."""
        mock_queue = AsyncMock()

        async def _fail():
            async with _run_refresh_task("hero:ana:en-us", mock_queue):
                msg = "oops"
                raise RuntimeError(msg)

        with contextlib.suppress(RuntimeError):
            await _fail()

        mock_queue.release_job.assert_awaited_once_with("hero:ana:en-us")

    @pytest.mark.asyncio
    async def test_exception_is_reraised(self):
        """The context manager logs the failure but must not swallow it —
        taskiq needs the exception to mark the job failed."""
        mock_queue = AsyncMock()
        msg = "task failed"

        async def _fail():
            async with _run_refresh_task("maps:all", mock_queue):
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match=msg):
            await _fail()


# ── refresh tasks ─────────────────────────────────────────────────────────────


class TestRefreshHeroes:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_list(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()

        await cast("Any", refresh_heroes).__wrapped__(
            "heroes:en-us", mock_service, mock_queue
        )

        mock_service.refresh_list.assert_awaited_once_with(Locale.ENGLISH_US)


class TestRefreshHero:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_single(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()
        first_key = str(next(iter(HeroKey)))

        await cast("Any", refresh_hero).__wrapped__(
            f"hero:{first_key}:en-us", mock_service, mock_queue
        )

        mock_service.refresh_single.assert_awaited_once_with(
            first_key, Locale.ENGLISH_US
        )


class TestRefreshRoles:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_list(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()

        await cast("Any", refresh_roles).__wrapped__(
            "roles:fr-fr", mock_service, mock_queue
        )

        mock_service.refresh_list.assert_awaited_once_with(Locale.FRENCH)


class TestRefreshMaps:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_list(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()

        await cast("Any", refresh_maps).__wrapped__(
            "maps:all", mock_service, mock_queue
        )

        mock_service.refresh_list.assert_awaited_once()


class TestRefreshGamemodes:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_list(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()

        await cast("Any", refresh_gamemodes).__wrapped__(
            "gamemodes:all", mock_service, mock_queue
        )

        mock_service.refresh_list.assert_awaited_once()


class TestRefreshPlayerProfile:
    @pytest.mark.asyncio
    async def test_calls_service_refresh_player_profile(self):
        mock_service = AsyncMock()
        mock_queue = AsyncMock()

        await cast("Any", refresh_player_profile).__wrapped__(
            "Player-1234", mock_service, mock_queue
        )

        mock_service.refresh_player_profile.assert_awaited_once_with("Player-1234")


# ── cleanup_stale_players ─────────────────────────────────────────────────────


class TestCleanupStalePlayers:
    @pytest.mark.asyncio
    async def test_skipped_when_max_age_zero(self):
        mock_storage = AsyncMock()
        with patch("app.adapters.tasks.worker.settings") as mock_settings:
            mock_settings.player_profile_max_age = 0
            await cast("Any", cleanup_stale_players).__wrapped__(mock_storage)

        mock_storage.delete_old_player_profiles.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_calls_delete_old_player_profiles(self):
        mock_storage = AsyncMock()
        with patch("app.adapters.tasks.worker.settings") as mock_settings:
            mock_settings.player_profile_max_age = 86400
            await cast("Any", cleanup_stale_players).__wrapped__(mock_storage)

        mock_storage.delete_old_player_profiles.assert_awaited_once_with(86400)

    @pytest.mark.asyncio
    async def test_storage_exception_is_swallowed(self):
        mock_storage = AsyncMock()
        mock_storage.delete_old_player_profiles.side_effect = Exception("DB gone")
        with patch("app.adapters.tasks.worker.settings") as mock_settings:
            mock_settings.player_profile_max_age = 3600
            # Should not propagate
            await cast("Any", cleanup_stale_players).__wrapped__(mock_storage)
