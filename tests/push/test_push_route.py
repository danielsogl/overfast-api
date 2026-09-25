"""Tests for the push subscription endpoints"""

from typing import TYPE_CHECKING

import pytest
from fastapi import status

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.fake_storage import FakeStorage

REGISTRATION_ID = "fMEP0vJqS0y-test-device-0000000000000"


def _body(**overrides) -> dict:
    return {
        "token": REGISTRATION_ID,
        "platform": "ios",
        "locale": "de",
        "player_ids": ["TeKrop-2217"],
        **overrides,
    }


@pytest.mark.asyncio
async def test_register_stores_the_subscription(
    client: TestClient, storage_db: FakeStorage
):
    response = client.put("/push/subscriptions", json=_body())

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"watched_players": 1}
    assert await storage_db.get_push_subscribed_player_ids() == ["TeKrop-2217"]


@pytest.mark.asyncio
async def test_the_shipped_payload_still_validates_without_the_new_fields(
    client: TestClient, storage_db: FakeStorage
):
    """A device that never sends recap_player_id/timezone must keep working —
    the app calls this on every launch and must never see a 422 for it."""
    response = client.put("/push/subscriptions", json=_body())

    assert response.status_code == status.HTTP_200_OK
    subscriptions = await storage_db.get_push_subscriptions()
    assert subscriptions[0]["recap_player_id"] is None
    assert subscriptions[0]["timezone"] is None


@pytest.mark.asyncio
async def test_register_stores_the_recap_opt_in(
    client: TestClient, storage_db: FakeStorage
):
    response = client.put(
        "/push/subscriptions",
        json=_body(recap_player_id="TeKrop-2217", timezone="Europe/Berlin"),
    )

    assert response.status_code == status.HTTP_200_OK
    subscriptions = await storage_db.get_push_subscriptions()
    assert subscriptions[0]["recap_player_id"] == "TeKrop-2217"
    assert subscriptions[0]["timezone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_an_unresolvable_timezone_is_still_accepted(client: TestClient):
    """Rejecting an unknown IANA name here would also cost the device its
    rank alerts; resolution and its UTC fallback happen at send time."""
    response = client.put("/push/subscriptions", json=_body(timezone="Not/ARealZone"))

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.asyncio
async def test_register_replaces_the_previous_roster(
    client: TestClient, storage_db: FakeStorage
):
    """The app re-sends its full roster, so a shrunk list must shrink here."""
    client.put("/push/subscriptions", json=_body(player_ids=["A-1", "B-2"]))
    client.put("/push/subscriptions", json=_body(player_ids=["B-2"]))

    assert await storage_db.get_push_subscribed_player_ids() == ["B-2"]


@pytest.mark.asyncio
async def test_subscribers_are_looked_up_per_player(
    client: TestClient, storage_db: FakeStorage
):
    client.put("/push/subscriptions", json=_body(player_ids=["A-1"]))
    client.put(
        "/push/subscriptions",
        json=_body(
            token=f"{REGISTRATION_ID}2", platform="android", player_ids=["A-1", "B-2"]
        ),
    )

    watchers = await storage_db.get_push_subscriptions_for_player("A-1")
    assert {w["token"] for w in watchers} == {REGISTRATION_ID, f"{REGISTRATION_ID}2"}
    assert [
        w["token"] for w in await storage_db.get_push_subscriptions_for_player("B-2")
    ] == [f"{REGISTRATION_ID}2"]


@pytest.mark.asyncio
async def test_delete_removes_the_subscription(
    client: TestClient, storage_db: FakeStorage
):
    client.put("/push/subscriptions", json=_body())

    response = client.delete(f"/push/subscriptions/{REGISTRATION_ID}")

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert await storage_db.get_push_subscribed_player_ids() == []


def test_delete_is_idempotent(client: TestClient):
    """The caller's intent is 'do not notify this token', not 'a row existed'."""
    response = client.delete(f"/push/subscriptions/{REGISTRATION_ID}")

    assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platform", "windows"),
        ("locale", "not a locale"),
        ("player_ids", []),
        ("player_ids", [f"P-{i}" for i in range(51)]),
        ("token", "short"),
    ],
)
def test_rejects_invalid_input(client: TestClient, field: str, value):
    response = client.put("/push/subscriptions", json=_body(**{field: value}))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
