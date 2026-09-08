"""Port for delivering a rank alert to a device"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class PushMessage:
    """One notification, already composed in the recipient's language."""

    token: str
    platform: str
    title: str
    body: str


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
