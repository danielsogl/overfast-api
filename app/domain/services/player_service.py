"""Player domain service — career, stats, summary, and search"""

import asyncio
import time
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import TYPE_CHECKING, Never, cast

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

from app.config import settings
from app.domain.enums import HeroKeyCareerFilter, PlayerGamemode, PlayerPlatform
from app.domain.exceptions import (
    ParserBlizzardError,
    ParserInternalError,
    ParserParsingError,
)
from app.domain.models.player import PlayerIdentity
from app.domain.parsers.player_career_stats import process_career_stats
from app.domain.parsers.player_profile import (
    extract_name_from_profile_html,
    fetch_player_html,
    filter_all_stats_data,
    filter_stats_by_query,
    parse_player_profile_html,
)
from app.domain.parsers.player_search import parse_player_search
from app.domain.parsers.player_snapshot import (
    build_player_snapshot,
    diff_player_snapshots,
)
from app.domain.parsers.player_stats import process_player_stats_summary
from app.domain.parsers.player_summary import (
    fetch_player_summary_json,
    parse_player_summary_json,
)
from app.domain.parsers.utils import is_blizzard_id
from app.domain.services.base_service import BaseService
from app.infrastructure.logger import logger

# Parsing one stored profile costs 10-20ms of blocking CPU (roughly half lexbor
# DOM build, half tree walk), and all five player endpoints funnel through the
# same parse of the same HTML. The API cache in Valkey expires after
# ``career_path_cache_timeout`` (600s) while the stored profile stays valid for
# ``player_staleness_threshold`` (3600s), so without this every endpoint reparses
# the identical blob roughly six times per stored profile.
#
# Keying on ``updated_at`` is what makes it safe: a background refresh writes a
# new timestamp and misses the cache, and a restart starts empty so parser
# changes take effect immediately — the same property ``_serve_from_storage``
# relies on for static data.
#
# ponytail: process-local OrderedDict, not Valkey. There is one app process and
# no `await` in the lookup, so it needs no lock. Move it to Valkey only if the
# app is ever scaled past one process AND the hit rate proves worth a round-trip.
_PARSED_PROFILE_CACHE: OrderedDict[tuple[str, int], dict] = OrderedDict()
_PARSED_PROFILE_CACHE_MAXSIZE = 16


def parse_stored_profile(
    player_id: str,
    updated_at: int,
    html: str,
    player_summary: dict,
) -> dict:
    """Parse a stored profile, reusing the result across endpoints.

    The returned dict is **shared between callers — treat it as read-only.**
    Three consumers pass nested parts of it straight through to the response
    (``summary``, per-platform stats, per-hero career stat lists), so mutating
    it in place would corrupt every later cache hit.
    """
    key = (player_id, updated_at)
    if (cached := _PARSED_PROFILE_CACHE.get(key)) is not None:
        _PARSED_PROFILE_CACHE.move_to_end(key)
        return cached

    parsed = parse_player_profile_html(html, player_summary)
    _PARSED_PROFILE_CACHE[key] = parsed
    if len(_PARSED_PROFILE_CACHE) > _PARSED_PROFILE_CACHE_MAXSIZE:
        _PARSED_PROFILE_CACHE.popitem(last=False)
    return parsed


def clear_parsed_profile_cache() -> None:
    """Drop all memoised profiles (tests)."""
    _PARSED_PROFILE_CACHE.clear()


# A cold player costs at least two Blizzard requests (identity search, then the
# profile page), and they all queue behind the same throttle. Without this, ten
# concurrent requests for one uncached player fire twenty, each slower than the
# last as the throttle paces them — and nineteen are redundant, because the
# first one stores the profile every later request could have read.
_INFLIGHT_LOCKS: dict[str, asyncio.Lock] = {}
_INFLIGHT_WAITERS: Counter[str] = Counter()


@asynccontextmanager
async def single_flight(key: str) -> AsyncIterator[None]:
    """Serialise concurrent work for *key*, dropping the lock when nobody waits.

    Callers must re-check their data source after acquiring: the point is that
    the holder ahead of them has usually already produced it.
    """
    lock = _INFLIGHT_LOCKS.setdefault(key, asyncio.Lock())
    _INFLIGHT_WAITERS[key] += 1
    try:
        async with lock:
            yield
    finally:
        _INFLIGHT_WAITERS[key] -= 1
        if _INFLIGHT_WAITERS[key] <= 0:
            del _INFLIGHT_WAITERS[key]
            _INFLIGHT_LOCKS.pop(key, None)


def clear_inflight_locks() -> None:
    """Drop all in-flight locks (tests)."""
    _INFLIGHT_LOCKS.clear()
    _INFLIGHT_WAITERS.clear()


# Default lookback for /stats/diff: "what changed since yesterday" is the
# question the endpoint exists to answer.
_DEFAULT_DIFF_WINDOW = 86400

# A window's worth of snapshots is read to find its oldest entry, but only the
# two ends are compared. 500 versions inside one window would mean Blizzard
# republished the profile every few minutes for the whole period; capping here
# keeps one query bounded instead of adding an ascending-order variant to the
# port for a case that does not occur.
_DIFF_SNAPSHOT_LIMIT = 500


class PlayerService(BaseService):
    """Domain service for all player-related endpoints.

    Wraps identity resolution, persistent storage profile caching, and SWR staleness logic
    that was previously scattered across multiple controllers.
    """

    # ------------------------------------------------------------------
    # Search  (Valkey-only, no persistent storage, no SWR)
    # ------------------------------------------------------------------

    async def search_players(
        self,
        name: str,
        order_by: str,
        offset: int,
        limit: int,
        cache_key: str,
    ) -> dict:
        """Search for players by name — Valkey-only cache, no persistent storage."""
        try:
            data = await parse_player_search(
                self.blizzard_client,
                name=name,
                order_by=order_by,
                offset=offset,
                limit=limit,
            )
        except ParserParsingError as exc:
            search_name = name.split("-", 1)[0]
            blizzard_url = (
                f"{settings.blizzard_host}{settings.search_account_path}/{search_name}/"
            )
            raise ParserInternalError(blizzard_url, exc) from exc

        await self._update_api_cache(
            cache_key, data, settings.search_account_path_cache_timeout
        )
        return data

    # ------------------------------------------------------------------
    # Player summary  (GET /players/{player_id}/summary)
    # ------------------------------------------------------------------

    async def get_player_summary(
        self,
        player_id: str,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return player summary (name, avatar, competitive ranks, …)."""

        def extract(profile: dict) -> dict:
            return profile.get("summary") or {}

        return await self._execute_player_request(player_id, cache_key, extract)

    # ------------------------------------------------------------------
    # Player career  (GET /players/{player_id})
    # ------------------------------------------------------------------

    async def get_player_career(
        self,
        player_id: str,
        gamemode: PlayerGamemode | None,
        platform: PlayerPlatform | None,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return full player data: summary + stats."""

        def extract(profile: dict) -> dict:
            return {
                "summary": profile.get("summary") or {},
                "stats": filter_all_stats_data(
                    profile.get("stats") or {}, platform, gamemode
                ),
            }

        return await self._execute_player_request(player_id, cache_key, extract)

    # ------------------------------------------------------------------
    # Background refresh  (worker only — bypasses storage fast-path)
    # ------------------------------------------------------------------

    async def refresh_player_profile(self, player_id: str) -> None:
        """Unconditionally fetch fresh player data from Blizzard and persist it.

        Unlike the public endpoint methods, this method bypasses
        ``_get_fresh_stored_profile`` entirely, so the worker always
        issues a live Blizzard request regardless of how recently the
        profile was last stored.  This prevents the background refresh
        task from silently no-oping when the stored profile is still
        within the staleness threshold.

        After updating persistent storage, all existing API cache keys for this
        player are deleted.  The next request will find a cache miss, hit the
        storage fast-path (profile is now fresh), compute the correct data slice,
        and repopulate the cache — without touching Blizzard.
        """
        identity = PlayerIdentity()
        try:
            identity = await self._resolve_player_identity(player_id)
            effective_id = identity.blizzard_id or player_id
            html = await self._get_player_html(
                effective_id, identity, force_update=True
            )
            # The worker is the only writer for a player nobody is requesting
            # right now, so without parsing here their series would simply stop.
            # The parse is guarded separately: a markup break must cost the
            # snapshot, not the refresh that already succeeded.
            try:
                parsed = parse_player_profile_html(html, identity.player_summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[history] Could not parse profile of {} for a snapshot: {}",
                    player_id,
                    exc,
                )
            else:
                await self._store_snapshot(effective_id, parsed)
            await self._evict_player_cache_keys(player_id)
        except Exception as exc:  # noqa: BLE001
            await self._handle_player_exceptions(exc, player_id, identity)

    async def _evict_player_cache_keys(self, player_id: str) -> None:
        """Delete all API cache keys for *player_id* from Valkey.

        Uses a glob scan so every endpoint/parameter combination is cleared
        without needing to enumerate them explicitly.  The next request for
        each key will hit the storage fast-path and repopulate the cache.
        """
        pattern = f"{settings.api_cache_key_prefix}:/players/{player_id}*"
        keys = await self.cache.scan_keys(pattern)
        if keys:
            # One round-trip, not one per key: this runs after every completed
            # player refresh, and a busy profile has a key per endpoint and
            # query-parameter combination.
            await self.cache.delete(*keys)
            logger.debug(
                "[refresh] Evicted {} cache key(s) for {}", len(keys), player_id
            )

    # ------------------------------------------------------------------
    # Player stats  (GET /players/{player_id}/stats)
    # ------------------------------------------------------------------

    async def get_player_stats(
        self,
        player_id: str,
        gamemode: PlayerGamemode,
        platform: PlayerPlatform | None,
        hero: HeroKeyCareerFilter | None,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return player stats with category labels."""

        def extract(profile: dict) -> dict:
            return filter_stats_by_query(
                profile.get("stats") or {}, gamemode, platform, hero
            )

        return await self._execute_player_request(player_id, cache_key, extract)

    # ------------------------------------------------------------------
    # Player stats summary  (GET /players/{player_id}/stats/summary)
    # ------------------------------------------------------------------

    async def get_player_stats_summary(
        self,
        player_id: str,
        gamemode: PlayerGamemode | None,
        platform: PlayerPlatform | None,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return player statistics summary (winrate, kda, …)."""

        def extract(profile: dict) -> dict:
            return process_player_stats_summary(profile, gamemode, platform)

        return await self._execute_player_request(player_id, cache_key, extract)

    # ------------------------------------------------------------------
    # Player career stats  (GET /players/{player_id}/stats/career)
    # ------------------------------------------------------------------

    async def get_player_career_stats(
        self,
        player_id: str,
        gamemode: PlayerGamemode,
        platform: PlayerPlatform | None,
        hero: HeroKeyCareerFilter | None,
        cache_key: str,
    ) -> tuple[dict, bool, int]:
        """Return player career stats (no labels)."""

        def extract(profile: dict) -> dict:
            return process_career_stats(profile, gamemode, platform, hero)

        return await self._execute_player_request(player_id, cache_key, extract)

    # ------------------------------------------------------------------
    # Snapshot history  (GET /players/{player_id}/history)
    # ------------------------------------------------------------------

    async def get_player_history(
        self,
        player_id: str,
        cache_key: str,
        since: int | None = None,
        limit: int = 100,
    ) -> tuple[dict, bool, int]:
        """Return the player's stored snapshot series, newest first.

        The live profile is requested first so that "now" is always the head of
        the series and the profile stays warm — otherwise a client polling only
        this endpoint would watch its own history go stale.
        """
        is_stale, age = await self._warm_player_profile(player_id)
        snapshots = await self.storage.get_player_snapshots(
            await self._canonical_player_id(player_id), since=since, limit=limit
        )

        data = {"snapshots": snapshots}
        await self._update_api_cache(
            cache_key, data, settings.career_path_cache_timeout
        )
        return data, is_stale, age

    # ------------------------------------------------------------------
    # Snapshot diff  (GET /players/{player_id}/stats/diff)
    # ------------------------------------------------------------------

    async def get_player_stats_diff(
        self,
        player_id: str,
        cache_key: str,
        since: int | None = None,
    ) -> tuple[dict, bool, int]:
        """Compare the oldest snapshot at/after *since* against the newest.

        ``since`` defaults to 24 hours ago. A player with no history yet gets an
        empty diff rather than a 404 — "nothing recorded" is a state to render,
        not an error.
        """
        is_stale, age = await self._warm_player_profile(player_id)
        if since is None:
            since = int(time.time()) - _DEFAULT_DIFF_WINDOW
        snapshots = await self.storage.get_player_snapshots(
            await self._canonical_player_id(player_id),
            since=since,
            limit=_DIFF_SNAPSHOT_LIMIT,
        )

        data = {"since": since, **diff_player_snapshots(snapshots)}
        await self._update_api_cache(
            cache_key, data, settings.career_path_cache_timeout
        )
        return data, is_stale, age

    async def _warm_player_profile(self, player_id: str) -> tuple[bool, int]:
        """Run the normal player request for its side effects only.

        Returns the SWR ``(is_stale, age)`` pair so the history endpoints report
        the same freshness as their siblings. ``cache_key=None`` keeps the
        scaffold from writing this discarded payload over the caller's key.
        """
        _, is_stale, age = await self._execute_player_request(
            player_id, None, lambda _parsed: {}
        )
        return is_stale, age

    async def _canonical_player_id(self, player_id: str) -> str:
        """Resolve *player_id* to the id the snapshot series is keyed on.

        A player can be requested as a BattleTag or as a Blizzard ID, and on the
        storage fast path the BattleTag is never resolved — so without this both
        spellings would grow their own half of the same history. The Blizzard ID
        wins because it is the only stable one: a BattleTag can be changed.
        """
        if is_blizzard_id(player_id):
            return player_id
        return await self.storage.get_player_id_by_battletag(player_id) or player_id

    async def _store_snapshot(self, player_id: str, parsed: dict) -> None:
        """Record one point of the player's history, if it is a new version.

        Never raises. History is a by-product of serving a profile we fetched
        anyway; a storage hiccup must cost the row, not the response.
        """
        try:
            snapshot = build_player_snapshot(parsed)
            if snapshot is None:
                return

            summary = parsed.get("summary") or {}
            # Blizzard's own version stamp — the series key, and what makes the
            # insert idempotent across the many endpoints serving one profile.
            last_updated = summary.get("last_updated_at")
            if not last_updated:
                return

            await self.storage.add_player_snapshot(
                await self._canonical_player_id(player_id),
                int(last_updated),
                snapshot,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[history] Failed to store snapshot for {}: {}", player_id, exc
            )

    # ------------------------------------------------------------------
    # Core request execution — universal scaffold
    # ------------------------------------------------------------------

    async def _execute_player_request(
        self,
        player_id: str,
        cache_key: str | None,
        data_factory: Callable[[dict], dict],
    ) -> tuple[dict, bool, int]:
        """Resolve identity → get HTML → parse → compute data → update cache → return.

        Fast path: if persistent storage has a profile fresher than
        ``player_staleness_threshold``, all Blizzard calls are skipped and
        the cached HTML + summary are used directly.
        """
        identity = PlayerIdentity()
        effective_id = player_id
        data: dict = {}
        age: int = 0
        stored_at: int | None = None

        try:
            profile, age = await self._get_fresh_stored_profile(player_id)

            if profile is not None:
                stored_at = profile["updated_at"]
                logger.info(
                    "Serving player data from persistent storage (within staleness threshold)"
                )
                parsed = self._parse_stored(player_id, profile)
            else:
                # Single-flight: the lock is held across the Blizzard round-trip
                # on purpose. Waiters would otherwise queue behind the throttle
                # anyway, and this way they get the stored profile instead of
                # firing their own redundant fetch.
                async with single_flight(player_id):
                    profile, age = await self._get_fresh_stored_profile(player_id)
                    if profile is not None:
                        stored_at = profile["updated_at"]
                        logger.info(
                            "Profile for {} was fetched by a concurrent request "
                            "while waiting — skipping Blizzard",
                            player_id,
                        )
                        parsed = self._parse_stored(player_id, profile)
                    else:
                        identity = await self._resolve_player_identity(player_id)
                        effective_id = identity.blizzard_id or player_id
                        html = await self._get_player_html(effective_id, identity)
                        # ponytail: not memoised — a cold fetch parses once
                        # anyway, and the next endpoint reads the profile back
                        # from storage with the authoritative ``updated_at``.
                        parsed = parse_player_profile_html(
                            html, identity.player_summary
                        )
                        # The data is now as fresh as it gets. _get_fresh_stored
                        # _profile reports the *stored* age even when it hands
                        # back None, so leaving it meant _check_player_staleness
                        # still saw the pre-fetch age, marked this response
                        # stale, and had the caller enqueue a background refresh
                        # that fetched the very page just fetched — two Blizzard
                        # round-trips behind the throttle instead of one.
                        age = 0

            # Both branches above converge here, and the insert is keyed on the
            # profile version, so a storage hit re-records nothing while a fresh
            # fetch appends exactly one row.
            await self._store_snapshot(player_id, parsed)

            data = data_factory(parsed)

        except Exception as exc:  # noqa: BLE001
            await self._handle_player_exceptions(exc, player_id, identity)

        is_stale = self._check_player_staleness(age)
        if cache_key is not None:
            await self._update_api_cache(
                cache_key,
                data,
                settings.career_path_cache_timeout,
                stored_at=stored_at,
                staleness_threshold=settings.player_staleness_threshold,
                stale_while_revalidate=settings.stale_cache_timeout if is_stale else 0,
            )
        if is_stale:
            await self._enqueue_refresh("player_profile", player_id)
        return data, is_stale, age

    # ------------------------------------------------------------------
    # Profile caching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_stored(player_id: str, profile: dict) -> dict:
        """Parse a storage hit through the shared parsed-profile cache."""
        return parse_stored_profile(
            player_id,
            profile["updated_at"],
            profile["profile"],
            profile["summary"],
        )

    async def get_player_profile_cache(self, player_id: str) -> dict | None:
        """Get player profile from persistent storage."""
        profile = await self.storage.get_player_profile(player_id)
        if not profile:
            return None

        return {
            "profile": profile["html"],
            "summary": profile["summary"],
            "battletag": profile.get("battletag"),
            "name": profile.get("name"),
            "updated_at": profile.get("updated_at", 0),
        }

    async def update_player_profile_cache(
        self,
        player_id: str,
        player_summary: dict,
        html: str,
        battletag: str | None = None,
        name: str | None = None,
    ) -> None:
        """Store player profile in persistent storage."""
        await self.storage.set_player_profile(
            player_id=player_id,
            html=html,
            summary=player_summary or None,
            battletag=battletag,
            name=name,
        )

    def _check_player_staleness(self, age: int) -> bool:
        """Return True when the stored profile is old enough to warrant a background pre-refresh.

        Applies SWR semantics: if the profile was served from persistent storage
        (``age > 0``) and has consumed at least half its staleness window, the response
        is marked stale so the caller enqueues a background refresh.  This keeps profiles
        pre-warmed and avoids a synchronous Blizzard call on the next request.

        ``age == 0`` means we just fetched fresh data from Blizzard — never stale.

        The SWR lifecycle for a player profile across the two threshold values:

        - ``0 ≤ age < threshold // 2``  — fresh; served from storage, no refresh enqueued.
        - ``threshold // 2 ≤ age < threshold``  — stale window: served from storage *and*
          a background refresh is enqueued so the next request finds a warm profile.
        - ``age ≥ threshold``  — ``_get_fresh_stored_profile`` returns ``None``; the
          request falls through to a synchronous Blizzard fetch.
        """
        if age == 0:
            return False
        swr_threshold = settings.player_staleness_threshold // 2
        return age >= swr_threshold

    async def _get_fresh_stored_profile(
        self, player_id: str
    ) -> tuple[dict | None, int]:
        """Return ``(profile, age_seconds)`` if the stored profile was updated within
        ``player_staleness_threshold``, else ``(None, 0)``.

        For BattleTag inputs, resolves to a Blizzard ID via the stored mapping
        before fetching the profile.  Returns ``None`` if no mapping exists or if the
        profile is absent. Returns tuple with ``(None, age)`` if the profile exists
        but is older than the threshold.

        See ``_check_player_staleness`` for the full SWR lifecycle description.
        """
        if is_blizzard_id(player_id):
            blizzard_id = player_id
        else:
            blizzard_id = await self.storage.get_player_id_by_battletag(player_id)
            if not blizzard_id:
                return None, 0

        profile = await self.get_player_profile_cache(blizzard_id)
        if not profile:
            return None, 0

        age = int(time.time()) - profile["updated_at"]
        if age < settings.player_staleness_threshold:
            logger.info(
                "Stored profile for {} is {:.0f}s old (threshold {}s) — skipping Blizzard",
                player_id,
                age,
                settings.player_staleness_threshold,
            )
            return profile, age

        return None, age

    async def _get_player_html(
        self,
        effective_id: str,
        identity: PlayerIdentity,
        *,
        force_update: bool = False,
    ) -> str:
        """Return player HTML, always storing fresh HTML in persistent storage.

        Priority order:
        1. ``identity.cached_html`` — fetched during identity resolution; store and return.
        2. persistent storage hit with matching ``lastUpdated`` — the profile hasn't changed
           on Blizzard's side, so there is no need to re-fetch the HTML page.  When
           ``force_update=True`` (background worker), ``update_player_profile_cache`` is
           called with the existing HTML to bump ``updated_at`` and reset the staleness clock.
           Battletag is backfilled in either case when it was previously missing.
        3. Fetch from Blizzard, store, return.
        """
        if identity.cached_html:
            name = extract_name_from_profile_html(
                identity.cached_html
            ) or identity.player_summary.get("name")
            await self.update_player_profile_cache(
                effective_id,
                identity.player_summary,
                identity.cached_html,
                identity.battletag_input,
                name,
            )
            return identity.cached_html

        player_cache = await self.get_player_profile_cache(effective_id)
        if (
            player_cache is not None
            and identity.player_summary
            and player_cache["summary"].get("lastUpdated")
            == identity.player_summary.get("lastUpdated")
        ):
            html = cast("str", player_cache["profile"])
            if force_update or (
                identity.battletag_input and not player_cache.get("battletag")
            ):
                await self.update_player_profile_cache(
                    effective_id,
                    identity.player_summary,
                    html,
                    identity.battletag_input,
                    player_cache.get("name"),
                )
            return html

        html, _ = await fetch_player_html(self.blizzard_client, effective_id)
        name = extract_name_from_profile_html(html) or identity.player_summary.get(
            "name"
        )
        await self.update_player_profile_cache(
            effective_id,
            identity.player_summary,
            html,
            identity.battletag_input,
            name,
        )
        return html

    # ------------------------------------------------------------------
    # Identity resolution
    # ------------------------------------------------------------------

    async def _resolve_player_identity(self, player_id: str) -> PlayerIdentity:
        """Resolve BattleTag or Blizzard ID to a canonical ``PlayerIdentity``."""
        logger.info("Retrieving Player Summary...")
        if is_blizzard_id(player_id):
            return await self._resolve_blizzard_id_identity(player_id)
        return await self._resolve_battletag_identity(player_id)

    async def _resolve_blizzard_id_identity(self, player_id: str) -> PlayerIdentity:
        """Resolve a raw Blizzard ID via reverse enrichment."""
        logger.info("Player ID is a Blizzard ID — attempting reverse enrichment")
        player_summary, html = await self._enrich_from_blizzard_id(player_id)
        return PlayerIdentity(
            blizzard_id=player_id,
            player_summary=player_summary,
            cached_html=html,
        )

    async def _resolve_battletag_identity(self, player_id: str) -> PlayerIdentity:
        """Resolve a BattleTag to a ``PlayerIdentity`` using search + fallbacks."""
        battletag_input = player_id
        search_json = await fetch_player_summary_json(self.blizzard_client, player_id)
        player_summary = parse_player_summary_json(search_json, player_id)

        if player_summary:
            logger.info("Player Summary retrieved!")
            return PlayerIdentity(
                blizzard_id=player_summary.get("url"),
                player_summary=player_summary,
                battletag_input=battletag_input,
            )

        if search_json:
            identity = await self._try_cached_blizzard_id(
                battletag_input, player_id, search_json
            )
            if identity:
                return identity

        return await self._resolve_via_redirect(player_id, battletag_input, search_json)

    async def _try_cached_blizzard_id(
        self, battletag_input: str, player_id: str, search_json: list
    ) -> PlayerIdentity | None:
        """Check storage for a cached Blizzard ID and retry the search with it."""
        logger.info(
            "Player not found in search — checking persistent storage for cached Blizzard ID"
        )
        cached_blizzard_id = await self.storage.get_player_id_by_battletag(
            battletag_input
        )

        if not cached_blizzard_id:
            return None

        logger.info("Blizzard ID found — retrying to find in search")
        player_summary = parse_player_summary_json(
            search_json, player_id, cached_blizzard_id
        )
        if player_summary:
            return PlayerIdentity(
                blizzard_id=cached_blizzard_id,
                player_summary=player_summary,
                battletag_input=battletag_input,
            )
        return None

    async def _resolve_via_redirect(
        self, player_id: str, battletag_input: str, search_json: list
    ) -> PlayerIdentity:
        """Resolve identity as a last resort via Blizzard redirect HTML fetch."""
        logger.info("No cached mapping — resolving via Blizzard redirect")
        html, blizzard_id = await fetch_player_html(self.blizzard_client, player_id)

        if blizzard_id and search_json:
            player_summary = parse_player_summary_json(
                search_json, player_id, blizzard_id
            )
            if player_summary:
                return PlayerIdentity(
                    blizzard_id=blizzard_id,
                    player_summary=player_summary,
                    cached_html=html,
                    battletag_input=battletag_input,
                )

        return PlayerIdentity(
            blizzard_id=blizzard_id,
            cached_html=html,
            battletag_input=battletag_input,
        )

    async def _enrich_from_blizzard_id(
        self, blizzard_id: str
    ) -> tuple[dict, str | None]:
        """Reverse-enrich: fetch HTML → extract name → search for summary."""
        html, _ = await fetch_player_html(self.blizzard_client, blizzard_id)
        if not html:
            return {}, None

        try:
            player_name = extract_name_from_profile_html(html)
            if player_name:
                logger.debug("Player name {} found, fetching summary...", player_name)
                search_json = await fetch_player_summary_json(
                    self.blizzard_client, player_name
                )
                player_summary = parse_player_summary_json(
                    search_json, player_name, blizzard_id
                )
                if player_summary:
                    return player_summary, html
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reverse enrichment failed: {}", exc)

        return {}, html

    # ------------------------------------------------------------------
    # Unknown player tracking
    # ------------------------------------------------------------------

    def _calculate_retry_after(self, check_count: int) -> int:
        base = settings.unknown_player_initial_retry
        multiplier = settings.unknown_player_retry_multiplier
        max_retry = settings.unknown_player_max_retry
        retry_after = base * (multiplier ** (check_count - 1))
        return min(int(retry_after), max_retry)

    async def _mark_player_unknown(
        self,
        blizzard_id: str,
        exception: ParserBlizzardError,
        battletag: str | None = None,
    ) -> None:
        if not settings.unknown_players_cache_enabled:
            return
        if exception.status_code != HTTPStatus.NOT_FOUND.value:
            return

        player_status = await self.cache.get_player_status(blizzard_id)
        check_count = player_status["check_count"] + 1 if player_status else 1
        retry_after = self._calculate_retry_after(check_count)
        next_check_at = int(time.time()) + retry_after

        await self.cache.set_player_status(
            blizzard_id, check_count, retry_after, battletag=battletag
        )

        exception.message = {
            "error": "Player not found",
            "retry_after": retry_after,
            "next_check_at": next_check_at,
            "check_count": check_count,
        }

        logger.info(
            "Marked player {} as unknown (check #{}, retry in {}s)",
            blizzard_id,
            check_count,
            retry_after,
        )

    async def _handle_player_exceptions(
        self,
        error: Exception,
        player_id: str,
        identity: PlayerIdentity,
    ) -> Never:
        """Translate known parser exceptions to a client-facing error and always raise.

        Raised errors are ``ParserBlizzardError``/``ParserInternalError`` — the API
        layer's registered ``OverfastError`` handler turns them into HTTP responses.
        """
        effective_id = identity.blizzard_id or player_id
        battletag_input = identity.battletag_input
        player_summary = identity.player_summary

        if isinstance(error, ParserBlizzardError):
            if error.status_code == HTTPStatus.NOT_FOUND.value:
                await self._mark_player_unknown(
                    effective_id, error, battletag=battletag_input
                )
            raise error

        if isinstance(error, ParserParsingError):
            if "Could not find main content in HTML" in str(error):
                not_found = ParserBlizzardError(
                    status_code=HTTPStatus.NOT_FOUND.value,
                    message="Player not found",
                )
                await self._mark_player_unknown(
                    effective_id, not_found, battletag=battletag_input
                )
                raise not_found from error

            blizzard_url = (
                f"{settings.blizzard_host}{settings.career_path}/"
                f"{player_summary.get('url', effective_id) if player_summary else effective_id}/"
            )
            raise ParserInternalError(blizzard_url, error) from error

        raise error
