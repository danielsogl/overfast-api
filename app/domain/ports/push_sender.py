"""Port for delivering a rank alert to a device"""

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
    # The player the alert is about, carried into the payload so tapping the
    # notification can open that profile. Without it the tap can only open the
    # app, which drops the reader exactly where they were not looking.
    player_id: str
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
