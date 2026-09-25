"""Refreshing watched players and telling their subscribers what changed.

This exists because the app cannot do it. iOS schedules background refresh by
how often an app is opened, so a device that stopped opening the app -- the
only device worth a re-engagement notification -- is exactly the one whose
background task stops running. Polling here is the only place the work
reliably happens.

It pays three times over: the refresh keeps the snapshot series dense for
players nobody is currently looking at, that same series is what the rank
comparison reads, and delivery no longer depends on the client's scheduler.

The hero-change alert rides the same subscriptions but works per device rather
than per player: it names heroes, and one device following three players who
share a main must get one notification, not three.

The weekly recap is opt-in and per device again, but on its own clock: it
fires once a device's local time reaches Sunday evening, which is why the
poller checking for it runs hourly rather than on the four-hour cadence the
other two alerts use.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import settings
from app.domain.ports.push_sender import PushMessage
from app.domain.push_alerts import changed_heroes, hero_alert, rank_alert, weekly_recap
from app.domain.push_messages import hero_alert_text, rank_alert_text, weekly_recap_text
from app.domain.utils.helpers import get_hero_name
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from datetime import tzinfo

    from app.domain.ports.push_sender import PushSenderPort
    from app.domain.ports.storage import StoragePort
    from app.domain.services.player_service import PlayerService

# The two snapshots the rank comparison needs, and nothing more.
_SNAPSHOTS_TO_COMPARE = 2

# A patch older than this is never announced. One guard, three embarrassments:
# the first run after a deploy blasting week-old news to every device, a run
# missed for two days resurrecting it, and Blizzard editing a published patch
# in place and moving nothing but its content.
_PATCH_MAX_AGE_DAYS = 2

# How many heroes a notification names. Three fits a lock screen; the badge on
# the heroes list is the complete answer.
_HEROES_IN_BODY = 3

# The local day and hour a device's weekly recap goes out: Sunday evening,
# once the week's games have happened but before it slips into Monday.
# ``datetime.weekday()``: Monday=0 .. Sunday=6.
_RECAP_WEEKDAY = 6
_RECAP_HOUR = 18

# The recap's lookback window. Fixed at 7 days rather than "since last
# Sunday": the poll runs hourly and a device's local Sunday 18:00 can fall on
# either UTC Sunday or Monday depending on the offset, so a rolling week is
# what stays correct regardless of which UTC day carries it.
_RECAP_WINDOW = timedelta(days=7)

# Enough to reach one snapshot past a week: the poller writes at most one
# every four hours (42 a week), requests add a few more.
_RECAP_SNAPSHOT_LIMIT = 200


def _resolve_timezone(name: str | None) -> tzinfo:
    """The device's local zone, or UTC when unset or unknown to our tzdata.

    The model only shape-checks the timezone string — rejecting one our
    tzdata doesn't recognise would also fail the whole registration and cost
    the device its rank alerts, so an unresolvable value degrades to UTC here
    instead of anywhere it could take the rest of the subscription down.
    """
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return UTC


class PushService:
    """Polls subscribed players and sends rank and hero-change alerts."""

    def __init__(
        self,
        storage: StoragePort,
        player_service: PlayerService,
        sender: PushSenderPort,
    ) -> None:
        self.storage = storage
        self.player_service = player_service
        self.sender = sender

    async def notify_rank_changes(self) -> int:
        """Run one poll. Returns the number of notifications handed to a service.

        Never raises: one unreachable player must not cost the rest of the run,
        and a scheduled job that dies on the first bad profile would silently
        stop notifying everyone.
        """
        player_ids = await self.storage.get_push_subscribed_player_ids()
        if not player_ids:
            logger.debug("[push] No subscriptions, nothing to poll")
            return 0

        logger.info("[push] Polling {} watched players", len(player_ids))
        sent = 0
        gone: list[str] = []

        for player_id in player_ids:
            try:
                messages = await self._messages_for(player_id)
            except Exception:  # noqa: BLE001
                logger.exception("[push] Failed to check {}", player_id)
                continue

            if not messages:
                continue
            gone.extend(await self.sender.send(messages))
            sent += len(messages)

        await self._drop(gone)
        logger.info("[push] Sent {} notifications", sent)
        return sent

    async def _messages_for(self, player_id: str) -> list[PushMessage]:
        """Refresh one player and compose an alert for each of its watchers."""
        await self.player_service.refresh_player_profile(player_id)

        snapshots = await self.storage.get_player_snapshots(
            player_id, limit=_SNAPSHOTS_TO_COMPARE
        )
        rank = rank_alert(snapshots)
        if rank is None:
            return []

        # The comparison above has no memory: it reads the two newest
        # snapshots and nothing else. While those two stay the newest and
        # differ, every run reaches the same conclusion — so a player who
        # earns a rank and then stops playing produces no newer snapshot, the
        # pair never moves, and the same notification goes out every four
        # hours until they play again.
        #
        # Announcing a rank only once closes that. Comparing the rank rather
        # than storing a "seen" flag keeps a genuine move back to a previous
        # rank notifiable: Gold 3 -> Plat 1 -> Gold 3 is two announcements,
        # because the second Gold 3 differs from the Plat 1 in between.
        if await self.storage.get_last_announced_rank(player_id) == rank:
            logger.debug("[push] {} still at {}, already announced", player_id, rank)
            return []

        subscriptions = await self.storage.get_push_subscriptions_for_player(player_id)
        if not subscriptions:
            return []

        # Recorded before delivery, not after. A send that fails is retried by
        # nothing, so the alternative is a partial failure re-announcing to
        # everyone who did receive it on the next run.
        await self.storage.set_last_announced_rank(player_id, rank)

        name = await self._display_name(player_id)
        messages = []
        for subscription in subscriptions:
            title, body = rank_alert_text(subscription["locale"], name, rank)
            messages.append(
                PushMessage(
                    token=subscription["token"],
                    platform=subscription["platform"],
                    title=title,
                    body=body,
                    player_id=player_id,
                    environment=subscription["environment"],
                )
            )
        return messages

    async def notify_hero_changes(self, patch: dict) -> int:
        """Tell each device which of the heroes it plays *patch* changed.

        Takes the already-fetched newest patch rather than fetching one: the
        caller owns the locale choice, and this stays a decision, not a
        request.

        Per device, not per player. A device watching three players who all
        main D.Va wants one notification naming D.Va, not three.
        """
        changed = changed_heroes(patch)
        patch_date = patch.get("date")
        if not changed or not patch_date:
            logger.debug("[push] Patch {} changed no hero, nothing to say", patch_date)
            return 0

        if (datetime.now(tz=UTC).date() - date.fromisoformat(patch_date)).days > (
            _PATCH_MAX_AGE_DAYS
        ):
            logger.info("[push] Patch {} is too old to announce", patch_date)
            return 0

        newest: dict[str, list[dict]] = {}
        messages: list[PushMessage] = []

        for subscription in await self.storage.get_push_subscriptions():
            if subscription["last_patch_alert"] == patch_date:
                continue

            snapshots = []
            for player_id in subscription["player_ids"]:
                if player_id not in newest:
                    newest[player_id] = await self.storage.get_player_snapshots(
                        player_id, limit=1
                    )
                snapshots.extend(newest[player_id])

            heroes = hero_alert(changed, snapshots, limit=_HEROES_IN_BODY)
            if not heroes:
                continue

            # Recorded before delivery for the same reason the rank path does
            # it: nothing retries a failed send, so the alternative is a
            # partial failure re-announcing to everyone who did receive it.
            await self.storage.set_last_announced_patch(
                subscription["token"], patch_date
            )

            title, body = hero_alert_text(
                subscription["locale"], [get_hero_name(hero) for hero in heroes]
            )
            messages.append(
                PushMessage(
                    token=subscription["token"],
                    platform=subscription["platform"],
                    title=title,
                    body=body,
                    # The first hero in the body, so the tap and the text agree
                    # about what the notification was about.
                    hero_key=heroes[0],
                    environment=subscription["environment"],
                )
            )

        gone = await self.sender.send(messages) if messages else []
        sent = len(messages)
        await self._drop(gone)
        logger.info("[push] Announced patch {} to {} devices", patch_date, sent)
        return sent

    async def notify_weekly_recaps(self, now: datetime | None = None) -> int:
        """Send each opted-in device its recap, when its local clock says so.

        Runs hourly (see the worker schedule) and checks local time per
        subscription before touching any snapshot, so an off-hour run across
        the whole table is cheap. *now* is overridable for tests; production
        callers leave it to the current time.

        Never raises: one subscription with a broken timezone or a missing
        player must not cost every other device its recap this hour.
        """
        now = now or datetime.now(tz=UTC)
        sent = 0
        gone: list[str] = []

        for subscription in await self.storage.get_push_subscriptions():
            player_id = subscription["recap_player_id"]
            if player_id is None:
                continue

            try:
                message = await self._recap_message(subscription, player_id, now)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[push] Failed to build recap for {}", subscription["token"]
                )
                continue

            if message is None:
                continue
            gone.extend(await self.sender.send([message]))
            sent += 1

        await self._drop(gone)
        logger.info("[push] Sent {} weekly recaps", sent)
        return sent

    async def _recap_message(
        self, subscription: dict, player_id: str, now: datetime
    ) -> PushMessage | None:
        """One device's recap message, or None when it is not due or is quiet."""
        local = now.astimezone(_resolve_timezone(subscription["timezone"]))
        if local.weekday() != _RECAP_WEEKDAY or local.hour != _RECAP_HOUR:
            return None

        local_date = local.date().isoformat()
        if subscription["last_recap"] == local_date:
            return None

        # The baseline is the newest snapshot from *before* the window, not the
        # oldest inside it. A snapshot is only written when the profile
        # changed, so the first one inside the week already contains that
        # week's first games — and a player who played once this week has one
        # snapshot in the window and nothing to compare it to.
        window_start = (now - _RECAP_WINDOW).timestamp()
        recent = await self.storage.get_player_snapshots(
            player_id, limit=_RECAP_SNAPSHOT_LIMIT
        )
        week = [s for s in recent if s["taken_at"] >= window_start]
        baseline = next((s for s in recent if s["taken_at"] < window_start), None)
        recap = weekly_recap(week if baseline is None else [*week, baseline])
        if recap is None:
            return None

        # Recorded before delivery, for the same reason the rank and hero
        # paths do it: nothing retries a failed send, so the alternative is a
        # duplicate recap to every device that did receive it on the next
        # hourly run.
        await self.storage.set_last_recap(subscription["token"], local_date)

        name = await self._display_name(player_id)
        hero = get_hero_name(recap["hero"]) if recap["hero"] else None
        title, body = weekly_recap_text(
            subscription["locale"],
            name,
            recap["games_played"],
            recap["games_won"],
            hero,
            recap["rank"],
        )
        return PushMessage(
            token=subscription["token"],
            platform=subscription["platform"],
            title=title,
            body=body,
            player_id=player_id,
            environment=subscription["environment"],
        )

    async def _display_name(self, player_id: str) -> str:
        """The name to put in the notification.

        Falls back to the id, which is a BattleTag in every case a user can
        subscribe to — unhelpful to nobody, and better than an empty sentence.
        """
        profile = await self.storage.get_player_profile(player_id)
        if profile is None:
            return player_id
        return profile.get("name") or profile.get("battletag") or player_id

    async def _drop(self, tokens: list[str]) -> None:
        """Remove the devices the push services reported as gone."""
        for token in tokens:
            await self.storage.delete_push_subscription(token)
        if tokens:
            logger.info("[push] Dropped {} unregistered devices", len(tokens))

    async def prune_stale_subscriptions(self) -> int:
        """Drop devices whose app has not launched inside the retention window.

        The registration doubles as a heartbeat, so silence is the only signal
        an uninstall ever gives us — the push services never mention a device
        that is simply never sent to.
        """
        return await self.storage.delete_old_push_subscriptions(
            settings.push_subscription_max_age_seconds
        )
