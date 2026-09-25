"""Tests for ValkeyTaskQueue adapter"""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

from app.adapters.tasks.valkey_task_queue import JOB_KEY_PREFIX, ValkeyTaskQueue


async def _pending(redis: fakeredis.FakeAsyncRedis, job_id: str) -> bool:
    return await redis.exists(f"{JOB_KEY_PREFIX}{job_id}") > 0


@pytest.fixture
def fake_redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(protocol=3)


@pytest.fixture
def queue(fake_redis: fakeredis.FakeAsyncRedis) -> ValkeyTaskQueue:
    return ValkeyTaskQueue(fake_redis)


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_job_skipped(self, queue: ValkeyTaskQueue):
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh": mock_task},
        ):
            await queue.enqueue("refresh", job_id="job-1")
            await queue.enqueue("refresh", job_id="job-1")

        mock_task.kiq.assert_awaited_once_with("job-1")

    @pytest.mark.asyncio
    async def test_different_job_ids_both_recorded(
        self, queue: ValkeyTaskQueue, fake_redis: fakeredis.FakeAsyncRedis
    ):
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh": mock_task},
        ):
            await queue.enqueue("refresh", job_id="job-a")
            await queue.enqueue("refresh", job_id="job-b")

        result_a = await _pending(fake_redis, "job-a")
        result_b = await _pending(fake_redis, "job-b")

        assert result_a
        assert result_b

    @pytest.mark.asyncio
    async def test_returns_effective_id(self, queue: ValkeyTaskQueue):
        result = await queue.enqueue("refresh", job_id="job-1")

        assert result == "job-1"

    @pytest.mark.asyncio
    async def test_falls_back_to_task_name_when_no_job_id(
        self, queue: ValkeyTaskQueue, fake_redis: fakeredis.FakeAsyncRedis
    ):
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh_heroes": mock_task},
        ):
            result = await queue.enqueue("refresh_heroes")

        pending = await _pending(fake_redis, "refresh_heroes")

        assert result == "refresh_heroes"
        assert pending


class TestDedupKey:
    @pytest.mark.asyncio
    async def test_pending_after_enqueue(
        self, queue: ValkeyTaskQueue, fake_redis: fakeredis.FakeAsyncRedis
    ):
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh": mock_task},
        ):
            await queue.enqueue("refresh", job_id="job-1")

        result = await _pending(fake_redis, "job-1")

        assert result


class TestEnqueueTaskDispatch:
    @pytest.mark.asyncio
    async def test_known_task_kiq_called(self, queue: ValkeyTaskQueue):
        """A known task name triggers task_fn.kiq(effective_id)."""
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh_heroes": mock_task},
        ):
            await queue.enqueue("refresh_heroes", job_id="heroes")
        mock_task.kiq.assert_awaited_once_with("heroes")

    @pytest.mark.asyncio
    async def test_unknown_task_skips_kiq(
        self, queue: ValkeyTaskQueue, fake_redis: fakeredis.FakeAsyncRedis
    ):
        """An unknown task name is a no-op — returns effective_id without claiming a dedup slot."""
        result = await queue.enqueue("nonexistent_task", job_id="xyz")

        pending = await _pending(fake_redis, "xyz")

        assert result == "xyz"
        assert not pending

    @pytest.mark.asyncio
    async def test_redis_exception_is_swallowed(
        self, fake_redis: fakeredis.FakeAsyncRedis
    ):
        """If redis raises, enqueue swallows the exception and returns effective_id."""
        queue = ValkeyTaskQueue(fake_redis)
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        cast("Any", fake_redis).set = AsyncMock(side_effect=RuntimeError("redis down"))
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh_heroes": mock_task},
        ):
            result = await queue.enqueue("refresh_heroes", job_id="boom")

        assert result == "boom"


class TestReleaseJob:
    @pytest.mark.asyncio
    async def test_release_removes_dedup_key(
        self, queue: ValkeyTaskQueue, fake_redis: fakeredis.FakeAsyncRedis
    ):
        """release_job deletes the dedup key."""
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh": mock_task},
        ):
            await queue.enqueue("refresh", job_id="job-1")

        await queue.release_job("job-1")

        result = await _pending(fake_redis, "job-1")
        assert not result

    @pytest.mark.asyncio
    async def test_release_allows_reenqueue(self, queue: ValkeyTaskQueue):
        """After release_job the same job_id can be dispatched again."""
        mock_task = MagicMock()
        mock_task.kiq = AsyncMock()
        with patch.dict(
            "app.adapters.tasks.valkey_task_queue.TASK_MAP",
            {"refresh": mock_task},
        ):
            await queue.enqueue("refresh", job_id="job-1")
            await queue.release_job("job-1")
            await queue.enqueue("refresh", job_id="job-1")

        assert mock_task.kiq.await_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_release_nonexistent_job_is_noop(self, queue: ValkeyTaskQueue):
        """Releasing an unknown job_id does not raise."""
        await queue.release_job("nonexistent-job")

    @pytest.mark.asyncio
    async def test_redis_exception_is_swallowed(
        self, fake_redis: fakeredis.FakeAsyncRedis
    ):
        """If redis raises during release, the exception is swallowed."""
        queue = ValkeyTaskQueue(fake_redis)
        cast("Any", fake_redis).delete = AsyncMock(
            side_effect=RuntimeError("redis down")
        )
        await queue.release_job("any-job")
