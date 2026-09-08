"""Tests for the rank-alert poller"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.domain.services.push_service import PushService

if TYPE_CHECKING:
    from tests.fake_storage import FakeStorage

PLAYER = "TeKrop-2217"

# Two watchers on the same player, so one rank move must fan out to both.
_WATCHERS = 2


def _snapshot(division: str, tier: int, last_updated: int) -> dict:
    return {
        "taken_at": last_updated,
        "last_updated_blizzard": last_updated,
        "data": {
            "competitive": {"pc": {"tank": {"division": division, "tier": tier}}},
            "heroes": {},
        },
    }


class _Sender:
    """Records what it was asked to deliver and reports `gone` back."""

    def __init__(self, gone: list[str] | None = None) -> None:
        self.sent: list = []
        self._gone = gone or []

    async def send(self, messages) -> list[str]:
        self.sent.extend(messages)
        return self._gone


async def _seed(storage: FakeStorage, *snapshots: dict) -> None:
    for snapshot in snapshots:
        await storage.add_player_snapshot(
            PLAYER, snapshot["last_updated_blizzard"], snapshot["data"]
        )


def _service(storage: FakeStorage, sender: _Sender) -> tuple[PushService, AsyncMock]:
    player_service = AsyncMock()
    return PushService(storage, player_service, sender), player_service  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_notifies_every_watcher_of_a_rank_move(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await storage_db.upsert_push_subscription(
        "tok-en-5678", "android", "en-US", [PLAYER]
    )
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()

    service, player_service = _service(storage_db, sender)
    sent = await service.notify_rank_changes()

    assert sent == _WATCHERS
    player_service.refresh_player_profile.assert_awaited_once_with(PLAYER)
    by_token = {m.token: m for m in sender.sent}
    assert by_token["tok-de-1234"].title == "Rang-Update"
    assert by_token["tok-de-1234"].body.endswith("Platinum 5")
    assert by_token["tok-en-5678"].title == "Rank Update"


@pytest.mark.asyncio
async def test_sends_nothing_when_the_rank_held(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("gold", 1, 2))
    sender = _Sender()

    service, _ = _service(storage_db, sender)

    assert await service.notify_rank_changes() == 0
    assert sender.sent == []


@pytest.mark.asyncio
async def test_does_not_poll_without_subscriptions(storage_db: FakeStorage):
    sender = _Sender()
    service, player_service = _service(storage_db, sender)

    assert await service.notify_rank_changes() == 0
    player_service.refresh_player_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_broken_player_does_not_stop_the_run(storage_db: FakeStorage):
    """A scheduled job that dies on the first bad profile notifies nobody."""
    await storage_db.upsert_push_subscription("tok-a-1234", "ios", "de", ["broken"])
    await storage_db.upsert_push_subscription("tok-b-5678", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("diamond", 3, 2))
    sender = _Sender()

    service, player_service = _service(storage_db, sender)
    player_service.refresh_player_profile.side_effect = lambda pid: (
        _raise() if pid == "broken" else None
    )

    assert await service.notify_rank_changes() == 1
    assert [m.token for m in sender.sent] == ["tok-b-5678"]


_BLIZZARD_DOWN = RuntimeError("blizzard is down")


def _raise() -> None:
    raise _BLIZZARD_DOWN


@pytest.mark.asyncio
async def test_drops_the_devices_the_service_reported_gone(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender(gone=["tok-de-1234"])

    service, _ = _service(storage_db, sender)
    await service.notify_rank_changes()

    assert await storage_db.get_push_subscribed_player_ids() == []


@pytest.mark.asyncio
async def test_carries_the_token_environment_into_the_message(
    storage_db: FakeStorage,
):
    await storage_db.upsert_push_subscription(
        "tok-de-1234", "ios", "de", [PLAYER], environment="sandbox"
    )
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    await service.notify_rank_changes()

    assert sender.sent[0].environment == "sandbox"


@pytest.mark.asyncio
async def test_falls_back_to_the_id_when_no_profile_is_stored(
    storage_db: FakeStorage,
):
    """The id is a BattleTag in every case a user can subscribe to."""
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    await service.notify_rank_changes()

    assert sender.sent[0].body.startswith(PLAYER)
