"""Unit tests for PlayerService domain service"""

import asyncio
import time
from fnmatch import fnmatch
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest
from fastapi import HTTPException, status

from app.config import settings
from app.domain.exceptions import (
    ParserBlizzardError,
    ParserInternalError,
    ParserParsingError,
)
from app.domain.models.player import (
    BlizzardSearchPlayer,
    PlayerIdentity,
    PlayerProfileData,
)
from app.domain.parsers import PARSER_VERSION
from app.domain.services.player_service import (
    _INFLIGHT_LOCKS,
    _PARSED_PROFILE_CACHE,
    _PARSED_PROFILE_CACHE_MAXSIZE,
    PlayerService,
    parse_stored_profile,
    single_flight,
)
from tests.fake_storage import FakeStorage
from tests.helpers import read_html_file

_TEKROP_HTML = read_html_file("players/TeKrop-2217.html") or ""
# Not a real Blizzard search payload (it uses "username" rather than "name",
# and omits fields BlizzardSearchPlayer declares) — most of these tests only
# round-trip it through storage, which stays untyped `dict`. Left as a plain
# dict for that reason; call sites that need the stricter type (below) cast
# it explicitly instead of narrowing every usage.
_PLAYER_SUMMARY = {
    "url": "abc123|def456",
    "username": "TeKrop",
    "avatar": "https://example.com/avatar.png",
    "lastUpdated": 1700000000,
}
# parse_stored_profile below takes the stricter BlizzardSearchPlayer — cast
# once here rather than widen _PLAYER_SUMMARY's own type for its many other
# (untyped-storage) call sites.
_TYPED_PLAYER_SUMMARY = cast("BlizzardSearchPlayer", _PLAYER_SUMMARY)


def _make_service(
    *,
    storage: FakeStorage | None = None,
    cache: AsyncMock | None = None,
    task_queue: AsyncMock | None = None,
) -> PlayerService:
    if storage is None:
        storage = FakeStorage()
    if cache is None:
        cache = AsyncMock()
        cache.get_player_status = AsyncMock(return_value=None)
        cache.set_player_status = AsyncMock()
    if task_queue is None:
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
    blizzard_client = AsyncMock()
    return PlayerService(cache, storage, blizzard_client, task_queue)


# ---------------------------------------------------------------------------
# _calculate_retry_after
# ---------------------------------------------------------------------------


_BASE_RETRY = 60
_DOUBLED_RETRY = 120
_MAX_RETRY = 300


class TestCalculateRetryAfter:
    def test_first_check_returns_base(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_player_initial_retry = _BASE_RETRY
            s.unknown_player_retry_multiplier = 2
            s.unknown_player_max_retry = 3600
            result = svc._calculate_retry_after(1)

        assert result == _BASE_RETRY

    def test_second_check_doubles(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_player_initial_retry = _BASE_RETRY
            s.unknown_player_retry_multiplier = 2
            s.unknown_player_max_retry = 3600
            result = svc._calculate_retry_after(2)

        assert result == _DOUBLED_RETRY

    def test_max_retry_capped(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_player_initial_retry = _BASE_RETRY
            s.unknown_player_retry_multiplier = 10
            s.unknown_player_max_retry = _MAX_RETRY
            result = svc._calculate_retry_after(5)

        assert result == _MAX_RETRY


# ---------------------------------------------------------------------------
# _check_player_staleness
# ---------------------------------------------------------------------------


class TestCheckPlayerStaleness:
    def test_age_zero_never_stale(self):
        svc = _make_service()

        actual = svc._check_player_staleness(0)

        assert actual is False

    def test_age_below_half_threshold_not_stale(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            result = svc._check_player_staleness(1000)

        assert result is False

    def test_age_at_half_threshold_is_stale(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            result = svc._check_player_staleness(1800)

        assert result is True

    def test_age_above_half_threshold_is_stale(self):
        svc = _make_service()
        with patch("app.domain.services.player_service.settings") as s:
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            result = svc._check_player_staleness(2000)

        assert result is True


# ---------------------------------------------------------------------------
# get_player_profile_cache
# ---------------------------------------------------------------------------


class TestGetPlayerProfileCache:
    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self):
        svc = _make_service()
        result = await svc.get_player_profile_cache("nobody-0000")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_profile_on_hit(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        svc = _make_service(storage=storage)
        result = await svc.get_player_profile_cache("abc123")

        assert result is not None
        assert result["profile"] == _TEKROP_HTML
        assert result["summary"] == _PLAYER_SUMMARY


# ---------------------------------------------------------------------------
# _get_stored_profile
# ---------------------------------------------------------------------------


class TestGetStoredProfile:
    @pytest.mark.asyncio
    async def test_blizzard_id_no_profile_returns_none_with_zero_age(self):
        svc = _make_service()
        with patch(
            "app.domain.services.player_service.is_blizzard_id", return_value=True
        ):
            result = await svc._get_stored_profile("abc123|def456")

        assert result == (None, 0)

    @pytest.mark.asyncio
    async def test_battletag_no_mapping_returns_none_with_zero_age(self):
        svc = _make_service()
        with patch(
            "app.domain.services.player_service.is_blizzard_id", return_value=False
        ):
            result = await svc._get_stored_profile("TeKrop-2217")

        assert result == (None, 0)

    @pytest.mark.asyncio
    async def test_stale_profile_is_returned_with_its_age(self):
        """Age is reported, not judged — the caller decides what is too old."""
        storage = FakeStorage()
        await storage.set_player_profile("abc123", html=_TEKROP_HTML)
        storage._profiles["abc123"]["updated_at"] = int(time.time()) - 99999
        svc = _make_service(storage=storage)
        with patch(
            "app.domain.services.player_service.is_blizzard_id", return_value=True
        ):
            profile, age = await svc._get_stored_profile("abc123")

        assert profile is not None
        assert age == 99999  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_fresh_profile_returns_tuple(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123", html=_TEKROP_HTML, summary=_PLAYER_SUMMARY
        )
        svc = _make_service(storage=storage)
        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            result = await svc._get_stored_profile("abc123")

        assert result is not None
        profile, age = result

        assert profile is not None
        assert profile["profile"] == _TEKROP_HTML
        assert age >= 0


# ---------------------------------------------------------------------------
# _mark_player_unknown
# ---------------------------------------------------------------------------


class TestMarkPlayerUnknown:
    @pytest.mark.asyncio
    async def test_disabled_feature_is_noop(self):
        svc = _make_service()
        exc = ParserBlizzardError(
            status_code=status.HTTP_404_NOT_FOUND, message="not found"
        )
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = False
            await svc._mark_player_unknown("abc123", exc)
        cast("Any", svc.cache).set_player_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_404_is_noop(self):
        svc = _make_service()
        exc = ParserBlizzardError(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, message="error"
        )
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = True
            await svc._mark_player_unknown("abc123", exc)
        cast("Any", svc.cache).set_player_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_404_marks_player(self):
        cache = AsyncMock()
        cache.get_player_status = AsyncMock(return_value=None)
        cache.set_player_status = AsyncMock()
        svc = _make_service(cache=cache)
        exc = ParserBlizzardError(
            status_code=status.HTTP_404_NOT_FOUND, message="not found"
        )
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = True
            s.unknown_player_initial_retry = 60
            s.unknown_player_retry_multiplier = 2
            s.unknown_player_max_retry = 3600
            await svc._mark_player_unknown("abc123", exc, battletag="TeKrop-2217")

        cache.set_player_status.assert_awaited_once()
        assert isinstance(exc.message, dict)
        assert exc.message["error"] == "Player not found"
        assert "retry_after" in exc.message
        assert "next_check_at" in exc.message
        assert "check_count" in exc.message

    @pytest.mark.asyncio
    async def test_404_increments_check_count(self):
        cache = AsyncMock()
        cache.get_player_status = AsyncMock(return_value={"check_count": 3})
        cache.set_player_status = AsyncMock()
        svc = _make_service(cache=cache)
        exc = ParserBlizzardError(
            status_code=status.HTTP_404_NOT_FOUND, message="not found"
        )
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = True
            s.unknown_player_initial_retry = 60
            s.unknown_player_retry_multiplier = 2
            s.unknown_player_max_retry = 3600
            await svc._mark_player_unknown("abc123", exc)
        args = cache.set_player_status.call_args
        # check_count should be incremented from 3 to 4
        _expected_check_count = 4

        assert args[0][1] == _expected_check_count


# ---------------------------------------------------------------------------
# _handle_player_exceptions
# ---------------------------------------------------------------------------


class TestHandlePlayerExceptions:
    @pytest.mark.asyncio
    async def test_blizzard_404_raises_http_not_found(self):
        svc = _make_service()
        error = ParserBlizzardError(
            status_code=status.HTTP_404_NOT_FOUND, message="Player not found"
        )
        identity = PlayerIdentity()
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = False
            with pytest.raises(ParserBlizzardError) as exc_info:
                await svc._handle_player_exceptions(error, "TeKrop-2217", identity)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_parser_parsing_error_main_content_raises_404(self):
        svc = _make_service()
        error = ParserParsingError("Could not find main content in HTML")
        identity = PlayerIdentity()
        with patch("app.domain.services.player_service.settings") as s:
            s.unknown_players_cache_enabled = False
            with pytest.raises(ParserBlizzardError) as exc_info:
                await svc._handle_player_exceptions(error, "TeKrop-2217", identity)

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_parser_parsing_error_other_raises_parser_internal_error(self):
        svc = _make_service()
        error = ParserParsingError("Some DOM parsing failure")
        identity = PlayerIdentity(player_summary={"url": "abc123"})

        with patch("app.domain.services.player_service.settings") as s:
            s.blizzard_host = "https://overwatch.blizzard.com"
            s.career_path = "/career"
            with pytest.raises(ParserInternalError) as exc_info:
                await svc._handle_player_exceptions(error, "TeKrop-2217", identity)

        assert "overwatch.blizzard.com" in exc_info.value.blizzard_url
        assert exc_info.value.cause is error

    @pytest.mark.asyncio
    async def test_unrecognized_exception_type_reraises_unmarked(self):
        """An HTTPException raised by the Blizzard adapter (e.g. 503 rate limit)
        isn't a domain exception, so it passes through unchanged — no unknown-player
        marking, since the marking logic only inspects ParserBlizzardError/ParserParsingError."""
        svc = _make_service()
        error = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
        identity = PlayerIdentity()
        with pytest.raises(HTTPException) as exc_info:
            await svc._handle_player_exceptions(error, "TeKrop-2217", identity)

        assert exc_info.value is error
        cast("Any", svc.cache).set_player_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_exception_non_404_reraises(self):
        svc = _make_service()
        error = HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limited"
        )
        identity = PlayerIdentity()
        with pytest.raises(HTTPException) as exc_info:
            await svc._handle_player_exceptions(error, "TeKrop-2217", identity)

        assert exc_info.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS

    @pytest.mark.asyncio
    async def test_generic_exception_reraises(self):
        svc = _make_service()
        error = RuntimeError("unexpected")
        identity = PlayerIdentity()
        with pytest.raises(RuntimeError):
            await svc._handle_player_exceptions(error, "TeKrop-2217", identity)


# ---------------------------------------------------------------------------
# _execute_player_request — fast path and slow path
# ---------------------------------------------------------------------------


class TestExecutePlayerRequest:
    @pytest.mark.asyncio
    async def test_fast_path_from_storage(self):
        """When storage has a fresh profile, Blizzard is never called."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        svc = _make_service(storage=storage)
        data_factory = Mock(return_value={"result": "ok"})

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            result, _is_stale, _age = await svc._execute_player_request(
                "abc123|def456", "test-key", data_factory
            )

        assert result == {"result": "ok"}
        data_factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_slow_path_calls_blizzard(self):
        """When no fresh profile in storage, Blizzard is called."""
        mock_response = Mock(
            status_code=status.HTTP_200_OK,
            text=_TEKROP_HTML,
            url="https://overwatch.blizzard.com/career/TeKrop-2217/",
        )
        blizzard_client = AsyncMock()
        blizzard_client.get = AsyncMock(return_value=mock_response)
        svc = _make_service()
        svc.blizzard_client = blizzard_client

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=False
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 0
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.blizzard_host = "https://overwatch.blizzard.com"
            s.career_path = "/career"
            s.unknown_players_cache_enabled = False
            result, _is_stale, _age = await svc._execute_player_request(
                "TeKrop-2217", "test-key", lambda _profile: {"from": "blizzard"}
            )

        assert result == {"from": "blizzard"}

    @pytest.mark.asyncio
    async def test_stale_profile_enqueues_refresh(self):
        """When profile is old enough, is_stale=True and refresh is enqueued."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        # Past the staleness threshold but inside player_max_serve_age: served
        # from storage, with a refresh enqueued. The Blizzard mocks below stay
        # in place to prove they are NOT reached.
        storage._profiles["abc123|def456"]["updated_at"] = int(time.time()) - 9999
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            result, _is_stale, _age = await svc._execute_player_request(
                "abc123|def456", "test-key", lambda _profile: {}
            )
        assert result == {}
        assert task_queue.enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_fast_path_preserves_stored_at_in_cache(self):
        """When serving from storage, stored_at is forwarded to the cache write
        so the Lua Age header reflects the real data age, not the write time."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        original_updated_at = storage._profiles["abc123|def456"]["updated_at"]
        cache = AsyncMock()
        svc = _make_service(storage=storage, cache=cache)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.stale_cache_timeout = 60
            await svc._execute_player_request(
                "abc123|def456", "test-key", lambda _profile: {}
            )

        call_kwargs = cache.update_api_cache.call_args.kwargs
        assert call_kwargs["stored_at"] == original_updated_at

    @pytest.mark.asyncio
    async def test_stale_fast_path_sets_stale_while_revalidate(self):
        """When is_stale=True (storage path), stale_while_revalidate is set in the
        cache envelope so Lua emits the correct X-Cache-Status: stale header."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        # Age the profile into the stale window (>= threshold // 2)
        storage._profiles["abc123|def456"]["updated_at"] = int(time.time()) - 2000
        cache = AsyncMock()
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, cache=cache, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.stale_cache_timeout = 60
            _data, is_stale, _age = await svc._execute_player_request(
                "abc123|def456", "test-key", lambda _profile: {}
            )

        assert is_stale is True
        call_kwargs = cache.update_api_cache.call_args.kwargs
        assert call_kwargs["stale_while_revalidate"] == 60  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_fresh_blizzard_fetch_stored_at_is_none(self):
        """On a fresh Blizzard fetch (age=0), stored_at=None so the cache adapter
        stamps the current time, which is correct."""
        svc = _make_service()
        cache = AsyncMock()
        svc.cache = cache

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=False
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 0
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.stale_cache_timeout = 60
            s.blizzard_host = "https://overwatch.blizzard.com"
            s.career_path = "/career"
            s.unknown_players_cache_enabled = False
            await svc._execute_player_request(
                "TeKrop-2217", "test-key", lambda _profile: {}
            )

        call_kwargs = cache.update_api_cache.call_args.kwargs
        assert call_kwargs["stored_at"] is None


# ---------------------------------------------------------------------------
# refresh_player_profile — bypasses storage fast-path
# ---------------------------------------------------------------------------


class TestRefreshPlayerProfile:
    @pytest.mark.asyncio
    async def test_always_calls_blizzard_even_when_profile_is_fresh(self):
        """refresh_player_profile bypasses _get_fresh_stored_profile and always
        fetches from Blizzard, even when the stored profile is within the
        staleness threshold."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ) as mock_fetch,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = (
                99999  # profile would pass the fast-path check
            )
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.unknown_players_cache_enabled = False
            await svc.refresh_player_profile("abc123|def456")

        mock_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_persistent_storage(self):
        """refresh_player_profile writes a fresh profile to persistent storage."""
        storage = FakeStorage()
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.unknown_players_cache_enabled = False
            await svc.refresh_player_profile("abc123|def456")

        profile = await storage.get_player_profile("abc123|def456")
        assert profile is not None

    @pytest.mark.asyncio
    async def test_blizzard_error_propagates(self):
        """A ParserBlizzardError from identity resolution is re-raised as-is by
        _handle_player_exceptions — the worker's _run_refresh_task except block
        captures it."""
        svc = _make_service()
        err = ParserBlizzardError(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Blizzard unavailable"
        )

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=False
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                side_effect=err,
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.unknown_players_cache_enabled = False
            with pytest.raises(ParserBlizzardError) as exc_info:
                await svc.refresh_player_profile("TeKrop-2217")

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# parse_stored_profile — memoisation across the five player endpoints
# ---------------------------------------------------------------------------


class TestParseStoredProfileCache:
    """The parsed-profile cache is what stops all five player endpoints from
    re-running the same 10-20ms parse on the same stored HTML."""

    def test_second_call_reuses_the_parse(self):
        with patch(
            "app.domain.services.player_service.parse_player_profile_html",
            return_value={"summary": {}, "stats": {}},
        ) as parse:
            first = parse_stored_profile("p", 100, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)
            second = parse_stored_profile("p", 100, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)

        assert parse.call_count == 1
        assert first is second

    def test_new_updated_at_reparses(self):
        """A background refresh writes a new updated_at — it must not serve the
        stale parse."""
        with patch(
            "app.domain.services.player_service.parse_player_profile_html",
            side_effect=[{"v": 1}, {"v": 2}],
        ):
            first = parse_stored_profile("p", 100, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)
            second = parse_stored_profile("p", 200, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)

        # Distinct values can only come out if the parser ran twice.
        assert (first, second) == ({"v": 1}, {"v": 2})

    def test_players_do_not_share_an_entry(self):
        with patch(
            "app.domain.services.player_service.parse_player_profile_html",
            side_effect=[{"who": "a"}, {"who": "b"}],
        ):
            first = parse_stored_profile("a", 100, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)
            second = parse_stored_profile("b", 100, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)

        assert (first, second) == ({"who": "a"}, {"who": "b"})

    def test_evicts_least_recently_used_beyond_maxsize(self):
        """Bounded so a burst of distinct players cannot grow the process
        without limit — each parsed profile is a few hundred KB."""
        with patch(
            "app.domain.services.player_service.parse_player_profile_html",
            side_effect=lambda *_: {},
        ):
            for i in range(_PARSED_PROFILE_CACHE_MAXSIZE + 5):
                parse_stored_profile(f"p{i}", 1, _TEKROP_HTML, _TYPED_PLAYER_SUMMARY)

        assert len(_PARSED_PROFILE_CACHE) == _PARSED_PROFILE_CACHE_MAXSIZE
        assert ("p0", 1) not in _PARSED_PROFILE_CACHE
        assert ("p20", 1) in _PARSED_PROFILE_CACHE

    @pytest.mark.asyncio
    async def test_endpoints_share_one_parse_of_the_same_profile(self):
        """The real payoff: two endpoints hitting the same stored profile parse
        it once between them."""
        storage = FakeStorage()
        await storage.initialize()
        await storage.set_player_profile(
            player_id="abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
            battletag="TeKrop-2217",
            name="TeKrop",
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "TeKrop"}, "stats": {}},
            ) as parse,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            summary, _, _ = await svc.get_player_summary("abc123|def456", "k1")
            career, _, _ = await svc.get_player_career(
                "abc123|def456", None, None, "k2"
            )

        assert parse.call_count == 1
        assert summary == {"username": "TeKrop"}
        assert career["summary"] == {"username": "TeKrop"}

        await storage.close()


# ---------------------------------------------------------------------------
# _parse_stored — use the storage-parsed payload when current, else write one back
# ---------------------------------------------------------------------------


class TestParseStoredWriteBack:
    """A row's own ``parsed`` + ``data_version`` now do what the in-process
    cache used to: a storage hit at the current ``PARSER_VERSION`` needs no
    parse at all. Anything older is parsed once and the result is written
    back so the *next* hit takes the fast path too."""

    @pytest.mark.asyncio
    async def test_current_data_version_skips_parsing(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
            parsed={"summary": {"username": "stored"}, "stats": {}},
            data_version=PARSER_VERSION,
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html"
            ) as parse,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            summary, _, _ = await svc.get_player_summary("abc123|def456", "k1")

        parse.assert_not_called()
        assert summary == {"username": "stored"}

    @pytest.mark.asyncio
    async def test_stale_data_version_parses_and_writes_back(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
            parsed={"summary": {"username": "old"}, "stats": {}},
            data_version=PARSER_VERSION - 1,
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "fresh"}, "stats": {}},
            ) as parse,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            summary, _, _ = await svc.get_player_summary("abc123|def456", "k1")

        parse.assert_called_once()
        assert summary == {"username": "fresh"}

        row = await storage.get_player_profile("abc123|def456")
        assert row is not None
        assert row["parsed"] == {"summary": {"username": "fresh"}, "stats": {}}
        assert row["data_version"] == PARSER_VERSION

    @pytest.mark.asyncio
    async def test_missing_parsed_parses_and_writes_back(self):
        """A row written before the ``parsed`` column existed has ``parsed=None``
        even at the current ``data_version`` — must still be treated as needing
        a reparse, not as "nothing to compute"."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456", html=_TEKROP_HTML, summary=_PLAYER_SUMMARY
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "fresh"}, "stats": {}},
            ) as parse,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            await svc.get_player_summary("abc123|def456", "k1")

        parse.assert_called_once()
        row = await storage.get_player_profile("abc123|def456")
        assert row is not None
        assert row["parsed"] is not None
        assert row["data_version"] == PARSER_VERSION

    @pytest.mark.asyncio
    async def test_write_back_failure_does_not_break_the_request(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
            data_version=PARSER_VERSION - 1,
        )
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "fresh"}, "stats": {}},
            ),
            patch.object(
                storage,
                "set_player_profile_parsed",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            summary, _, _ = await svc.get_player_summary("abc123|def456", "k1")

        assert summary == {"username": "fresh"}

    @pytest.mark.asyncio
    async def test_write_back_does_not_change_reported_age(self):
        """A lazy reparse must not disturb ``updated_at`` — it is the age of the
        Blizzard profile, read by staleness checks and the ``Age`` header."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456",
            html=_TEKROP_HTML,
            summary=_PLAYER_SUMMARY,
            data_version=PARSER_VERSION - 1,
        )
        original_updated_at = storage._profiles["abc123|def456"]["updated_at"]
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {}, "stats": {}},
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            _data, _is_stale, age = await svc._execute_player_request(
                "abc123|def456", "test-key", lambda _profile: {}
            )

        assert age >= 0
        assert storage._profiles["abc123|def456"]["updated_at"] == original_updated_at

    @pytest.mark.asyncio
    async def test_fresh_blizzard_fetch_persists_parsed_profile(self):
        """A cold Blizzard fetch (the single-flight miss branch) stores the
        parsed profile too, so the very next read needs no parse."""
        svc = _make_service()

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=False
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "fresh"}, "stats": {}},
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 0
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.blizzard_host = "https://overwatch.blizzard.com"
            s.career_path = "/career"
            s.unknown_players_cache_enabled = False
            await svc._execute_player_request(
                "TeKrop-2217", "test-key", lambda _profile: {}
            )

        row = await svc.storage.get_player_profile("abc123|def456")
        assert row is not None
        assert row["parsed"] == {"summary": {"username": "fresh"}, "stats": {}}
        assert row["data_version"] == PARSER_VERSION

    @pytest.mark.asyncio
    async def test_refresh_persists_parsed_profile(self):
        """refresh_player_profile reuses its snapshot parse for storage too,
        rather than parsing the same HTML twice."""
        storage = FakeStorage()
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                return_value={"summary": {"username": "fresh"}, "stats": {}},
            ) as parse,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            s.unknown_players_cache_enabled = False
            await svc.refresh_player_profile("abc123|def456")

        parse.assert_called_once()
        row = await storage.get_player_profile("abc123|def456")
        assert row is not None
        assert row["parsed"] == {"summary": {"username": "fresh"}, "stats": {}}
        assert row["data_version"] == PARSER_VERSION


# ---------------------------------------------------------------------------
# single_flight — concurrent cold misses collapse to one Blizzard fetch
# ---------------------------------------------------------------------------


class TestSingleFlightColdMiss:
    """A cold player costs at least two Blizzard requests, all queued behind the
    same throttle. Concurrent requests for one player must not multiply that."""

    @pytest.mark.asyncio
    async def test_concurrent_cold_requests_fetch_blizzard_once(self):
        storage = FakeStorage()
        await storage.initialize()
        svc = _make_service(storage=storage)
        fetch_calls = 0

        async def _slow_fetch(_client, _pid):
            nonlocal fetch_calls
            fetch_calls += 1
            # Hold long enough that every sibling task is queued on the lock.
            await asyncio.sleep(0.05)
            return _TEKROP_HTML, "abc123|def456"

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                side_effect=_slow_fetch,
            ),
            patch.object(
                PlayerService,
                "_enrich_from_blizzard_id",
                new_callable=AsyncMock,
                return_value=({}, None),
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 99999
            s.player_max_serve_age = 86400
            s.prometheus_enabled = False
            s.career_path_cache_timeout = 300
            results = await asyncio.gather(
                *(svc.get_player_summary("abc123|def456", f"key-{i}") for i in range(8))
            )

        assert fetch_calls == 1
        assert all(r[0] for r in results)

    @pytest.mark.asyncio
    async def test_lock_is_released_and_dropped(self):
        """The lock dict must not grow without bound across distinct players."""
        async with single_flight("p1"):
            assert "p1" in _INFLIGHT_LOCKS

        assert _INFLIGHT_LOCKS == {}

    @pytest.mark.asyncio
    async def test_lock_dropped_even_when_the_body_raises(self):
        msg = "boom"
        with pytest.raises(ValueError, match=msg):
            async with single_flight("p2"):
                raise ValueError(msg)

        assert _INFLIGHT_LOCKS == {}

    @pytest.mark.asyncio
    async def test_distinct_players_do_not_block_each_other(self):
        order = []

        async def _work(key: str, delay: float):
            async with single_flight(key):
                await asyncio.sleep(delay)
                order.append(key)

        await asyncio.gather(_work("slow", 0.05), _work("fast", 0.0))

        assert order == ["fast", "slow"]


# ---------------------------------------------------------------------------
# Snapshot history
# ---------------------------------------------------------------------------


_BLIZZARD_ID = "abc123|def456"
_BATTLETAG = "TeKrop-2217"


async def _seed_stored_profile(storage: FakeStorage) -> None:
    """Store a fresh profile reachable by both its BattleTag and its Blizzard ID."""
    await storage.set_player_profile(
        _BLIZZARD_ID,
        html=_TEKROP_HTML,
        summary=_PLAYER_SUMMARY,
        battletag=_BATTLETAG,
    )


class TestStoreSnapshot:
    @pytest.mark.asyncio
    async def test_snapshot_recorded_on_the_storage_fast_path(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)

        await svc.get_player_summary(_BLIZZARD_ID, "key")

        snapshots = await storage.get_player_snapshots(_BLIZZARD_ID)
        assert len(snapshots) == 1
        assert snapshots[0]["last_updated_blizzard"] == _PLAYER_SUMMARY["lastUpdated"]
        assert snapshots[0]["data"]["heroes"]

    @pytest.mark.asyncio
    async def test_battletag_and_blizzard_id_share_one_series(self):
        """The storage fast path never resolves a BattleTag, so without the
        canonical lookup each spelling would grow its own half of the history."""
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)

        await svc.get_player_summary(_BATTLETAG, "key-battletag")
        await svc.get_player_summary(_BLIZZARD_ID, "key-blizzard-id")

        assert len(await storage.get_player_snapshots(_BLIZZARD_ID)) == 1
        assert await storage.get_player_snapshots(_BATTLETAG) == []

    @pytest.mark.asyncio
    async def test_repeated_requests_do_not_duplicate_a_row(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)

        await svc.get_player_summary(_BLIZZARD_ID, "key-1")
        await svc.get_player_career(_BLIZZARD_ID, None, None, "key-2")

        assert len(await storage.get_player_snapshots(_BLIZZARD_ID)) == 1

    @pytest.mark.asyncio
    async def test_storage_failure_does_not_break_the_request(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)
        storage.add_player_snapshot = AsyncMock(side_effect=Exception("DB gone"))

        data, _is_stale, _age = await svc.get_player_summary(_BLIZZARD_ID, "key")

        assert data["username"] == "TeKrop"

    @pytest.mark.asyncio
    async def test_nothing_is_recorded_without_a_blizzard_version_stamp(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)
        # Partial on purpose: _store_snapshot only ever reads summary.competitive
        # and summary.last_updated_at, so these tests exercise it with just
        # those keys rather than a full PlayerProfileSummary.
        parsed = cast(
            "PlayerProfileData",
            {
                "summary": {
                    "competitive": {"pc": {"tank": {"division": "gold", "tier": 1}}}
                },
                "stats": None,
            },
        )

        await svc._store_snapshot(_BLIZZARD_ID, parsed)

        assert await storage.get_player_snapshots(_BLIZZARD_ID) == []

    @pytest.mark.asyncio
    async def test_private_profile_records_nothing(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)
        parsed = cast(
            "PlayerProfileData",
            {"summary": {"last_updated_at": 1700000000}, "stats": None},
        )

        await svc._store_snapshot(_BLIZZARD_ID, parsed)

        assert await storage.get_player_snapshots(_BLIZZARD_ID) == []

    @pytest.mark.asyncio
    async def test_refresh_records_a_snapshot(self):
        storage = FakeStorage()
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, _BLIZZARD_ID),
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=_PLAYER_SUMMARY,
            ),
        ):
            await svc.refresh_player_profile(_BLIZZARD_ID)

        assert len(await storage.get_player_snapshots(_BLIZZARD_ID)) == 1

    @pytest.mark.asyncio
    async def test_refresh_survives_an_unparseable_profile(self):
        storage = FakeStorage()
        svc = _make_service(storage=storage)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, _BLIZZARD_ID),
            ),
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=_PLAYER_SUMMARY,
            ),
            patch(
                "app.domain.services.player_service.parse_player_profile_html",
                side_effect=Exception("markup changed"),
            ),
        ):
            await svc.refresh_player_profile(_BLIZZARD_ID)

        assert await storage.get_player_snapshots(_BLIZZARD_ID) == []


def _cache_holding(*keys: str) -> AsyncMock:
    """A cache whose ``scan_keys`` answers from a fixed keyspace, like Valkey."""
    cache = AsyncMock()
    cache.scan_keys = AsyncMock(
        side_effect=lambda pattern: [key for key in keys if fnmatch(key, pattern)]
    )
    return cache


class TestEvictionCoversTheHistoryEndpoints:
    @pytest.mark.asyncio
    async def test_glob_matches_history_and_diff_cache_keys(self):
        """The existing per-player glob already covers the two new routes —
        this asserts it rather than adding a second eviction pattern."""
        cache = AsyncMock()
        cache.scan_keys = AsyncMock(return_value=[])
        svc = _make_service(cache=cache)

        await svc._evict_player_cache_keys(_BATTLETAG)

        pattern = cache.scan_keys.call_args[0][0]
        prefix = f"{settings.api_cache_key_prefix}:"
        assert fnmatch(f"{prefix}/players/{_BATTLETAG}/history", pattern)
        assert fnmatch(f"{prefix}/players/{_BATTLETAG}/history?limit=50", pattern)
        assert fnmatch(f"{prefix}/players/{_BATTLETAG}/stats/diff", pattern)
        assert fnmatch(f"{prefix}/players/{_BATTLETAG}/stats/diff?since=1", pattern)

    @pytest.mark.asyncio
    async def test_glob_matches_percent_encoded_blizzard_id_keys(self):
        """A Blizzard ID reaches the service decoded ("abc|def") while its cache
        keys are stored percent-encoded ("abc%7Cdef"), so an unquoted glob
        matched nothing and the refresh evicted none of that player's keys."""
        cache = AsyncMock()
        cache.scan_keys = AsyncMock(return_value=[])
        svc = _make_service(cache=cache)

        await svc._evict_player_cache_keys(_BLIZZARD_ID)

        pattern = cache.scan_keys.call_args[0][0]
        prefix = f"{settings.api_cache_key_prefix}:"
        stored_key = f"{prefix}/players/{quote(_BLIZZARD_ID, safe='')}/summary"
        assert fnmatch(stored_key, pattern)

    @pytest.mark.asyncio
    async def test_battletag_refresh_also_clears_the_blizzard_id_keys(self):
        """Both identifiers reach the same profile, so both can be cached. A
        refresh triggered with the BattleTag used to clear only that glob,
        leaving the Blizzard-ID keys serving the pre-refresh payload."""
        prefix = f"{settings.api_cache_key_prefix}:"
        battletag_key = f"{prefix}/players/{_BATTLETAG}/summary"
        blizzard_key = f"{prefix}/players/{quote(_BLIZZARD_ID, safe='')}/summary"
        storage = FakeStorage()
        await storage.set_player_profile(
            _BLIZZARD_ID, html="<html></html>", battletag=_BATTLETAG
        )
        cache = _cache_holding(battletag_key, blizzard_key)
        svc = _make_service(cache=cache, storage=storage)

        await svc._evict_player_cache_keys(_BATTLETAG)

        assert cache.delete.await_args[0] == (battletag_key, blizzard_key)
        assert cache.delete.await_count == 1

    @pytest.mark.asyncio
    async def test_blizzard_id_refresh_also_clears_the_battletag_keys(self):
        """The mirror case: the profile row carries the battletag, so the keys
        cached under that spelling are reachable and must go too."""
        prefix = f"{settings.api_cache_key_prefix}:"
        battletag_key = f"{prefix}/players/{_BATTLETAG}/summary"
        blizzard_key = f"{prefix}/players/{quote(_BLIZZARD_ID, safe='')}/summary"
        storage = FakeStorage()
        await storage.set_player_profile(
            _BLIZZARD_ID, html="<html></html>", battletag=_BATTLETAG
        )
        cache = _cache_holding(battletag_key, blizzard_key)
        svc = _make_service(cache=cache, storage=storage)

        await svc._evict_player_cache_keys(_BLIZZARD_ID)

        assert cache.delete.await_args[0] == (blizzard_key, battletag_key)
        assert cache.delete.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_counterpart_falls_back_to_the_requested_form(self):
        """A player only ever requested one way has no counterpart — that is
        normal, and must cost neither an extra scan nor the eviction."""
        prefix = f"{settings.api_cache_key_prefix}:"
        battletag_key = f"{prefix}/players/{_BATTLETAG}/summary"
        cache = _cache_holding(battletag_key)
        storage = FakeStorage()
        lookup = AsyncMock(return_value=None)
        svc = _make_service(cache=cache, storage=storage)

        with patch.object(storage, "get_player_id_by_battletag", lookup):
            await svc._evict_player_cache_keys(_BATTLETAG)

        assert lookup.await_count == 1
        assert cache.scan_keys.await_count == 1
        assert cache.delete.await_args[0] == (battletag_key,)

    @pytest.mark.asyncio
    async def test_storage_failure_still_evicts_the_requested_form(self):
        """This runs after a completed refresh, so a storage hiccup costs the
        second glob, never the eviction — and never raises."""
        prefix = f"{settings.api_cache_key_prefix}:"
        battletag_key = f"{prefix}/players/{_BATTLETAG}/summary"
        cache = _cache_holding(battletag_key)
        storage = FakeStorage()
        lookup = AsyncMock(side_effect=Exception("postgres is down"))
        svc = _make_service(cache=cache, storage=storage)

        with patch.object(storage, "get_player_id_by_battletag", lookup):
            await svc._evict_player_cache_keys(_BATTLETAG)

        assert lookup.await_count == 1
        assert cache.delete.await_args[0] == (battletag_key,)


class TestGetPlayerHistory:
    @pytest.mark.asyncio
    async def test_returns_the_series_newest_first(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        await storage.add_player_snapshot(_BLIZZARD_ID, 1_600_000_000, {"old": True})
        svc = _make_service(storage=storage)

        data, _is_stale, _age = await svc.get_player_history(_BATTLETAG, "key")

        versions = [row["last_updated_blizzard"] for row in data["snapshots"]]
        assert versions == [_PLAYER_SUMMARY["lastUpdated"], 1_600_000_000]

    @pytest.mark.asyncio
    async def test_the_current_state_is_part_of_the_series(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)

        data, _is_stale, _age = await svc.get_player_history(_BLIZZARD_ID, "key")

        assert len(data["snapshots"]) == 1

    @pytest.mark.asyncio
    async def test_limit_is_honoured(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        await storage.add_player_snapshot(_BLIZZARD_ID, 1_600_000_000, {"old": True})
        svc = _make_service(storage=storage)

        data, _is_stale, _age = await svc.get_player_history(
            _BLIZZARD_ID, "key", limit=1
        )

        assert len(data["snapshots"]) == 1


class TestGetPlayerStatsDiff:
    @pytest.mark.asyncio
    async def test_no_history_is_not_an_error(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)

        data, _is_stale, _age = await svc.get_player_stats_diff(_BLIZZARD_ID, "key")

        # The warm-up request itself records the first point of the series.
        assert data["snapshots_compared"] == 1
        assert data["heroes"] == []
        assert data["ranks"] == []

    @pytest.mark.asyncio
    async def test_since_defaults_to_the_last_day(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        svc = _make_service(storage=storage)
        before = int(time.time())

        data, _is_stale, _age = await svc.get_player_stats_diff(_BLIZZARD_ID, "key")

        # Bracketed rather than compared against a single reading: the service
        # takes its own int(time.time()) and the assertion takes another, so a
        # call that happens to straddle a whole second made the difference 86401
        # and failed the run. Seen once in a full-suite run, never in isolation.
        after = int(time.time())
        one_day = 86400
        assert before - one_day <= data["since"] <= after - one_day

    @pytest.mark.asyncio
    async def test_compares_the_ends_of_the_window(self):
        storage = FakeStorage()
        await _seed_stored_profile(storage)
        older = {
            "endorsement": 3,
            "competitive": {},
            "heroes": {
                "pc": {
                    "quickplay": {
                        "ana": {"time_played": 0, "games_won": 0, "win_percentage": 0}
                    }
                }
            },
        }
        await storage.add_player_snapshot(_BLIZZARD_ID, 1_600_000_000, older)
        svc = _make_service(storage=storage)

        data, _is_stale, _age = await svc.get_player_stats_diff(_BLIZZARD_ID, "key")

        assert data["snapshots_compared"] == 2  # noqa: PLR2004
        assert data["totals"]["time_played"] > 0


# ---------------------------------------------------------------------------
# Serving past the staleness threshold
# ---------------------------------------------------------------------------


class TestServesStaleWithinCeiling:
    """A stored profile is served whatever its age, up to player_max_serve_age.

    Before this, anything past player_staleness_threshold fell through to a
    synchronous Blizzard fetch — so the first request for a profile nobody had
    asked about in an hour paid the full throttled round-trip. Measured against
    production: 3.5s for such a request, 0.21s once the profile was warm.
    """

    @pytest.mark.asyncio
    async def test_profile_past_the_threshold_is_served_without_blizzard(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456", html=_TEKROP_HTML, summary=_PLAYER_SUMMARY
        )
        storage._profiles["abc123|def456"]["updated_at"] = int(time.time()) - 7200
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
            ) as fetch,
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.career_path_cache_timeout = 300
            _data, is_stale, age = await svc._execute_player_request(
                "abc123|def456", "key", lambda _profile: {}
            )

        fetch.assert_not_awaited()
        assert is_stale is True
        assert age >= 7200  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_serving_stale_still_enqueues_the_refresh(self):
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456", html=_TEKROP_HTML, summary=_PLAYER_SUMMARY
        )
        storage._profiles["abc123|def456"]["updated_at"] = int(time.time()) - 7200
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.career_path_cache_timeout = 300
            await svc._execute_player_request("abc123|def456", "key", lambda _p: {})

        assert task_queue.enqueue.await_count == 1

    @pytest.mark.asyncio
    async def test_profile_past_the_ceiling_falls_back_to_blizzard(self):
        """The ceiling is the guard against a worker that stopped refreshing."""
        storage = FakeStorage()
        await storage.set_player_profile(
            "abc123|def456", html=_TEKROP_HTML, summary=_PLAYER_SUMMARY
        )
        storage._profiles["abc123|def456"]["updated_at"] = int(time.time()) - 200_000
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ) as fetch,
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.career_path_cache_timeout = 300
            await svc._execute_player_request("abc123|def456", "key", lambda _p: {})

        fetch.assert_awaited()

    @pytest.mark.asyncio
    async def test_absent_profile_still_fetches(self):
        """Nothing stored is the one case with genuinely nothing to serve."""
        storage = FakeStorage()
        task_queue = AsyncMock()
        task_queue.is_job_pending_or_running = AsyncMock(return_value=False)
        svc = _make_service(storage=storage, task_queue=task_queue)

        with (
            patch(
                "app.domain.services.player_service.is_blizzard_id", return_value=True
            ),
            patch(
                "app.domain.services.player_service.fetch_player_html",
                new_callable=AsyncMock,
                return_value=(_TEKROP_HTML, "abc123|def456"),
            ) as fetch,
            patch(
                "app.domain.services.player_service.fetch_player_summary_json",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "app.domain.services.player_service.parse_player_summary_json",
                return_value=None,
            ),
            patch("app.domain.services.player_service.settings") as s,
        ):
            s.player_staleness_threshold = 3600
            s.player_max_serve_age = 86400
            s.career_path_cache_timeout = 300
            await svc._execute_player_request("abc123|def456", "key", lambda _p: {})

        fetch.assert_awaited()
