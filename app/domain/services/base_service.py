"""Domain service base classes.

``BaseService`` holds infrastructure adapters and low-level helpers shared by
*all* services (static + player).

``StaticDataService`` extends it with the generic Stale-While-Revalidate flow
for data that is stored as JSON in the ``static_data`` table and where
staleness is determined by a configurable time threshold.  All static-content
services (heroes, maps, gamemodes, roles) inherit from this class.

``PlayerService`` inherits directly from ``BaseService`` and implements its own
staleness strategy (Blizzard ``lastUpdated`` comparison) and storage logic
(``player_profiles`` table).
"""

from typing import TYPE_CHECKING, Any, NamedTuple

from app.config import settings
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.ports import (
        BlizzardClientPort,
        CachePort,
        StoragePort,
        TaskQueuePort,
    )


class SwrResult[T](NamedTuple):
    """What every SWR read hands back: the payload plus its freshness.

    A NamedTuple rather than a dataclass on purpose. Twenty-three call sites in
    the routers already unpack this positionally (`data, is_stale, age = ...`),
    and a NamedTuple leaves every one of them working untouched while giving the
    signatures names and giving `ty` something to check. A dataclass would have
    bought the same names for a diff across every router.

    ``age`` is seconds since the payload's data was stored, and it is what nginx
    turns into the ``Age`` header; ``is_stale`` says a background refresh was
    enqueued, not that the payload is unusable.
    """

    data: T
    is_stale: bool
    age: int


class BaseService:
    """Infrastructure holder shared by all domain services.

    Provides:
    - Adapter references (cache, storage, blizzard_client, task_queue)
    - ``_update_api_cache``: write to Valkey after serving data
    - ``_enqueue_refresh``: deduplicated background refresh scheduling
    """

    def __init__(
        self,
        cache: CachePort,
        storage: StoragePort,
        blizzard_client: BlizzardClientPort,
        task_queue: TaskQueuePort,
    ) -> None:
        self.cache = cache
        self.storage = storage
        self.blizzard_client = blizzard_client
        self.task_queue = task_queue

    async def _update_api_cache(
        self,
        cache_key: str,
        data: Any,
        cache_ttl: int,
        *,
        stored_at: int | None = None,
        staleness_threshold: int | None = None,
        stale_while_revalidate: int = 0,
    ) -> None:
        """Write data to Valkey API cache, swallowing errors."""
        try:
            await self.cache.update_api_cache(
                cache_key,
                data,
                cache_ttl,
                stored_at=stored_at,
                staleness_threshold=staleness_threshold,
                stale_while_revalidate=stale_while_revalidate,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SWR] Valkey write failed for {}: {}", cache_key, exc)

    async def _invalidate_derived_cache(self, path: str, keep: str) -> None:
        """Drop the API-cache entries derived from *path*, except *keep*.

        Refresh tasks write a single hardcoded key — ``/heroes``, ``/maps`` — but
        the request that enqueued the refresh may well have been
        ``/heroes?role=damage``. Refreshing only the bare key left every filtered
        and localised variant stale for its full 24h TTL: each request found it
        stale, enqueued a refresh, and that refresh once again updated a key the
        request had not asked for.

        Deleting the variants rather than refetching them is deliberate. The
        refresh has already put fresh data in storage, so the next request for a
        variant rebuilds it from there — no Blizzard call, no throttle.

        *keep* is the key this refresh just wrote, and it must survive: a
        localised refresh writes ``/heroes?locale=de-de``, which the pattern
        below matches and would otherwise delete immediately after writing it.

        ponytail: this also drops other locales' entries, which then rebuild
        from their own unchanged storage. Harmless and cheap at this keyspace
        size (~74 entries in production); scope the pattern per locale if that
        ever stops being true.
        """
        # "\?" — in a Valkey glob a bare "?" matches ANY single character, so
        # "/heroes?*" also matches "/heroes/ana" and would wipe every hero detail
        # entry, plus /heroes/stats, on a heroes-list refresh. Verified: the
        # unescaped pattern deleted 5 keys where 2 were meant.
        pattern = f"{settings.api_cache_key_prefix}:{path}\\?*"
        keep_key = f"{settings.api_cache_key_prefix}:{keep}"
        try:
            keys = [k for k in await self.cache.scan_keys(pattern) if k != keep_key]
            if keys:
                await self.cache.delete(*keys)
                logger.info(
                    "[SWR] Invalidated {} derived cache entr{} for {}",
                    len(keys),
                    "y" if len(keys) == 1 else "ies",
                    path,
                )
        except Exception as exc:  # noqa: BLE001
            # Same posture as _update_api_cache: a cache problem must not fail
            # the refresh that just succeeded.
            logger.warning("[SWR] Failed to invalidate variants of {}: {}", path, exc)

    async def _enqueue_refresh(
        self,
        entity_type: str,
        entity_id: str,
    ) -> None:
        """Enqueue a background refresh, deduplicating via job_id.

        ``job_id`` is set to ``entity_id`` so the task receives it directly
        as its first positional argument — no separate args needed.
        """
        job_id = entity_id
        try:
            # No pre-check: enqueue() claims the job with SET NX and returns
            # early when it loses. Asking first only added a round-trip and a
            # window for another process to claim in between.
            await self.task_queue.enqueue(
                f"refresh_{entity_type}",
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SWR] Failed to enqueue refresh for {}/{}: {}",
                entity_type,
                entity_id,
                exc,
            )
