"""In-memory FakeStorage implementing StoragePort — used in tests only."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.domain.parsers import PARSER_VERSION

if TYPE_CHECKING:
    from datetime import date

    from app.domain.ports.storage import StaticDataCategory


class FakeStorage:
    """
    In-memory storage stub that satisfies ``StoragePort``.

    All data lives in plain dicts — no DB, no compression, no I/O.
    Provides the same interface as ``PostgresStorage`` so unit tests
    run without a real database.
    """

    def __init__(self) -> None:
        self._static: dict[str, dict] = {}
        self._profiles: dict[str, dict] = {}
        self._battletag_index: dict[str, str] = {}
        # player_id -> last_updated_blizzard -> row, mirroring the composite
        # primary key that makes the real INSERT idempotent.
        self._snapshots: dict[str, dict[int, dict]] = {}
        # (taken_on, platform, gamemode, region) -> payload, again mirroring the
        # real primary key.
        self._hero_stats: dict[tuple[date, str, str, str], list[dict]] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # Static data
    # ------------------------------------------------------------------ #

    async def get_static_data(self, key: str) -> dict | None:
        return self._static.get(key)

    async def set_static_data(
        self,
        key: str,
        data: str,
        category: StaticDataCategory,
        data_version: int = PARSER_VERSION,
        parsed: dict | list | None = None,
    ) -> None:
        now = int(time.time())
        existing = self._static.get(key)
        self._static[key] = {
            "data": data,
            "parsed": parsed,
            "category": str(category),
            "data_version": data_version,
            "updated_at": now,
            "created_at": existing["created_at"] if existing else now,
        }

    async def set_static_data_parsed(
        self,
        key: str,
        parsed: dict | list,
        data_version: int = PARSER_VERSION,
    ) -> None:
        existing = self._static.get(key)
        if existing is None:
            return
        # updated_at is left untouched on purpose — see the port docstring.
        existing["parsed"] = parsed
        existing["data_version"] = data_version

    # ------------------------------------------------------------------ #
    # Player profiles
    # ------------------------------------------------------------------ #

    async def get_player_profile(self, player_id: str) -> dict | None:
        profile = self._profiles.get(player_id)
        if profile is None:
            return None
        summary = profile.get("summary") or {}
        if not summary:
            summary = {
                "url": player_id,
                "lastUpdated": profile.get("last_updated_blizzard"),
            }
        return {**profile, "summary": summary}

    async def get_player_id_by_battletag(self, battletag: str) -> str | None:
        return self._battletag_index.get(battletag)

    async def set_player_profile(
        self,
        player_id: str,
        html: str,
        summary: dict | None = None,
        battletag: str | None = None,
        name: str | None = None,
        last_updated_blizzard: int | None = None,
        data_version: int = PARSER_VERSION,
        parsed: dict | None = None,
    ) -> None:
        now = int(time.time())
        existing = self._profiles.get(player_id)
        self._profiles[player_id] = {
            "html": html,
            "parsed": parsed,
            "summary": summary or {},
            "battletag": battletag or (existing["battletag"] if existing else None),
            "name": name or (existing["name"] if existing else None),
            "last_updated_blizzard": last_updated_blizzard,
            "updated_at": now,
            "created_at": existing["created_at"] if existing else now,
            "data_version": data_version,
        }
        if battletag:
            self._battletag_index[battletag] = player_id

    async def set_player_profile_parsed(
        self,
        player_id: str,
        parsed: dict,
        data_version: int = PARSER_VERSION,
    ) -> None:
        existing = self._profiles.get(player_id)
        if existing is None:
            return
        # updated_at is left untouched on purpose — see the port docstring.
        existing["parsed"] = parsed
        existing["data_version"] = data_version

    # ------------------------------------------------------------------ #
    # Player snapshots
    # ------------------------------------------------------------------ #

    async def add_player_snapshot(
        self,
        player_id: str,
        last_updated_blizzard: int,
        data: dict,
    ) -> None:
        # setdefault, not assignment: ON CONFLICT DO NOTHING keeps the first row.
        self._snapshots.setdefault(player_id, {}).setdefault(
            last_updated_blizzard,
            {
                # Sub-second precision so ordering stays deterministic when a
                # test writes several snapshots in the same second.
                "taken_at": time.time(),
                "last_updated_blizzard": last_updated_blizzard,
                "data": data,
            },
        )

    async def get_player_snapshots(
        self,
        player_id: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        rows = [
            row
            for row in self._snapshots.get(player_id, {}).values()
            if since is None or row["taken_at"] >= since
        ]
        rows.sort(
            key=lambda row: (row["taken_at"], row["last_updated_blizzard"]),
            reverse=True,
        )
        return [{**row, "taken_at": int(row["taken_at"])} for row in rows[:limit]]

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
        # setdefault, not assignment: ON CONFLICT DO NOTHING keeps the first row
        # recorded for this day and filter combination.
        self._hero_stats.setdefault((taken_on, platform, gamemode, region), data)

    async def get_hero_stats_snapshots(
        self,
        platform: str,
        gamemode: str,
        region: str,
        since: int | None = None,
        limit: int = 30,
    ) -> list[dict]:
        since_date = (
            None if since is None else datetime.fromtimestamp(since, tz=UTC).date()
        )
        rows = [
            {"taken_on": key[0], "data": data}
            for key, data in self._hero_stats.items()
            if key[1:] == (platform, gamemode, region)
            and (since_date is None or key[0] >= since_date)
        ]
        rows.sort(key=lambda row: row["taken_on"], reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    async def delete_old_player_profiles(self, max_age_seconds: int) -> int:
        cutoff = time.time() - max_age_seconds
        to_delete = [
            pid for pid, p in self._profiles.items() if p["updated_at"] < cutoff
        ]
        for pid in to_delete:
            bt = self._profiles[pid].get("battletag")
            if bt:
                self._battletag_index.pop(bt, None)
            del self._profiles[pid]
        return len(to_delete)

    async def delete_old_player_snapshots(self, max_age_seconds: int) -> int:
        cutoff = time.time() - max_age_seconds
        deleted = 0
        for player_id, versions in list(self._snapshots.items()):
            stale = [
                version for version, row in versions.items() if row["taken_at"] < cutoff
            ]
            for version in stale:
                del versions[version]
            deleted += len(stale)
            if not versions:
                del self._snapshots[player_id]
        return deleted

    async def delete_old_hero_stats_snapshots(self, max_age_seconds: int) -> int:
        cutoff = (datetime.now(tz=UTC) - timedelta(seconds=max_age_seconds)).date()
        stale = [key for key in self._hero_stats if key[0] < cutoff]
        for key in stale:
            del self._hero_stats[key]
        return len(stale)

    async def clear_all_data(self) -> None:
        self._static.clear()
        self._profiles.clear()
        self._battletag_index.clear()
        self._snapshots.clear()
        self._hero_stats.clear()
