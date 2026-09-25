"""Tests for the rank-alert poller and the hero-change announcer"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


@pytest.mark.asyncio
async def test_announces_a_rank_once_even_when_the_snapshots_stop_moving(
    storage_db: FakeStorage,
):
    """The repeat this guards against is the common case, not an edge one.

    `rank_alert` reads the two newest snapshots and has no memory. A player who
    climbs and then stops playing produces no newer snapshot, so that pair stays
    the newest and every four-hourly run reaches the same conclusion. Before the
    announced-rank record, that resent the same notification indefinitely.
    """
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    assert await service.notify_rank_changes() == 1
    assert len(sender.sent) == 1

    # Same data, same comparison, three more runs.
    for _ in range(3):
        assert await service.notify_rank_changes() == 0
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_announces_a_return_to_a_previous_rank(storage_db: FakeStorage):
    """Storing the rank, not a "seen" flag, is what makes this work.

    Gold 3 -> Platinum 5 -> Gold 3 is two pieces of news. A boolean "already
    notified about this player" would swallow the second.
    """
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 3, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    assert await service.notify_rank_changes() == 1

    await _seed(storage_db, _snapshot("gold", 3, 3))
    assert await service.notify_rank_changes() == 1
    assert [m.body.split()[-2:] for m in sender.sent] == [
        ["Platinum", "5"],
        ["Gold", "3"],
    ]


@pytest.mark.asyncio
async def test_carries_the_player_id_into_the_message(storage_db: FakeStorage):
    """The payload is what lets a tap open the right profile."""
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await _seed(storage_db, _snapshot("gold", 1, 1), _snapshot("platinum", 5, 2))
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    await service.notify_rank_changes()

    assert sender.sent[0].player_id == PLAYER


# ─── Hero change announcements ────────────────────────────────────────────────

OTHER_PLAYER = "Player-4242"


def _today(days_ago: int = 0) -> str:
    return (datetime.now(tz=UTC).date() - timedelta(days=days_ago)).isoformat()


def _patch(*heroes: str, days_ago: int = 0) -> dict:
    return {
        "date": _today(days_ago),
        "sections": [
            {
                "entries": [
                    {"hero": hero, "details": ["Nerfed."], "abilities": []}
                    for hero in heroes
                ]
            }
        ],
    }


def _hero_snapshot(**by_hero: int) -> dict:
    return {
        "competitive": {},
        "heroes": {
            "pc": {
                "quickplay": {
                    hero: {"time_played": seconds} for hero, seconds in by_hero.items()
                }
            }
        },
    }


@pytest.mark.asyncio
async def test_names_the_changed_heroes_the_device_plays_most(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-de-1234", "ios", "de", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(ana=10, dva=900))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    sent = await service.notify_hero_changes(_patch("ana", "dva"))

    assert sent == 1
    message = sender.sent[0]
    assert message.title == "Helden-Update"
    assert message.body == "Änderungen an D.Va, Ana im neuesten Patch"
    # The deep link is the hero the body names first, so tap and text agree.
    assert message.hero_key == "dva"
    assert message.player_id is None


@pytest.mark.asyncio
async def test_one_device_watching_two_players_gets_one_message(
    storage_db: FakeStorage,
):
    await storage_db.upsert_push_subscription(
        "tok-en-5678", "android", "en-US", [PLAYER, OTHER_PLAYER]
    )
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    await storage_db.add_player_snapshot(OTHER_PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    sent = await service.notify_hero_changes(_patch("dva"))

    assert sent == 1
    assert sender.sent[0].body == "Changes to D.Va in the latest patch"


@pytest.mark.asyncio
async def test_announces_a_patch_once_per_device(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    patch = _patch("dva")
    assert await service.notify_hero_changes(patch) == 1
    assert await service.notify_hero_changes(patch) == 0


@pytest.mark.asyncio
async def test_a_relaunch_does_not_re_arm_the_announcement(storage_db: FakeStorage):
    """The app re-upserts on every launch; that write is the client's, not ours."""
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender()

    service, _ = _service(storage_db, sender)
    patch = _patch("dva")
    await service.notify_hero_changes(patch)
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])

    assert await service.notify_hero_changes(patch) == 0


@pytest.mark.asyncio
async def test_never_announces_a_stale_patch(storage_db: FakeStorage):
    """The first run after a deploy must not blast week-old news."""
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender()

    service, _ = _service(storage_db, sender)

    assert await service.notify_hero_changes(_patch("dva", days_ago=7)) == 0
    assert sender.sent == []


@pytest.mark.asyncio
async def test_says_nothing_about_heroes_the_device_does_not_play(
    storage_db: FakeStorage,
):
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(genji=900))
    sender = _Sender()

    service, _ = _service(storage_db, sender)

    assert await service.notify_hero_changes(_patch("dva")) == 0


@pytest.mark.asyncio
async def test_a_patch_touching_no_hero_sends_nothing(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender()

    service, _ = _service(storage_db, sender)

    assert await service.notify_hero_changes({"date": _today(), "sections": []}) == 0


@pytest.mark.asyncio
async def test_drops_the_devices_the_service_reported_gone_on_a_hero_alert(
    storage_db: FakeStorage,
):
    await storage_db.upsert_push_subscription("tok-en-5678", "ios", "en-US", [PLAYER])
    await storage_db.add_player_snapshot(PLAYER, 1, _hero_snapshot(dva=500))
    sender = _Sender(gone=["tok-en-5678"])

    service, _ = _service(storage_db, sender)
    await service.notify_hero_changes(_patch("dva"))

    assert await storage_db.get_push_subscriptions() == []


# ─── Weekly recap ──────────────────────────────────────────────────────────────

# A Sunday 18:00 UTC instant, used as the "now" every recap test fires from.
_SUNDAY_1800_UTC = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)


def _week_data(games_played: int, games_won: int, **by_hero: int) -> dict:
    return {
        "general": {
            "pc": {
                "quickplay": {
                    "games_played": games_played,
                    "games_won": games_won,
                    "games_lost": games_played - games_won,
                    "time_played": 0,
                }
            }
        },
        "heroes": {
            "pc": {
                "quickplay": {
                    hero: {"time_played": seconds, "games_won": 0}
                    for hero, seconds in by_hero.items()
                }
            }
        },
        "competitive": {},
    }


async def _seed_week(storage: FakeStorage, player_id: str) -> None:
    """A week's worth of play: an old snapshot and a fresh one, 7 games apart.

    ``add_player_snapshot`` stamps ``taken_at`` with wall-clock time at
    insertion, which lands well after any ``since`` this suite computes from
    the fixed ``_SUNDAY_1800_UTC`` — so the pair is "within the last 7 days"
    from the recap's point of view without having to fake the clock.
    """
    await storage.add_player_snapshot(player_id, 1, _week_data(1, 1, dva=50))
    await storage.add_player_snapshot(player_id, 2, _week_data(8, 5, dva=950))


@pytest.mark.asyncio
async def test_sends_a_recap_at_sunday_eighteen_local(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert sent == 1
    assert sender.sent[0].body == "TeKrop-2217: Games: 7 · Wins: 4 · Top hero: D.Va"
    assert sender.sent[0].player_id == PLAYER


@pytest.mark.asyncio
async def test_a_single_game_this_week_is_compared_to_last_week(
    storage_db: FakeStorage,
):
    """One snapshot inside the window still recaps, against the one before it."""
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await _seed_week(storage_db, PLAYER)
    last_week = (_SUNDAY_1800_UTC - timedelta(days=8)).timestamp()
    storage_db._snapshots[PLAYER][1]["taken_at"] = last_week
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert sent == 1
    assert sender.sent[0].body == "TeKrop-2217: Games: 7 · Wins: 4 · Top hero: D.Va"


@pytest.mark.asyncio
async def test_uses_the_device_timezone(storage_db: FakeStorage):
    """20:00 UTC is 18:00 in a UTC-2 zone, so only that device fires."""
    utc_now = _SUNDAY_1800_UTC + timedelta(hours=2)
    await storage_db.upsert_push_subscription(
        "tok-1234",
        "ios",
        "en-US",
        [PLAYER],
        recap_player_id=PLAYER,
        timezone="Etc/GMT+2",
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=utc_now)

    assert sent == 1


@pytest.mark.asyncio
async def test_an_unknown_timezone_falls_back_to_utc(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription(
        "tok-1234",
        "ios",
        "en-US",
        [PLAYER],
        recap_player_id=PLAYER,
        timezone="Not/AZone",
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert sent == 1


@pytest.mark.asyncio
async def test_silent_outside_sunday_eighteen(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    not_sunday = _SUNDAY_1800_UTC + timedelta(days=1)
    wrong_hour = _SUNDAY_1800_UTC.replace(hour=17)

    sent = [
        await service.notify_weekly_recaps(now=not_sunday),
        await service.notify_weekly_recaps(now=wrong_hour),
    ]

    assert sent == [0, 0]
    assert sender.sent == []


@pytest.mark.asyncio
async def test_sent_once_per_week(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    # Same hour, run twice — a re-run within the hour must not double-send.
    sent = [
        await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC),
        await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC),
    ]

    assert sent == [1, 0]
    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_silent_with_no_games_in_the_week(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await storage_db.add_player_snapshot(PLAYER, 1, _week_data(3, 2))
    await storage_db.add_player_snapshot(PLAYER, 2, _week_data(3, 2))
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert sent == 0
    assert sender.sent == []


@pytest.mark.asyncio
async def test_silent_for_devices_without_a_recap_player(storage_db: FakeStorage):
    await storage_db.upsert_push_subscription("tok-1234", "ios", "en-US", [PLAYER])
    await _seed_week(storage_db, PLAYER)
    sender = _Sender()
    service, _ = _service(storage_db, sender)

    sent = await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert sent == 0


@pytest.mark.asyncio
async def test_drops_the_devices_the_service_reported_gone_on_a_recap(
    storage_db: FakeStorage,
):
    await storage_db.upsert_push_subscription(
        "tok-1234", "ios", "en-US", [PLAYER], recap_player_id=PLAYER
    )
    await _seed_week(storage_db, PLAYER)
    sender = _Sender(gone=["tok-1234"])
    service, _ = _service(storage_db, sender)

    await service.notify_weekly_recaps(now=_SUNDAY_1800_UTC)

    assert await storage_db.get_push_subscriptions() == []
