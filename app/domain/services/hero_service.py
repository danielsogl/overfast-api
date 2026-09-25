"""Hero domain service — heroes list, hero detail, hero stats"""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from app.config import settings
from app.domain.enums import (
    CompetitiveDivisionFilter,
    Locale,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
    SubRole,
)
from app.domain.exceptions import (
    InvalidGamemodeFilterError,
    ParserInternalError,
    ParserParsingError,
)
from app.domain.models.hero import HeroDetail, HeroListEntry
from app.domain.parsers.hero import fetch_hero_html, parse_hero_html
from app.domain.parsers.hero_stats_summary import (
    build_hero_stats_snapshot,
    parse_hero_stats_summary,
)
from app.domain.parsers.heroes import (
    fetch_heroes_html,
    filter_heroes,
    parse_heroes_html,
)
from app.domain.parsers.heroes_hitpoints import parse_heroes_hitpoints
from app.domain.services import SwrResult
from app.domain.services.static_data_service import StaticDataService, StaticFetchConfig
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.enums import (
        HeroGamemode,
        MapKey,
        Role,
    )

# The slices of the /heroes/stats cross product recorded daily: PC, competitive,
# no role or map filter, crossed with region and competitive division — one
# unfiltered ("all") row per region plus one row per region per division, so
# 3 + 3x8 = 27 Blizzard requests and 27 rows a day.
#
# The unfiltered slice is "the meta as a whole", which is the question a winrate
# series answers by default. Per-division history earns its own request budget
# on top of that: rank tiers move differently (a hero can be dominant in Bronze
# and dead in Grandmaster), and per-rank meta trends are worth asking Blizzard
# for — 24 extra requests a day is well within the throttle that already carries
# the unfiltered three. Per-map or per-platform history is not: crossing those
# in too would mean the full cross product, thousands of requests a day and a
# table nobody could query usefully, so role, map and platform stay out on
# purpose. A knob for those here would be an invitation to switch the cross
# product on by accident.
HERO_STATS_SNAPSHOT_PLATFORM = PlayerPlatform.PC
HERO_STATS_SNAPSHOT_GAMEMODE = PlayerGamemode.COMPETITIVE
HERO_STATS_SNAPSHOT_SLICES = tuple(
    (HERO_STATS_SNAPSHOT_PLATFORM, HERO_STATS_SNAPSHOT_GAMEMODE, region, division)
    for region in PlayerRegion
    for division in (None, *CompetitiveDivisionFilter)
)


class HeroService(StaticDataService):
    """Domain service for hero data: list, detail, and usage statistics."""

    # ------------------------------------------------------------------
    # Heroes list  (GET /heroes)
    # ------------------------------------------------------------------

    def _heroes_list_config(
        self,
        locale: Locale,
        cache_key: str,
        role: Role | SubRole | None = None,
        gamemode: HeroGamemode | None = None,
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for the heroes list."""

        async def _fetch() -> str:
            return await fetch_heroes_html(self.blizzard_client, locale)

        def _parse(html: str) -> list[HeroListEntry]:
            try:
                return parse_heroes_html(html)
            except ParserParsingError as exc:
                blizzard_url = (
                    f"{settings.blizzard_host}/{locale}{settings.heroes_path}"
                )
                raise ParserInternalError(blizzard_url, exc) from exc

        return StaticFetchConfig(
            storage_key=f"heroes:{locale}",
            fetcher=_fetch,
            parser=_parse,
            result_filter=(
                (lambda data: filter_heroes(data, role, gamemode))
                if (role or gamemode)
                else None
            ),
            cache_key=cache_key,
            cache_ttl=settings.heroes_path_cache_timeout,
            staleness_threshold=settings.heroes_staleness_threshold,
            entity_type="heroes",
        )

    async def list_heroes(
        self,
        locale: Locale,
        role: Role | SubRole | None,
        gamemode: HeroGamemode | None,
        cache_key: str,
    ) -> SwrResult[list[HeroListEntry]]:
        """Return the heroes list (with optional role/gamemode filters).

        Stores raw Blizzard HTML per locale in persistent storage so that
        code changes to the parser take effect on the next request after restart.
        """
        return await self.get_or_fetch(
            self._heroes_list_config(locale, cache_key, role, gamemode)
        )

    async def refresh_list(self, locale: Locale) -> None:
        """Fetch fresh heroes list, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer so that
        fresh data is always fetched from Blizzard regardless of stored age.
        """
        locale_str = locale.value
        cache_key = (
            f"/heroes?locale={locale_str}" if locale != Locale.ENGLISH_US else "/heroes"
        )
        await self._fetch_and_store(self._heroes_list_config(locale, cache_key))
        await self._invalidate_derived_cache("/heroes", keep=cache_key)

    # ------------------------------------------------------------------
    # Single hero  (GET /heroes/{hero_key})
    # ------------------------------------------------------------------

    def _hero_detail_config(
        self, hero_key: str, locale: Locale, cache_key: str
    ) -> StaticFetchConfig:
        """Build a StaticFetchConfig for a single hero detail."""

        async def _fetch() -> str:
            hero_html = await fetch_hero_html(self.blizzard_client, hero_key, locale)
            # Validate hero exists before making the second Blizzard request.
            # parse_hero_html raises ParserBlizzardError (404) for unknown heroes,
            # which propagates to the API layer's registered OverfastError handler.
            parse_hero_html(hero_html, locale)
            heroes_html = await fetch_heroes_html(self.blizzard_client, locale)
            return json.dumps(
                {"hero_html": hero_html, "heroes_html": heroes_html},
                separators=(",", ":"),
            )

        def _parse(raw: str) -> HeroDetail:
            sources = json.loads(raw)
            try:
                hero_data = parse_hero_html(sources["hero_html"], locale)
                heroes_list = parse_heroes_html(sources["heroes_html"])
                heroes_hitpoints = parse_heroes_hitpoints()
                return _merge_hero_data(
                    hero_data, heroes_list, heroes_hitpoints, hero_key
                )
            except ParserParsingError as exc:
                blizzard_url = f"{settings.blizzard_host}/{locale}{settings.heroes_path}{hero_key}/"
                raise ParserInternalError(blizzard_url, exc) from exc

        return StaticFetchConfig(
            storage_key=f"hero:{hero_key}:{locale}",
            fetcher=_fetch,
            parser=_parse,
            cache_key=cache_key,
            cache_ttl=settings.hero_path_cache_timeout,
            staleness_threshold=settings.heroes_staleness_threshold,
            entity_type="hero",
        )

    async def get_hero(
        self,
        hero_key: str,
        locale: Locale,
        cache_key: str,
    ) -> SwrResult[HeroDetail]:
        """Return full hero details merged with portrait and hitpoints.

        Stores a JSON-encoded dict of raw HTML sources per ``hero_key:locale``
        in persistent storage so that code changes to the parser take effect
        on the next request after restart.
        """
        return await self.get_or_fetch(
            self._hero_detail_config(hero_key, locale, cache_key)
        )

    async def refresh_single(self, hero_key: str, locale: Locale) -> None:
        """Fetch fresh hero detail, persist to storage and update API cache.

        Called by the background worker — bypasses the SWR layer.
        """
        locale_str = locale.value
        cache_key = (
            f"/heroes/{hero_key}?locale={locale_str}"
            if locale != Locale.ENGLISH_US
            else f"/heroes/{hero_key}"
        )
        await self._fetch_and_store(
            self._hero_detail_config(hero_key, locale, cache_key)
        )
        await self._invalidate_derived_cache(f"/heroes/{hero_key}", keep=cache_key)

    # ------------------------------------------------------------------
    # Hero stats summary  (GET /heroes/stats)
    # ------------------------------------------------------------------

    async def get_hero_stats(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        region: PlayerRegion,
        role: Role | SubRole | None,
        map_filter: MapKey | None,
        competitive_division: CompetitiveDivisionFilter | None,
        order_by: str,
        cache_key: str,
    ) -> SwrResult[list[dict]]:
        """Return hero usage statistics — Valkey-only cache, no persistent storage.

        Stats change frequently and have too many parameter combinations to
        store in persistent storage. The Valkey API cache (populated here, served by nginx)
        is sufficient.
        """
        data = await self._fetch_hero_stats(
            platform,
            gamemode,
            region,
            role,
            map_filter,
            competitive_division,
            order_by,
        )
        await self._update_api_cache(
            cache_key,
            data,
            settings.hero_stats_cache_timeout,
        )
        return SwrResult(data, False, 0)

    async def _fetch_hero_stats(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        region: PlayerRegion,
        role: Role | SubRole | None,
        map_filter: MapKey | None,
        competitive_division: CompetitiveDivisionFilter | None,
        order_by: str,
    ) -> list[dict]:
        """Fetch hero stats from Blizzard, trying each gamemode filter candidate.

        Shared by the endpoint and the daily snapshot job so both benefit from
        the filter fallback and both refresh the cached working filter.
        """
        for gamemode_filter in await self._get_hero_stats_gamemode_filters(gamemode):
            try:
                data = await self._get_hero_stats(
                    platform,
                    gamemode,
                    gamemode_filter,
                    region,
                    role,
                    map_filter,
                    competitive_division,
                    order_by,
                )
                working_filter = gamemode_filter
                break  # filter worked — stop retrying (data may legitimately be empty)
            except InvalidGamemodeFilterError as exc:
                # Blizzard may have changed the filter value; try the next candidate.
                gamemode_filter_exception = exc
        else:
            # All filter candidates exhausted without a successful call.
            blizzard_url = f"{settings.blizzard_host}{settings.hero_stats_path}"
            raise ParserInternalError(
                blizzard_url, gamemode_filter_exception
            ) from gamemode_filter_exception

        await self.cache.set_gamemode_filter(gamemode, working_filter)
        return data

    # ------------------------------------------------------------------
    # Hero stats history  (GET /heroes/stats/history + daily snapshot job)
    # ------------------------------------------------------------------

    async def record_hero_stats_snapshots(self) -> int:
        """Record today's reading of every canonical slice. Returns rows written.

        Requests are sequential on purpose: they queue behind the same Blizzard
        throttle either way, and firing them all at once only makes the throttle
        back off. A slice that fails is logged and skipped — a partial day is
        worth more than no day, and nothing here may abort the other slices.
        """
        taken_on = datetime.now(tz=UTC).date()
        recorded = 0

        for platform, gamemode, region, division in HERO_STATS_SNAPSHOT_SLICES:
            division_str = "all" if division is None else str(division)
            try:
                stats = await self._fetch_hero_stats(
                    platform, gamemode, region, None, None, division, "hero:asc"
                )
                await self.storage.add_hero_stats_snapshot(
                    taken_on,
                    str(platform),
                    str(gamemode),
                    str(region),
                    build_hero_stats_snapshot(stats),
                    division=division_str,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[hero stats history] Failed to record {} {} : {}",
                    region,
                    division_str,
                    exc,
                )
                continue

            recorded += 1

        logger.info(
            "[hero stats history] Recorded {}/{} slices for {}",
            recorded,
            len(HERO_STATS_SNAPSHOT_SLICES),
            taken_on,
        )
        return recorded

    async def get_hero_stats_history(
        self,
        region: PlayerRegion,
        cache_key: str,
        hero: str | None = None,
        since: int | None = None,
        limit: int = 30,
        division: CompetitiveDivisionFilter | None = None,
    ) -> SwrResult[dict]:
        """Return the recorded hero stats series for one region, newest first.

        Only platform PC and gamemode competitive are recorded, so they are
        fixed here rather than exposed as filters. ``division`` selects the
        per-division series instead of the unfiltered ("all") one; omitted, it
        reads "all", which is the only series recorded before per-division
        history was added. Filtering on ``hero`` drops the days that hero was
        not recorded on, so a client charting one hero gets points rather than
        gaps.
        """
        division_str = "all" if division is None else str(division)
        snapshots = await self.storage.get_hero_stats_snapshots(
            str(HERO_STATS_SNAPSHOT_PLATFORM),
            str(HERO_STATS_SNAPSHOT_GAMEMODE),
            str(region),
            since=since,
            limit=limit,
            division=division_str,
        )

        series = []
        for snapshot in snapshots:
            stats = (
                snapshot["data"]
                if hero is None
                else [row for row in snapshot["data"] if row["hero"] == hero]
            )
            if stats:
                series.append(
                    {"taken_on": snapshot["taken_on"].isoformat(), "stats": stats}
                )

        data = {
            "region": str(region),
            "competitive_division": None if division is None else str(division),
            "snapshots": series,
        }
        await self._update_api_cache(
            cache_key,
            data,
            settings.hero_stats_cache_timeout,
        )
        return SwrResult(data, False, 0)

    async def _get_hero_stats_gamemode_filters(
        self, gamemode: PlayerGamemode
    ) -> list[str]:
        """Return the ordered candidate filter values to try for a given gamemode.

        The cached working filter (if any) is moved to the front so the correct
        value is tried first, avoiding a redundant Blizzard call on every request.

        Args:
            gamemode: Gamemode for validation

        Returns:
            Filter values ordered with the cached working filter first

        Raises:
            ParserParsingError: If gamemode is not supported
        """
        gamemode_mapping: dict[PlayerGamemode, list[str]] = {
            PlayerGamemode.QUICKPLAY: ["0"],
            PlayerGamemode.COMPETITIVE: ["1", "2"],
        }

        if gamemode not in gamemode_mapping:
            msg = f"{gamemode} is not a supported gamemode filter"
            raise ParserParsingError(msg)

        candidates = gamemode_mapping[gamemode]

        cached_filter = await self.cache.get_gamemode_filter(gamemode)
        if cached_filter and cached_filter in candidates:
            return [cached_filter] + [f for f in candidates if f != cached_filter]

        return candidates

    async def _get_hero_stats(
        self,
        platform: PlayerPlatform,
        gamemode: PlayerGamemode,
        gamemode_filter: str,
        region: PlayerRegion,
        role: Role | SubRole | None,
        map_filter: MapKey | None,
        competitive_division: CompetitiveDivisionFilter | None,
        order_by: str,
    ) -> list[dict]:
        try:
            data = await parse_hero_stats_summary(
                self.blizzard_client,
                platform=platform,
                gamemode=gamemode,
                gamemode_filter=gamemode_filter,
                region=region,
                role=role,
                map_filter=map_filter,
                competitive_division=competitive_division,
                order_by=order_by,
            )
        except ParserParsingError as exc:
            blizzard_url = f"{settings.blizzard_host}{settings.hero_stats_path}"
            raise ParserInternalError(blizzard_url, exc) from exc

        return data


# ---------------------------------------------------------------------------
# Module-level helpers (kept accessible for tests)
# ---------------------------------------------------------------------------


def _merge_hero_data(
    hero_data: HeroDetail,
    heroes_list: list[HeroListEntry],
    heroes_hitpoints: dict,
    hero_key: str,
) -> HeroDetail:
    """Merge data from hero details, heroes list, and heroes hitpoints."""
    # dict_insert_value_before_key works positionally on any dict and returns
    # a plain dict — a TypedDict is a plain dict at runtime, so `working`
    # carries the same object/shape as hero_data throughout, just without a
    # key-by-key-checked type. cast() below is a pure typing shim (see
    # app/domain/models/hero.py for why portrait/hitpoints are NotRequired).
    working: dict = cast("dict", hero_data)
    try:
        portrait_value = next(
            hero["portrait"] for hero in heroes_list if hero["key"] == hero_key
        )
    except StopIteration:
        portrait_value = None
    else:
        working = dict_insert_value_before_key(
            working, "role", "portrait", portrait_value
        )

    if hero_key in heroes_hitpoints:
        for mode_key in ("hitpoints", "hitpoints_6v6"):
            working = dict_insert_value_before_key(
                working, "abilities", mode_key, heroes_hitpoints[hero_key][mode_key]
            )

    return cast("HeroDetail", working)


def dict_insert_value_before_key(
    data: dict,
    key: str,
    new_key: str,
    new_value: Any,
) -> dict:
    """Insert ``new_key: new_value`` before ``key`` in ``data``."""
    if key not in data:
        raise KeyError
    pos = list(data.keys()).index(key)
    items = list(data.items())
    items.insert(pos, (new_key, new_value))
    return dict(items)
