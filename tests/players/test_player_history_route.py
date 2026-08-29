"""Route tests for the player snapshot history and diff endpoints"""

from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest
from fastapi import status

from tests.helpers import players_ids, read_html_file

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_TEKROP_HTML = read_html_file("players/TeKrop-2217.html") or ""


def _blizzard_calls(player_html_data: str, player_search_response_mock: Mock) -> list:
    return [
        # Players search call first
        player_search_response_mock,
        # Player profile page
        Mock(status_code=status.HTTP_200_OK, text=player_html_data),
    ]


@pytest.mark.parametrize(
    ("player_id", "player_html_data"),
    [(player_id, player_id) for player_id in players_ids],
    indirect=["player_html_data"],
)
def test_get_player_history(
    client: TestClient,
    player_id: str,
    player_html_data: str,
    player_search_response_mock: Mock,
):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=_blizzard_calls(player_html_data, player_search_response_mock),
    ):
        response = client.get(f"/players/{player_id}/history")

    assert response.status_code == status.HTTP_200_OK
    snapshots = response.json()["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["taken_at"] > 0
    assert snapshots[0]["data"]["heroes"]


def test_get_player_history_is_capped_by_limit(
    client: TestClient,
    player_search_response_mock: Mock,
):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=_blizzard_calls(_TEKROP_HTML, player_search_response_mock),
    ):
        response = client.get("/players/TeKrop-2217/history?limit=1")

    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["snapshots"]) == 1


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=501", "since=0", "since=abc"],
)
def test_get_player_history_rejects_invalid_query(client: TestClient, query: str):
    response = client.get(f"/players/TeKrop-2217/history?{query}")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_player_history_blizzard_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get("/players/TeKrop-2217/history")

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT


def test_get_player_stats_diff_without_history(
    client: TestClient,
    player_search_response_mock: Mock,
):
    """A player we only just started recording is a 200, not a 404."""
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=_blizzard_calls(_TEKROP_HTML, player_search_response_mock),
    ):
        response = client.get("/players/TeKrop-2217/stats/diff")

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["snapshots_compared"] == 1
    assert data["compared_from"] is None
    assert data["compared_to"] is None
    assert data["ranks"] == []
    assert data["heroes"] == []
    assert data["totals"] == {"time_played": 0, "games_won": 0}


def test_get_player_stats_diff_accepts_since(
    client: TestClient,
    player_search_response_mock: Mock,
):
    with patch(
        "httpx2.AsyncClient.get",
        side_effect=_blizzard_calls(_TEKROP_HTML, player_search_response_mock),
    ):
        response = client.get("/players/TeKrop-2217/stats/diff?since=1739547600")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["since"] == 1739547600  # noqa: PLR2004


def test_get_player_stats_diff_rejects_an_invalid_since(client: TestClient):
    response = client.get("/players/TeKrop-2217/stats/diff?since=0")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_player_stats_diff_blizzard_error(client: TestClient):
    with patch(
        "httpx2.AsyncClient.get",
        return_value=Mock(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            text="Service Unavailable",
        ),
    ):
        response = client.get("/players/TeKrop-2217/stats/diff")

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
