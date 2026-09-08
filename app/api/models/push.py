"""Pydantic models for the push subscription endpoints"""

from pydantic import BaseModel, Field

from app.domain.enums import PushPlatform

# One row per device, and the app caps a free user at 5 followed players and a
# subscriber at 50. Anything past that is not a real roster.
MAX_WATCHED_PLAYERS = 50


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
    player_ids: list[str] = Field(
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


class PushSubscriptionAck(BaseModel):
    watched_players: int = Field(
        ...,
        description="Number of players now registered for this device",
        examples=[3],
        ge=0,
    )
