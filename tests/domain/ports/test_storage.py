"""Tests for the StoragePort contract — exercised via FakeStorage"""

import datetime
import time

import pytest

from app.domain.ports.storage import StaticDataCategory


class TestStaticData:
    """Test static data storage operations"""

    @pytest.mark.asyncio
    async def test_set_and_get_static_data(self, storage_db):
        test_data = {"key": "hero-ana", "name": "Ana", "role": "support"}

        await storage_db.set_static_data(
            key="hero-ana",
            data=test_data,
            category=StaticDataCategory.HERO,
            data_version=1,
        )

        result = await storage_db.get_static_data("hero-ana")

        assert result is not None
        assert result["category"] == "hero"
        assert result["data_version"] == 1
        assert result["data"] == test_data
        assert result["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_get_nonexistent_static_data(self, storage_db):
        result = await storage_db.get_static_data("nonexistent-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_static_data(self, storage_db):
        await storage_db.set_static_data(
            key="hero-mercy",
            data={"version": 1},
            category=StaticDataCategory.HERO,
            data_version=1,
        )
        first = await storage_db.get_static_data("hero-mercy")

        await storage_db.set_static_data(
            key="hero-mercy",
            data={"version": 2},
            category=StaticDataCategory.HERO,
            data_version=2,
        )
        updated = await storage_db.get_static_data("hero-mercy")

        assert updated["data"]["version"] == 2  # noqa: PLR2004
        assert updated["data_version"] == 2  # noqa: PLR2004
        assert updated["updated_at"] >= first["updated_at"]


class TestPlayerProfiles:
    """Test player profile storage operations"""

    @pytest.mark.asyncio
    async def test_set_and_get_player_profile_with_summary(self, storage_db):
        player_id = "TeKrop-2217"
        html = "<html>Player profile data</html>"
        summary = {
            "name": "TeKrop",
            "isPublic": True,
            "lastUpdated": 1678536999,
            "url": "abc123",
        }

        await storage_db.set_player_profile(
            player_id=player_id, html=html, summary=summary
        )

        result = await storage_db.get_player_profile(player_id)
        assert result is not None
        assert result["html"] == html
        assert result["summary"] == summary
        assert result["updated_at"] > 0

    @pytest.mark.asyncio
    async def test_set_and_get_player_profile_without_summary(self, storage_db):
        player_id = "Player-1234"

        await storage_db.set_player_profile(
            player_id=player_id, html="<html/>", summary=None
        )

        result = await storage_db.get_player_profile(player_id)
        assert result is not None
        assert "url" in result["summary"]
        assert "lastUpdated" in result["summary"]
        assert result["last_updated_blizzard"] is None

    @pytest.mark.asyncio
    async def test_get_nonexistent_player_profile(self, storage_db):
        result = await storage_db.get_player_profile("NonExistent-9999")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_player_profile(self, storage_db):
        player_id = "UpdateTest-1111"

        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html>v1</html>",
            summary={"lastUpdated": 1000000},
        )
        first = await storage_db.get_player_profile(player_id)

        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html>v2</html>",
            summary={"lastUpdated": 2000000},
        )
        updated = await storage_db.get_player_profile(player_id)

        assert updated["html"] == "<html>v2</html>"
        assert updated["summary"]["lastUpdated"] == 2000000  # noqa: PLR2004
        assert updated["updated_at"] >= first["updated_at"]

    @pytest.mark.asyncio
    async def test_get_player_id_by_battletag(self, storage_db):
        player_id = "Player-1234"
        battletag = "TestPlayer-5678"

        await storage_db.set_player_profile(
            player_id=player_id,
            html="<html/>",
            summary={"url": player_id, "lastUpdated": 123},
            battletag=battletag,
        )

        actual = await storage_db.get_player_id_by_battletag(battletag)

        assert actual == player_id

    @pytest.mark.asyncio
    async def test_get_player_id_by_battletag_not_found(self, storage_db):
        actual = await storage_db.get_player_id_by_battletag("Unknown-9999")

        assert actual is None


class TestPlayerSnapshots:
    """Test the snapshot history operations"""

    @pytest.mark.asyncio
    async def test_add_and_get_snapshot(self, storage_db):
        data = {"endorsement": 3, "competitive": {}, "heroes": {}}

        await storage_db.add_player_snapshot("abc123", 1700000000, data)

        result = await storage_db.get_player_snapshots("abc123")
        assert len(result) == 1
        assert result[0]["last_updated_blizzard"] == 1700000000  # noqa: PLR2004
        assert result[0]["data"] == data
        assert result[0]["taken_at"] > 0

    @pytest.mark.asyncio
    async def test_get_snapshots_for_unknown_player(self, storage_db):
        result = await storage_db.get_player_snapshots("nobody")

        assert result == []

    @pytest.mark.asyncio
    async def test_same_version_is_stored_once(self, storage_db):
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 1})
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 2})

        result = await storage_db.get_player_snapshots("abc123")

        assert len(result) == 1
        assert result[0]["data"] == {"v": 1}

    @pytest.mark.asyncio
    async def test_snapshots_are_returned_newest_first(self, storage_db):
        for version in (1700000000, 1700000100, 1700000200):
            await storage_db.add_player_snapshot("abc123", version, {"v": version})

        result = await storage_db.get_player_snapshots("abc123")

        assert [row["last_updated_blizzard"] for row in result] == [
            1700000200,
            1700000100,
            1700000000,
        ]

    @pytest.mark.asyncio
    async def test_limit_keeps_the_newest(self, storage_db):
        for version in (1700000000, 1700000100, 1700000200):
            await storage_db.add_player_snapshot("abc123", version, {"v": version})

        result = await storage_db.get_player_snapshots("abc123", limit=2)

        assert [row["last_updated_blizzard"] for row in result] == [
            1700000200,
            1700000100,
        ]

    @pytest.mark.asyncio
    async def test_since_filters_on_taken_at(self, storage_db):
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 1})

        in_window = await storage_db.get_player_snapshots("abc123", since=1)
        out_of_window = await storage_db.get_player_snapshots(
            "abc123", since=int(time.time()) + 3600
        )

        assert len(in_window) == 1
        assert out_of_window == []

    @pytest.mark.asyncio
    async def test_snapshots_are_isolated_per_player(self, storage_db):
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 1})
        await storage_db.add_player_snapshot("def456", 1700000000, {"v": 2})

        result = await storage_db.get_player_snapshots("abc123")

        assert len(result) == 1
        assert result[0]["data"] == {"v": 1}

    @pytest.mark.asyncio
    async def test_delete_old_snapshots(self, storage_db):
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 1})

        kept = await storage_db.delete_old_player_snapshots(3600)
        deleted = await storage_db.delete_old_player_snapshots(0)

        assert kept == 0
        assert deleted == 1
        assert await storage_db.get_player_snapshots("abc123") == []

    @pytest.mark.asyncio
    async def test_snapshots_survive_a_profile_cleanup(self, storage_db):
        """No foreign key: the history outlives the profile it came from."""
        await storage_db.set_player_profile(
            player_id="abc123", html="<html/>", summary={"lastUpdated": 1700000000}
        )
        await storage_db.add_player_snapshot("abc123", 1700000000, {"v": 1})

        await storage_db.delete_old_player_profiles(0)

        assert await storage_db.get_player_profile("abc123") is None
        assert len(await storage_db.get_player_snapshots("abc123")) == 1


class TestHeroStatsSnapshots:
    """Test the hero stats history operations"""

    _SLICE = ("pc", "competitive", "europe")

    @pytest.mark.asyncio
    async def test_add_and_get_snapshot(self, storage_db):
        data = [{"hero": "ana", "winrate": 52.1, "pickrate": 8.3, "banrate": None}]

        await storage_db.add_hero_stats_snapshot(
            datetime.date(2026, 8, 29), *self._SLICE, data
        )

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE)
        assert result == [{"taken_on": datetime.date(2026, 8, 29), "data": data}]

    @pytest.mark.asyncio
    async def test_get_snapshots_for_unrecorded_region(self, storage_db):
        result = await storage_db.get_hero_stats_snapshots(
            "pc", "competitive", "americas"
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_same_day_is_stored_once(self, storage_db):
        taken_on = datetime.date(2026, 8, 29)
        await storage_db.add_hero_stats_snapshot(taken_on, *self._SLICE, [{"v": 1}])
        await storage_db.add_hero_stats_snapshot(taken_on, *self._SLICE, [{"v": 2}])

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE)

        assert len(result) == 1
        assert result[0]["data"] == [{"v": 1}]

    @pytest.mark.asyncio
    async def test_snapshots_are_returned_newest_first(self, storage_db):
        for day in (27, 28, 29):
            await storage_db.add_hero_stats_snapshot(
                datetime.date(2026, 8, day), *self._SLICE, [{"day": day}]
            )

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE)

        assert [row["taken_on"].day for row in result] == [29, 28, 27]

    @pytest.mark.asyncio
    async def test_limit_keeps_the_newest(self, storage_db):
        for day in (27, 28, 29):
            await storage_db.add_hero_stats_snapshot(
                datetime.date(2026, 8, day), *self._SLICE, [{"day": day}]
            )

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE, limit=2)

        assert [row["taken_on"].day for row in result] == [29, 28]

    @pytest.mark.asyncio
    async def test_since_filters_on_the_day(self, storage_db):
        for day in (27, 29):
            await storage_db.add_hero_stats_snapshot(
                datetime.date(2026, 8, day), *self._SLICE, [{"day": day}]
            )
        since = int(datetime.datetime(2026, 8, 28, tzinfo=datetime.UTC).timestamp())

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE, since=since)

        assert [row["taken_on"].day for row in result] == [29]

    @pytest.mark.asyncio
    async def test_snapshots_are_isolated_per_slice(self, storage_db):
        taken_on = datetime.date(2026, 8, 29)
        await storage_db.add_hero_stats_snapshot(taken_on, *self._SLICE, [{"v": 1}])
        await storage_db.add_hero_stats_snapshot(
            taken_on, "pc", "competitive", "asia", [{"v": 2}]
        )

        result = await storage_db.get_hero_stats_snapshots(*self._SLICE)

        assert len(result) == 1
        assert result[0]["data"] == [{"v": 1}]

    @pytest.mark.asyncio
    async def test_delete_old_snapshots(self, storage_db):
        today = datetime.datetime.now(tz=datetime.UTC).date()
        await storage_db.add_hero_stats_snapshot(today, *self._SLICE, [{"v": 1}])
        await storage_db.add_hero_stats_snapshot(
            today - datetime.timedelta(days=10), *self._SLICE, [{"v": 2}]
        )

        deleted = await storage_db.delete_old_hero_stats_snapshots(86400)

        assert deleted == 1
        assert [
            row["taken_on"]
            for row in await storage_db.get_hero_stats_snapshots(*self._SLICE)
        ] == [today]


class TestStorageStats:
    """Test storage statistics"""

    @pytest.mark.asyncio
    async def test_large_html_integrity(self, storage_db):
        large_html = "<html>" + ("x" * 10000) + "</html>"
        await storage_db.set_player_profile(
            player_id="LargeHTML-5555", html=large_html, summary={"name": "Large"}
        )
        result = await storage_db.get_player_profile("LargeHTML-5555")
        assert result["html"] == large_html

    @pytest.mark.asyncio
    async def test_unicode_data_integrity(self, storage_db):
        test_data = {
            "name": "Lúcio",
            "emoji": "🎵🎶",
            "description": "Héros de soutien",
        }
        await storage_db.set_static_data(
            key="hero-lucio", data=test_data, category=StaticDataCategory.HERO
        )
        result = await storage_db.get_static_data("hero-lucio")
        assert result["data"]["name"] == "Lúcio"
        assert result["data"]["emoji"] == "🎵🎶"
