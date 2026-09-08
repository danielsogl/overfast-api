"""Push subscription endpoints : device registration for rank alerts.

A subscription is a device saying "tell me when one of these players changes
rank". It carries no account and no personal data beyond the FCM token itself;
the players are public profiles anyone can look up here anyway.

The registration is an upsert the app repeats on every launch. That is what
keeps the table rebuildable — see the note above ``push_subscriptions`` in
``schema.sql`` — and it doubles as the liveness stamp that prunes devices which
uninstalled the app.
"""

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.dependencies import StorageDep
from app.api.enums import RouteTag
from app.api.models.push import PushSubscription, PushSubscriptionAck

router = APIRouter()


@router.put(
    "/subscriptions",
    response_model=PushSubscriptionAck,
    tags=[RouteTag.PUSH],
    summary="Register a device for rank alerts",
    description=(
        "Register or refresh the set of players a device wants rank-change "
        "notifications for. The list replaces whatever was stored for this "
        "token, so the app sends its full roster rather than a delta.<br />"
        "Safe to repeat : the app is expected to call this on every launch, "
        "which is also how a subscription stays alive. One that nobody "
        "refreshes is pruned."
    ),
)
async def register_push_subscription(
    subscription: PushSubscription,
    storage: StorageDep,
) -> PushSubscriptionAck:
    await storage.upsert_push_subscription(
        subscription.token,
        subscription.platform.value,
        subscription.locale,
        subscription.player_ids,
    )
    return PushSubscriptionAck(watched_players=len(subscription.player_ids))


@router.delete(
    "/subscriptions/{token}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=[RouteTag.PUSH],
    summary="Unregister a device",
    description=(
        "Drop a device's subscription. Returns 204 whether or not a row "
        "existed : the caller's intent is 'do not notify this token', and an "
        "already-absent token satisfies it."
    ),
)
async def delete_push_subscription(
    storage: StorageDep,
    token: Annotated[
        str,
        Path(
            title="FCM registration token",
            min_length=8,
            max_length=4096,
        ),
    ],
) -> None:
    await storage.delete_push_subscription(token)
