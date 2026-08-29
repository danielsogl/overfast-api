"""A refresh must reach the entries a request actually asked for.

Refresh tasks write one hardcoded key (``/heroes``), but the request that
enqueued them may have been ``/heroes?role=damage``. Those variants used to sit
stale for their full 24h TTL, re-enqueueing a refresh that once again updated a
key nobody had asked for.
"""

import fakeredis
import pytest

from app.adapters.cache import ValkeyCache
from app.config import settings
from app.domain.services.base_service import BaseService


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> ValkeyCache:
    """ValkeyCache is a singleton with a no-arg constructor, so swap its server."""
    manager = ValkeyCache()
    monkeypatch.setattr(manager, "valkey_server", fakeredis.FakeAsyncRedis(protocol=3))
    return manager


def key(path: str) -> str:
    return f"{settings.api_cache_key_prefix}:{path}"


def service_with(cache: ValkeyCache) -> BaseService:
    """Only the cache adapter is exercised here."""
    return BaseService(
        cache=cache,
        storage=None,  # ty: ignore[invalid-argument-type]
        blizzard_client=None,  # ty: ignore[invalid-argument-type]
        task_queue=None,  # ty: ignore[invalid-argument-type]
    )


class TestDerivedCacheInvalidation:
    @pytest.mark.asyncio
    async def test_variants_go_and_siblings_stay(self, cache: ValkeyCache):
        """The '?' must be matched as a literal.

        In a Valkey glob a bare '?' matches any single character, so the obvious
        pattern "/heroes?*" also matches "/heroes/ana" — a heroes-list refresh
        would have wiped every hero detail entry and /heroes/stats with it.
        """
        paths = [
            "/heroes",
            "/heroes?role=damage",
            "/heroes?locale=de-de",
            "/heroes/ana",
            "/heroes/ana?locale=de-de",
            "/heroes/stats",
            "/maps",
        ]
        for path in paths:
            await cache.set(key(path), b"x")

        await service_with(cache)._invalidate_derived_cache("/heroes", keep="/heroes")

        survivors = sorted(await cache.scan_keys(f"{settings.api_cache_key_prefix}:*"))

        assert survivors == sorted(
            key(path)
            for path in [
                "/heroes",
                "/heroes/ana",
                "/heroes/ana?locale=de-de",
                "/heroes/stats",
                "/maps",
            ]
        )

    @pytest.mark.asyncio
    async def test_the_key_just_written_survives(self, cache: ValkeyCache):
        """A localised refresh writes /heroes?locale=de-de, which the pattern
        matches — deleting it would drop the entry the refresh just produced."""
        for path in ["/heroes?locale=de-de", "/heroes?role=damage"]:
            await cache.set(key(path), b"x")

        await service_with(cache)._invalidate_derived_cache(
            "/heroes", keep="/heroes?locale=de-de"
        )

        survivors = await cache.scan_keys(f"{settings.api_cache_key_prefix}:*")

        assert survivors == [key("/heroes?locale=de-de")]
