"""Route tests for the hero stats history endpoint"""

import datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from fastapi import status

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.fake_storage import FakeStorage

_TODAY = datetime.datetime.now(tz=datetime.UTC).date()
_YESTERDAY = _TODAY - datetime.timedelta(days=1)


@pytest_asyncio.fixture
async def _recorded_history(storage_db: FakeStorage):
    await storage_db.add_hero_stats_snapshot(
        _YESTERDAY,
        "pc",
        "competitive",
        "europe",
        [{"hero": "mercy", "winrate": 48.0, "pickrate": 5.5, "banrate": None}],
    )
    await storage_db.add_hero_stats_snapshot(
        _TODAY,
        "pc",
        "competitive",
        "europe",
        [
            {"hero": "ana", "winrate": 52.1, "pickrate": 8.3, "banrate": 12.7},
            {"hero": "mercy", "winrate": 49.0, "pickrate": 6.0, "banrate": None},
        ],
    )


@pytest.mark.usefixtures("_recorded_history")
def test_get_hero_stats_history(client: TestClient):
    response = client.get("/heroes/stats/history", params={"region": "europe"})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["region"] == "europe"
    assert [snapshot["taken_on"] for snapshot in body["snapshots"]] == [
        _TODAY.isoformat(),
        _YESTERDAY.isoformat(),
    ]
    assert body["snapshots"][0]["stats"][0] == {
        "hero": "ana",
        "winrate": 52.1,
        "pickrate": 8.3,
        "banrate": 12.7,
    }


@pytest.mark.usefixtures("_recorded_history")
def test_get_hero_stats_history_filtered_by_hero(client: TestClient):
    response = client.get(
        "/heroes/stats/history", params={"region": "europe", "hero": "ana"}
    )

    assert response.status_code == status.HTTP_200_OK
    snapshots = response.json()["snapshots"]
    # Only the day Ana was recorded on, and only her row
    assert len(snapshots) == 1
    assert snapshots[0]["taken_on"] == _TODAY.isoformat()
    assert [row["hero"] for row in snapshots[0]["stats"]] == ["ana"]


@pytest.mark.usefixtures("_recorded_history")
def test_get_hero_stats_history_is_capped_by_limit(client: TestClient):
    response = client.get(
        "/heroes/stats/history", params={"region": "europe", "limit": 1}
    )

    assert response.status_code == status.HTTP_200_OK
    assert [snapshot["taken_on"] for snapshot in response.json()["snapshots"]] == [
        _TODAY.isoformat()
    ]


@pytest.mark.usefixtures("_recorded_history")
def test_get_hero_stats_history_since_filters_older_days(client: TestClient):
    since = int(
        datetime.datetime.combine(
            _TODAY, datetime.time(), tzinfo=datetime.UTC
        ).timestamp()
    )

    response = client.get(
        "/heroes/stats/history", params={"region": "europe", "since": since}
    )

    assert [snapshot["taken_on"] for snapshot in response.json()["snapshots"]] == [
        _TODAY.isoformat()
    ]


@pytest.mark.usefixtures("_recorded_history")
def test_get_hero_stats_history_of_an_unrecorded_region_is_empty(
    client: TestClient,
):
    """Nothing recorded is a state to render, not a 404."""
    response = client.get("/heroes/stats/history", params={"region": "asia"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"region": "asia", "snapshots": []}


def test_get_hero_stats_history_requires_a_region(client: TestClient):
    response = client.get("/heroes/stats/history")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    "query",
    [
        "region=europe&hero=unknown-hero",
        "region=europe&limit=0",
        "region=europe&limit=366",
        "region=europe&since=0",
        "region=atlantis",
    ],
)
def test_get_hero_stats_history_rejects_invalid_query(client: TestClient, query: str):
    response = client.get(f"/heroes/stats/history?{query}")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.usefixtures("_recorded_history")
@pytest.mark.parametrize("query", ["platform=console", "gamemode=quickplay"])
def test_get_hero_stats_history_has_no_platform_or_gamemode_filter(
    client: TestClient, query: str
):
    """Only the canonical slice is recorded, so these are not parameters —
    passing them changes nothing rather than returning a slice we never took."""
    response = client.get(f"/heroes/stats/history?region=europe&{query}")

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["snapshots"]) == 2  # noqa: PLR2004
