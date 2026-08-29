"""Tests for the hero stats history parts of HeroService"""

import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import PlayerGamemode, PlayerPlatform, PlayerRegion
from app.domain.services.hero_service import (
    HERO_STATS_SNAPSHOT_SLICES,
    HeroService,
)
from tests.fake_storage import FakeStorage

_STATS_ROW = {
    "hero": "ana",
    "winrate": 52.1,
    "pickrate": 8.3,
    "banrate": None,
    "role": "support",
    "subrole": "medic",
    "color": "#48699e",
}
_TODAY = datetime.datetime.now(tz=datetime.UTC).date()


def _make_hero_service(storage: Any = None) -> tuple[HeroService, Any]:
    storage = FakeStorage() if storage is None else storage
    cache = AsyncMock()
    blizzard_client = AsyncMock()
    task_queue = AsyncMock()
    task_queue.is_job_pending_or_running.return_value = False
    return HeroService(cache, storage, blizzard_client, task_queue), storage


class TestCanonicalSlices:
    def test_one_slice_per_region_on_pc_competitive(self):
        slices = HERO_STATS_SNAPSHOT_SLICES

        assert slices == tuple(
            (PlayerPlatform.PC, PlayerGamemode.COMPETITIVE, region)
            for region in PlayerRegion
        )
        assert len(slices) == 3  # noqa: PLR2004


class TestRecordHeroStatsSnapshots:
    @pytest.mark.asyncio
    async def test_records_one_row_per_region(self):
        svc, storage = _make_hero_service()

        with patch.object(
            HeroService, "_fetch_hero_stats", AsyncMock(return_value=[_STATS_ROW])
        ):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == 3  # noqa: PLR2004
        assert sorted(storage._hero_stats) == sorted(
            (_TODAY, "pc", "competitive", str(region)) for region in PlayerRegion
        )

    @pytest.mark.asyncio
    async def test_stores_only_the_three_rates(self):
        svc, storage = _make_hero_service()

        with patch.object(
            HeroService, "_fetch_hero_stats", AsyncMock(return_value=[_STATS_ROW])
        ):
            await svc.record_hero_stats_snapshots()

        result = await storage.get_hero_stats_snapshots("pc", "competitive", "europe")
        assert result == [
            {
                "taken_on": _TODAY,
                "data": [
                    {"hero": "ana", "winrate": 52.1, "pickrate": 8.3, "banrate": None}
                ],
            }
        ]

    @pytest.mark.asyncio
    async def test_fetches_the_unfiltered_slice(self):
        svc, _ = _make_hero_service()
        fetch = AsyncMock(return_value=[_STATS_ROW])

        with patch.object(HeroService, "_fetch_hero_stats", fetch):
            await svc.record_hero_stats_snapshots()

        # role, map and competitive division filters are all None
        assert fetch.call_args.args[3:] == (None, None, None, "hero:asc")

    @pytest.mark.asyncio
    async def test_one_failing_region_does_not_abort_the_others(self):
        svc, storage = _make_hero_service()
        fetch = AsyncMock(
            side_effect=[Exception("Blizzard is down"), [_STATS_ROW], [_STATS_ROW]]
        )

        with patch.object(HeroService, "_fetch_hero_stats", fetch):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == 2  # noqa: PLR2004
        assert [key[3] for key in storage._hero_stats] == [
            str(PlayerRegion.AMERICAS),
            str(PlayerRegion.ASIA),
        ]

    @pytest.mark.asyncio
    async def test_a_storage_failure_only_costs_its_own_region(self):
        storage = AsyncMock()
        storage.add_hero_stats_snapshot.side_effect = [Exception("DB gone"), None, None]
        svc, _ = _make_hero_service(storage)

        with patch.object(
            HeroService, "_fetch_hero_stats", AsyncMock(return_value=[_STATS_ROW])
        ):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == 2  # noqa: PLR2004


class TestGetHeroStatsHistory:
    _ROWS = [  # noqa: RUF012
        {
            "taken_on": datetime.date(2026, 8, 29),
            "data": [
                {"hero": "ana", "winrate": 52.1, "pickrate": 8.3, "banrate": None},
                {"hero": "mercy", "winrate": 49.0, "pickrate": 6.0, "banrate": None},
            ],
        },
        {
            "taken_on": datetime.date(2026, 8, 28),
            "data": [
                {"hero": "mercy", "winrate": 48.0, "pickrate": 5.5, "banrate": None}
            ],
        },
    ]

    def _make_service_with_rows(self, rows: list[dict]) -> HeroService:
        storage = AsyncMock()
        storage.get_hero_stats_snapshots.return_value = rows
        svc, _ = _make_hero_service(storage)
        return svc

    @pytest.mark.asyncio
    async def test_reads_the_canonical_slice_only(self):
        storage = AsyncMock()
        storage.get_hero_stats_snapshots.return_value = []
        svc, _ = _make_hero_service(storage)

        await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE, cache_key="/heroes/stats/history", limit=10
        )

        call = storage.get_hero_stats_snapshots.call_args
        assert call.args == ("pc", "competitive", "europe")
        assert call.kwargs == {"since": None, "limit": 10}

    @pytest.mark.asyncio
    async def test_returns_the_full_series_without_a_hero_filter(self):
        svc = self._make_service_with_rows(self._ROWS)

        data, is_stale, age = await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE, cache_key="/heroes/stats/history"
        )

        assert (is_stale, age) == (False, 0)
        assert data["region"] == "europe"
        assert [snapshot["taken_on"] for snapshot in data["snapshots"]] == [
            "2026-08-29",
            "2026-08-28",
        ]
        assert len(data["snapshots"][0]["stats"]) == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_hero_filter_drops_the_days_without_that_hero(self):
        svc = self._make_service_with_rows(self._ROWS)

        data, _, _ = await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE, cache_key="/heroes/stats/history", hero="ana"
        )

        assert data["snapshots"] == [
            {
                "taken_on": "2026-08-29",
                "stats": [
                    {"hero": "ana", "winrate": 52.1, "pickrate": 8.3, "banrate": None}
                ],
            }
        ]

    @pytest.mark.asyncio
    async def test_empty_history_is_an_empty_list(self):
        svc = self._make_service_with_rows([])

        data, _, _ = await svc.get_hero_stats_history(
            region=PlayerRegion.ASIA, cache_key="/heroes/stats/history"
        )

        assert data == {"region": "asia", "snapshots": []}
