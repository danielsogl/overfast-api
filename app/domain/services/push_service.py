"""Refreshing watched players and telling their subscribers about rank moves.

This exists because the app cannot do it. iOS schedules background refresh by
how often an app is opened, so a device that stopped opening the app -- the
only device worth a re-engagement notification -- is exactly the one whose
background task stops running. Polling here is the only place the work
reliably happens.

It pays three times over: the refresh keeps the snapshot series dense for
players nobody is currently looking at, that same series is what the rank
comparison reads, and delivery no longer depends on the client's scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import settings
from app.domain.ports.push_sender import PushMessage
from app.domain.push_alerts import rank_alert
from app.domain.push_messages import rank_alert_text
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from app.domain.ports.push_sender import PushSenderPort
    from app.domain.ports.storage import StoragePort
    from app.domain.services.player_service import PlayerService

# The two snapshots the rank comparison needs, and nothing more.
_SNAPSHOTS_TO_COMPARE = 2


class PushService:
    """Polls subscribed players and sends one alert per real rank move."""

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
