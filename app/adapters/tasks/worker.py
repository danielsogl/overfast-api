"""Background worker tasks for OverFast API.

Tasks are executed by the taskiq worker process::

    taskiq worker app.adapters.tasks.worker:broker

Cron tasks are scheduled by the taskiq scheduler::

    taskiq scheduler app.adapters.tasks.worker:scheduler

:func:`taskiq_fastapi.init` wires FastAPI's dependency injection so each task
function receives its service dependencies from the same DI container used by
the API server (including any overrides set in ``app.main``).
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from taskiq import TaskiqDepends
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.scheduler.scheduler import TaskiqScheduler
from taskiq_fastapi import init as taskiq_init

from app.adapters.tasks.task_registry import TASK_MAP
from app.adapters.tasks.valkey_broker import ValkeyListBroker
from app.api.dependencies import (
    get_blizzard_client,
    get_gamemode_service,
    get_hero_service,
    get_map_service,
    get_patch_notes_service,
    get_player_service,
    get_role_service,
    get_storage,
    get_task_queue,
)
from app.config import settings
from app.domain.enums import Locale
from app.domain.ports import BlizzardClientPort, StoragePort, TaskQueuePort
from app.domain.services import (
    GamemodeService,
    HeroService,
    MapService,
    PatchNotesService,
    PlayerService,
    RoleService,
)
from app.infrastructure.logger import logger

# ─── Broker ───────────────────────────────────────────────────────────────────

broker = ValkeyListBroker(
    url=f"valkey://{settings.valkey_host}:{settings.valkey_port}",
    queue_name="taskiq:queue",
    max_pool_size=settings.worker_max_concurrent_jobs,
)

# Wire FastAPI DI into taskiq tasks.
# In worker mode this also triggers the FastAPI lifespan (DB init, cache eviction…).
taskiq_init(broker, "app.main:app")


# ─── Scheduler (cron) ────────────────────────────────────────────────────────

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)

# ─── Dependency type aliases ─────────────────────────────────────────────────

HeroServiceDep = Annotated[HeroService, TaskiqDepends(get_hero_service)]
RoleServiceDep = Annotated[RoleService, TaskiqDepends(get_role_service)]
MapServiceDep = Annotated[MapService, TaskiqDepends(get_map_service)]
GamemodeServiceDep = Annotated[GamemodeService, TaskiqDepends(get_gamemode_service)]
PatchNotesServiceDep = Annotated[
    PatchNotesService, TaskiqDepends(get_patch_notes_service)
]
PlayerServiceDep = Annotated[PlayerService, TaskiqDepends(get_player_service)]
BlizzardClientDep = Annotated[BlizzardClientPort, TaskiqDepends(get_blizzard_client)]
StorageDep = Annotated[StoragePort, TaskiqDepends(get_storage)]
TaskQueueDep = Annotated[TaskQueuePort, TaskiqDepends(get_task_queue)]


# ─── Refresh helper ──────────────────────────────────────────────────────────


@asynccontextmanager
async def _run_refresh_task(
    entity_id: str,
    task_queue: TaskQueuePort,
) -> AsyncIterator[None]:
    """Run a refresh task end-to-end: log the outcome and duration, then
    release the dedup key so the job can be re-enqueued immediately.
    """
    start = time.monotonic()
    duration = 0.0
    try:
        yield
        duration = time.monotonic() - start
        logger.info("[Worker] Refresh completed: {} in {:.3f}s", entity_id, duration)
    except Exception as exc:
        duration = time.monotonic() - start
        logger.warning(
            "[Worker] Refresh failed: {} — {} ({:.3f}s)", entity_id, exc, duration
        )
        raise
    finally:
        await task_queue.release_job(entity_id)


# ─── Refresh tasks ────────────────────────────────────────────────────────────


@broker.task
async def refresh_heroes(
    entity_id: str, service: HeroServiceDep, task_queue: TaskQueueDep
) -> None:
    """Refresh the heroes list for one locale.

    ``entity_id`` format: ``heroes:{locale}``  e.g. ``heroes:en-us``
    """
    _, locale_str = entity_id.split(":", 1)
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_list(Locale(locale_str))


@broker.task
async def refresh_hero(
    entity_id: str, service: HeroServiceDep, task_queue: TaskQueueDep
) -> None:
    """Refresh a single hero for one locale.

    ``entity_id`` format: ``hero:{hero_key}:{locale}``  e.g. ``hero:ana:en-us``
    """
    _, hero_key, locale_str = entity_id.split(":", 2)
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_single(hero_key, Locale(locale_str))


@broker.task
async def refresh_roles(
    entity_id: str, service: RoleServiceDep, task_queue: TaskQueueDep
) -> None:
    """Refresh roles for one locale.

    ``entity_id`` format: ``roles:{locale}``  e.g. ``roles:en-us``
    """
    _, locale_str = entity_id.split(":", 1)
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_list(Locale(locale_str))


@broker.task
async def refresh_maps(
    entity_id: str,
    service: MapServiceDep,
    task_queue: TaskQueueDep,
) -> None:
    """Refresh all maps. ``entity_id`` is always ``maps:all``."""
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_list()


@broker.task
async def refresh_gamemodes(
    entity_id: str,
    service: GamemodeServiceDep,
    task_queue: TaskQueueDep,
) -> None:
    """Refresh all game modes. ``entity_id`` is always ``gamemodes:all``."""
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_list()


@broker.task
async def refresh_patch_notes(
    entity_id: str,
    service: PatchNotesServiceDep,
    task_queue: TaskQueueDep,
) -> None:
    """Refresh the patch notes for one locale.

    ``entity_id`` format: ``patch_notes:{locale}``  e.g. ``patch_notes:en-us``
    """
    _, locale_str = entity_id.split(":", 1)
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_list(Locale(locale_str))


@broker.task
async def refresh_player_profile(
    entity_id: str, service: PlayerServiceDep, task_queue: TaskQueueDep
) -> None:
    """Refresh a player career profile.

    ``entity_id`` is the raw ``player_id`` string.
    Calls :meth:`~app.domain.services.PlayerService.refresh_player_profile`
    which bypasses the persistent-storage fast-path to guarantee a live
    Blizzard fetch regardless of how recently the profile was stored.
    """
    async with _run_refresh_task(entity_id, task_queue):
        await service.refresh_player_profile(entity_id)


# ─── Cron tasks ───────────────────────────────────────────────────────────────


@broker.task(schedule=[{"cron": "0 3 * * *"}])
async def cleanup_stale_players(storage: StorageDep) -> None:
    """Apply the player retention windows (runs daily at 03:00 UTC).

    Profiles and snapshots age out on separate clocks — a profile is a cache of
    something Blizzard will hand back, a snapshot is not — but they are pruned in
    one job so the cleanup stays one schedule and one log line. Either window can
    be disabled on its own with ``<= 0``.
    """
    prune_profiles = settings.player_profile_max_age > 0
    prune_snapshots = settings.player_snapshot_max_age > 0
    if not prune_profiles and not prune_snapshots:
        logger.debug(
            "[Worker] cleanup_stale_players: disabled (max_age <= 0), skipping."
        )
        return

    logger.info("[Worker] cleanup_stale_players: Deleting stale player data...")
    try:
        if prune_profiles:
            await storage.delete_old_player_profiles(settings.player_profile_max_age)
        if prune_snapshots:
            await storage.delete_old_player_snapshots(settings.player_snapshot_max_age)
    except Exception:  # noqa: BLE001
        logger.exception("[Worker] cleanup_stale_players: Failed.")
        return

    logger.info("[Worker] cleanup_stale_players: Done.")


# ─── Task registry (used by ValkeyTaskQueue for dispatch) ────────────────────

TASK_MAP.update(
    {
        "refresh_heroes": refresh_heroes,
        "refresh_hero": refresh_hero,
        "refresh_roles": refresh_roles,
        "refresh_maps": refresh_maps,
        "refresh_gamemodes": refresh_gamemodes,
        "refresh_patch_notes": refresh_patch_notes,
        "refresh_player_profile": refresh_player_profile,
    }
)
