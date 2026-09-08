"""Bearer tokens for the two push services.

Both want a short-lived JWT the sender signs itself — ES256 with an Apple push
key for APNs, RS256 with a Google service account for FCM (exchanged for an
OAuth access token). Both are cached: APNs rejects a token refreshed more than
once every 20 minutes, and re-signing per notification would be pure waste.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx2
import jwt

from app.infrastructure.logger import logger

# Apple accepts a token for 60 minutes and refuses one refreshed sooner than
# every 20; the midpoint keeps well clear of both edges.
_APNS_TOKEN_TTL = 40 * 60
# Google issues one-hour tokens; renew a minute early rather than race the edge.
_GOOGLE_TOKEN_TTL = 59 * 60

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class _CachedToken:
    """A bearer token plus the moment it stops being reusable."""

    def __init__(self) -> None:
        self._value: str | None = None
        self._expires_at: float = 0.0

    def get(self) -> str | None:
        if self._value is not None and time.time() < self._expires_at:
            return self._value
        return None

    def set(self, value: str, ttl: float) -> str:
        self._value = value
        self._expires_at = time.time() + ttl
        return value


class ApnsTokenSource:
    """Signs and caches the ES256 provider token APNs authenticates with."""

    def __init__(self, key_path: str, key_id: str, team_id: str) -> None:
        self._key = Path(key_path).read_text()
        self._key_id = key_id
        self._team_id = team_id
        self._cached = _CachedToken()

    def get(self) -> str:
        cached = self._cached.get()
        if cached is not None:
            return cached

        token = jwt.encode(
            {"iss": self._team_id, "iat": int(time.time())},
            self._key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        return self._cached.set(token, _APNS_TOKEN_TTL)


class GoogleTokenSource:
    """Exchanges a service account assertion for an FCM access token."""

    def __init__(self, service_account_path: str) -> None:
        self._account = json.loads(Path(service_account_path).read_text())
        self._cached = _CachedToken()

    async def get(self, client: httpx2.AsyncClient) -> str | None:
        cached = self._cached.get()
        if cached is not None:
            return cached

        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self._account["client_email"],
                "scope": _FCM_SCOPE,
                "aud": self._account["token_uri"],
                "iat": now,
                "exp": now + 3600,
            },
            self._account["private_key"],
            algorithm="RS256",
        )

        response = await client.post(
            self._account["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        if response.status_code != httpx2.codes.OK:
            logger.warning(
                "[push] FCM token exchange failed ({}): {}",
                response.status_code,
                response.text[:200],
            )
            return None

        payload = response.json()
        return self._cached.set(payload["access_token"], _GOOGLE_TOKEN_TTL)
