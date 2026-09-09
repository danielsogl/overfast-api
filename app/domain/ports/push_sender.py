"""Port for delivering a push alert to a device"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


# kw_only because the fields are six strings in a row, several of them
# interchangeable to the type checker. Adding `player_id` ahead of
# `environment` would otherwise have silently shifted every positional
# construction by one and put an environment into the player id — accepted by
# every check, wrong at runtime.
@dataclass(frozen=True, slots=True, kw_only=True)
class PushMessage:
    """One notification, already composed in the recipient's language."""

    token: str
    platform: str
    title: str
    body: str
    # Where tapping the notification should land. Exactly one is set: a rank
    # alert is about a player, a hero alert about a hero. Without either the
    # tap can only open the app, which drops the reader exactly where they
    # were not looking — so the senders omit the key rather than send a null,
    # which the app would read as a destination.
    player_id: str | None = None
    hero_key: str | None = None
    # Which APNs host and key may deliver this. Ignored on Android.
    environment: str = "production"


class PushSenderPort(Protocol):
    """Delivers notifications and reports which tokens are gone.

    Returning the dead tokens rather than raising is the point: a device that
    uninstalled the app is the normal case, not a failure, and it is the only
    moment the push services will ever tell us. The caller drops those rows.
    """

    async def send(self, messages: Sequence[PushMessage]) -> list[str]:
        """
        Deliver each message, independently.

        Returns:
            Tokens the service reported as unregistered or malformed
        """
        ...
