"""Tests for the hero stats history parts of HeroService"""

import datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.enums import (
    CompetitiveDivisionFilter,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
)
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
    return HeroService(cache, storage, blizzard_client, task_queue), storage


class TestCanonicalSlices:
    def test_one_unfiltered_and_one_per_division_slice_per_region(self):
        slices = HERO_STATS_SNAPSHOT_SLICES

        assert slices == tuple(
            (PlayerPlatform.PC, PlayerGamemode.COMPETITIVE, region, division)
            for region in PlayerRegion
            for division in (None, *CompetitiveDivisionFilter)
        )
        # 3 regions x (1 unfiltered + 8 divisions)
        assert len(slices) == 27  # noqa: PLR2004


class TestRecordHeroStatsSnapshots:
    @pytest.mark.asyncio
    async def test_records_one_row_per_region_and_division(self):
        svc, storage = _make_hero_service()

        with patch.object(
            HeroService, "_fetch_hero_stats", AsyncMock(return_value=[_STATS_ROW])
        ):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == len(HERO_STATS_SNAPSHOT_SLICES)
        assert sorted(storage._hero_stats) == sorted(
            (_TODAY, "pc", "competitive", str(region), division_str)
            for region in PlayerRegion
            for division_str in ("all", *(str(d) for d in CompetitiveDivisionFilter))
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
    async def test_fetches_the_unfiltered_slice_for_every_region(self):
        svc, _ = _make_hero_service()
        fetch = AsyncMock(return_value=[_STATS_ROW])

        with patch.object(HeroService, "_fetch_hero_stats", fetch):
            await svc.record_hero_stats_snapshots()

        # role and map filters are always None; one call per region carries a
        # None (unfiltered) competitive division alongside the per-division ones.
        unfiltered_calls = [
            call
            for call in fetch.call_args_list
            if call.args[3:] == (None, None, None, "hero:asc")
        ]
        assert len(unfiltered_calls) == len(PlayerRegion)

    @pytest.mark.asyncio
    async def test_fetches_every_division_for_every_region(self):
        svc, _ = _make_hero_service()
        fetch = AsyncMock(return_value=[_STATS_ROW])

        with patch.object(HeroService, "_fetch_hero_stats", fetch):
            await svc.record_hero_stats_snapshots()

        divisions_called = {call.args[5] for call in fetch.call_args_list}
        assert divisions_called == {None, *CompetitiveDivisionFilter}

    @pytest.mark.asyncio
    async def test_one_failing_slice_does_not_abort_the_rest(self):
        svc, storage = _make_hero_service()
        total = len(HERO_STATS_SNAPSHOT_SLICES)
        fetch = AsyncMock(
            side_effect=[Exception("Blizzard is down"), *([[_STATS_ROW]] * (total - 1))]
        )

        with patch.object(HeroService, "_fetch_hero_stats", fetch):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == total - 1
        # the first slice attempted (first region, unfiltered) recorded nothing
        assert (_TODAY, "pc", "competitive", "europe", "all") not in storage._hero_stats
        assert len(storage._hero_stats) == total - 1

    @pytest.mark.asyncio
    async def test_a_storage_failure_only_costs_its_own_slice(self):
        storage = AsyncMock()
        total = len(HERO_STATS_SNAPSHOT_SLICES)
        storage.add_hero_stats_snapshot.side_effect = [
            Exception("DB gone"),
            *([None] * (total - 1)),
        ]
        svc, _ = _make_hero_service(storage)

        with patch.object(
            HeroService, "_fetch_hero_stats", AsyncMock(return_value=[_STATS_ROW])
        ):
            recorded = await svc.record_hero_stats_snapshots()

        assert recorded == total - 1


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
    async def test_reads_the_all_division_by_default(self):
        storage = AsyncMock()
        storage.get_hero_stats_snapshots.return_value = []
        svc, _ = _make_hero_service(storage)

        await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE, cache_key="/heroes/stats/history", limit=10
        )

        call = storage.get_hero_stats_snapshots.call_args
        assert call.args == ("pc", "competitive", "europe")
        assert call.kwargs == {"since": None, "limit": 10, "division": "all"}

    @pytest.mark.asyncio
    async def test_reads_the_given_division(self):
        storage = AsyncMock()
        storage.get_hero_stats_snapshots.return_value = []
        svc, _ = _make_hero_service(storage)

        data, _, _ = await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE,
            cache_key="/heroes/stats/history",
            division=CompetitiveDivisionFilter.GOLD,
        )

        call = storage.get_hero_stats_snapshots.call_args
        assert call.kwargs["division"] == "gold"
        assert data["competitive_division"] == "gold"

    @pytest.mark.asyncio
    async def test_returns_the_full_series_without_a_hero_filter(self):
        svc = self._make_service_with_rows(self._ROWS)

        data, is_stale, age = await svc.get_hero_stats_history(
            region=PlayerRegion.EUROPE, cache_key="/heroes/stats/history"
        )

        assert (is_stale, age) == (False, 0)
        assert data["region"] == "europe"
        assert data["competitive_division"] is None
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

        assert data == {
            "region": "asia",
            "competitive_division": None,
            "snapshots": [],
        }
