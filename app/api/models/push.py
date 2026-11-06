"""Pydantic models for the push subscription endpoints"""

import re
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import PushEnvironment, PushPlatform

# One row per device, and the app caps a free user at 5 followed players and a
# subscriber at 50. Anything past that is not a real roster.
MAX_WATCHED_PLAYERS = 50

# An APNs device token is hex, and the APNs sender puts it into the request
# path, so anything else could rewrite that URL. FCM tokens travel in a JSON
# body and are left alone: their format is not documented closely enough to
# reject one safely, and a rejected registration costs every alert silently.
_APNS_TOKEN = re.compile(r"^[0-9A-Fa-f]+$")

PlayerId = Annotated[str, Field(min_length=1, max_length=256)]


class PushSubscription(BaseModel):
    token: str = Field(
        ...,
        description=(
            "FCM registration token of the device. Treated as a device "
            "identifier : it is never logged in full and no read path returns "
            "it."
        ),
        min_length=8,
        max_length=4096,
        examples=["fMEP0vJqS0y…"],
    )
    platform: PushPlatform = Field(
        ...,
        description="Platform the token was issued for",
        examples=["ios"],
    )
    locale: str = Field(
        "en-US",
        description=(
            "Language the notification text should be composed in. Unknown or "
            "unsupported values fall back to English rather than failing the "
            "registration — a device that cannot be reached in its own "
            "language is still worth reaching."
        ),
        pattern=r"^[a-z]{2}(-[A-Za-z]{2,4})?$",
        max_length=10,
        examples=["de", "zh-Hans"],
    )
    environment: PushEnvironment = Field(
        PushEnvironment.PRODUCTION,
        description=(
            "Which APNs environment the token belongs to. A property of the "
            "build : a development-provisioned binary registers against "
            "sandbox, a TestFlight or App Store one against production, and "
            "APNs rejects a token on the host it does not belong to. Ignored "
            "for Android, where FCM has one environment."
        ),
        examples=["production"],
    )
    player_ids: list[PlayerId] = Field(
        ...,
        description=(
            "Players this device wants rank alerts for. Replaces the previous "
            "list wholesale : the app re-sends the full set on every launch, "
            "which is also what keeps this table rebuildable."
        ),
        min_length=1,
        max_length=MAX_WATCHED_PLAYERS,
        examples=[["TeKrop-2217"]],
    )
    recap_player_id: str | None = Field(
        None,
        description=(
            "Player the opt-in weekly recap should summarise, usually the "
            "device's own. Omit to stay opted out : a device that never sends "
            "this is never sent a recap."
        ),
        min_length=1,
        max_length=256,
        examples=["TeKrop-2217"],
    )
    timezone: str | None = Field(
        None,
        description=(
            "IANA timezone name (e.g. `Europe/Berlin`) the weekly recap is "
            "timed against. Only shape-checked, not resolved : rejecting a "
            "zone our tzdata doesn't know would fail the whole registration "
            "and cost the device its rank alerts too, so an unresolvable "
            "value quietly falls back to UTC at send time instead."
        ),
        pattern=r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$",
        max_length=64,
        examples=["Europe/Berlin"],
    )

    @model_validator(mode="after")
    def _apns_token_is_hex(self) -> Self:
        if self.platform == PushPlatform.IOS and not _APNS_TOKEN.match(self.token):
            msg = "An iOS token must be the hex APNs device token"
            raise ValueError(msg)
        return self


class PushSubscriptionAck(BaseModel):
    watched_players: int = Field(
        ...,
        description="Number of players now registered for this device",
        examples=[3],
        ge=0,
    )
