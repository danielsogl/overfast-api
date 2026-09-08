"""Delivering rank alerts to APNs and FCM.

Two services because the device tokens differ: `expo-notifications` hands the
app a native APNs token on iOS and a native FCM token on Android, so each goes
to its own provider. Routing iOS through FCM as well would mean shipping the
Firebase Messaging SDK in the app purely to swap one token type for another.

Both senders are best-effort. A notification is not worth an exception in a
scheduled job, so a failure is logged and the run continues; the only thing
that travels back is the list of tokens the service says are gone, because
that is the sole moment we learn a device is no longer there.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2

from app.adapters.push.token_source import ApnsTokenSource, GoogleTokenSource
from app.config import settings
from app.domain.enums import PushEnvironment, PushPlatform
from app.infrastructure.logger import logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.domain.ports.push_sender import PushMessage, PushSenderPort

# Apple issues environment-bound keys, and each host accepts only its own.
# Verified 2026-09-08: the other host answers 403 BadEnvironmentKeyInToken for
# a token that its own host merely calls a bad device token.
_APNS_HOSTS = {
    PushEnvironment.PRODUCTION.value: "https://api.push.apple.com",
    PushEnvironment.SANDBOX.value: "https://api.sandbox.push.apple.com",
}

# Only reasons that can mean nothing but "this device is gone". 410
# Unregistered is unambiguous; BadDeviceToken also covers a token sent to the
# wrong environment, which the routing above prevents and which a re-launch
# repairs anyway.
#
# `DeviceTokenNotForTopic` is deliberately absent. It fires when the topic does
# not match the token, so a mistyped `apns_topic` would return it for *every*
# device and one poll would delete every subscription we have. A wrong config
# must cost notifications, never the rows needed to resume them.
_APNS_GONE_REASONS = frozenset({"Unregistered", "BadDeviceToken"})

# UNREGISTERED means the token is gone; SENDER_ID_MISMATCH means it belongs to
# a different Firebase project and never will be ours.
#
# INVALID_ARGUMENT is deliberately absent even though a malformed token
# produces it: so does a malformed *payload*, and FCM does not distinguish the
# two in the status. Treating it as a dead device would let one bad message
# shape wipe every subscription in a single poll. A genuinely broken token
# instead ages out through the liveness window.
_FCM_GONE_STATUSES = frozenset({"UNREGISTERED", "SENDER_ID_MISMATCH"})

# One connection, many notifications: both services are HTTP/2 and expect the
# stream to be reused rather than reconnected per device.
_TIMEOUT = httpx2.Timeout(10.0)


def _redact(token: str) -> str:
    """Device tokens are identifiers; logs get enough to correlate, no more."""
    return f"{token[:8]}…"


class ApnsSender:
    """Sends to Apple Push Notification service over HTTP/2."""

    def __init__(self, tokens_by_environment: dict[str, ApnsTokenSource]) -> None:
        self._tokens = tokens_by_environment

    async def send(self, messages: Sequence[PushMessage]) -> list[str]:
        if not messages:
            return []

        async with httpx2.AsyncClient(http2=True, timeout=_TIMEOUT) as client:
            return [
                message.token
                for message in messages
                if await self._send_one(client, message)
            ]

    async def _send_one(self, client: httpx2.AsyncClient, message: PushMessage) -> bool:
        """True when the token should be dropped."""
        tokens = self._tokens.get(message.environment)
        host = _APNS_HOSTS.get(message.environment)
        if tokens is None or host is None:
            # A build registered against an environment this deployment has no
            # key for. Not the device's fault, so the row stays.
            logger.warning("[push] No APNs key for environment {}", message.environment)
            return False

        try:
            response = await client.post(
                f"{host}/3/device/{message.token}",
                headers={
                    "authorization": f"bearer {tokens.get()}",
                    "apns-topic": settings.apns_topic,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
                # `player_id` sits beside `aps`, not inside it: APNs treats
                # every top-level key other than `aps` as the app's own
                # payload and hands it to the client untouched.
                json={
                    "aps": {
                        "alert": {"title": message.title, "body": message.body},
                        "sound": "default",
                    },
                    "player_id": message.player_id,
                },
            )
        except httpx2.HTTPError as error:
            logger.warning("[push] APNs request failed: {}", error)
            return False

        if response.status_code == httpx2.codes.OK:
            return False

        reason = ""
        try:
            reason = response.json().get("reason", "")
        except ValueError:
            reason = response.text[:100]

        if reason in _APNS_GONE_REASONS:
            logger.info("[push] APNs dropped {}: {}", _redact(message.token), reason)
            return True

        logger.warning(
            "[push] APNs refused {} ({}): {}",
            _redact(message.token),
            response.status_code,
            reason,
        )
        return False


class FcmSender:
    """Sends to Firebase Cloud Messaging's HTTP v1 API."""

    def __init__(self, service_account_path: str, project_id: str) -> None:
        self._tokens = GoogleTokenSource(service_account_path)
        self._url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async def send(self, messages: Sequence[PushMessage]) -> list[str]:
        if not messages:
            return []

        async with httpx2.AsyncClient(timeout=_TIMEOUT) as client:
            access_token = await self._tokens.get(client)
            if access_token is None:
                return []
            return [
                message.token
                for message in messages
                if await self._send_one(client, access_token, message)
            ]

    async def _send_one(
        self, client: httpx2.AsyncClient, access_token: str, message: PushMessage
    ) -> bool:
        """True when the token should be dropped."""
        try:
            response = await client.post(
                self._url,
                headers={"authorization": f"Bearer {access_token}"},
                # FCM `data` values must be strings, and the block is
                # delivered alongside the notification so the tap handler can
                # read it.
                json={
                    "message": {
                        "token": message.token,
                        "notification": {
                            "title": message.title,
                            "body": message.body,
                        },
                        "data": {"player_id": message.player_id},
                    }
                },
            )
        except httpx2.HTTPError as error:
            logger.warning("[push] FCM request failed: {}", error)
            return False

        if response.status_code == httpx2.codes.OK:
            return False

        status = ""
        try:
            status = response.json().get("error", {}).get("status", "")
        except ValueError:
            status = response.text[:100]

        if status in _FCM_GONE_STATUSES:
            logger.info("[push] FCM dropped {}: {}", _redact(message.token), status)
            return True

        logger.warning(
            "[push] FCM refused {} ({}): {}",
            _redact(message.token),
            response.status_code,
            status,
        )
        return False


class PushSender:
    """Routes each message to the service that issued its token.

    A platform whose credentials are absent is skipped rather than treated as
    an error: half-configured is a normal state while one of the two keys is
    still being provisioned, and Android alerts should not wait for Apple.
    """

    def __init__(
        self,
        apns: PushSenderPort | None = None,
        fcm: PushSenderPort | None = None,
    ) -> None:
        self._apns = apns
        self._fcm = fcm

    async def send(self, messages: Sequence[PushMessage]) -> list[str]:
        by_platform: dict[str, list[PushMessage]] = {
            PushPlatform.IOS.value: [],
            PushPlatform.ANDROID.value: [],
        }
        for message in messages:
            bucket = by_platform.get(message.platform)
            if bucket is None:
                logger.warning("[push] Unknown platform {}", message.platform)
                continue
            bucket.append(message)

        senders = [
            (self._apns, by_platform[PushPlatform.IOS.value]),
            (self._fcm, by_platform[PushPlatform.ANDROID.value]),
        ]
        results = await asyncio.gather(
            *(
                sender.send(batch)
                for sender, batch in senders
                if sender is not None and batch
            )
        )
        return [token for result in results for token in result]


def build_push_sender() -> PushSender | None:
    """The configured sender, or None when nothing can be delivered.

    Returning None rather than a no-op keeps the scheduled job honest: it can
    skip the whole poll instead of refreshing profiles for notifications that
    would go nowhere.
    """
    if not settings.push_enabled:
        return None

    apns_tokens: dict[str, ApnsTokenSource] = {}
    if settings.apns_key_path and settings.apns_key_id:
        apns_tokens[PushEnvironment.PRODUCTION.value] = ApnsTokenSource(
            settings.apns_key_path, settings.apns_key_id, settings.apns_team_id
        )
    if settings.apns_sandbox_key_path and settings.apns_sandbox_key_id:
        apns_tokens[PushEnvironment.SANDBOX.value] = ApnsTokenSource(
            settings.apns_sandbox_key_path,
            settings.apns_sandbox_key_id,
            settings.apns_team_id,
        )
    apns = ApnsSender(apns_tokens) if apns_tokens else None

    fcm = None
    if settings.fcm_service_account_path:
        fcm = FcmSender(settings.fcm_service_account_path, settings.fcm_project_id)

    if apns is None and fcm is None:
        logger.warning("[push] Enabled but no credentials configured")
        return None

    return PushSender(apns, fcm)
