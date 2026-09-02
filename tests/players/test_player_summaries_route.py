import asyncio
from fnmatch import fnmatch
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException, Response, status

from app.api.routers.players import MAX_BATCH_SUMMARIES, get_players_summaries
from app.config import settings
from app.domain.exceptions import ParserBlizzardError, ParserInternalError
from tests.helpers import read_html_file

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

SUMMARY_METHOD = "app.domain.services.player_service.PlayerService.get_player_summary"


def build_summary(username: str) -> dict:
    return {
        "username": username,
        "avatar": None,
        "namecard": None,
        "title": None,
        "endorsement": None,
        "competitive": None,
        "last_updated_at": None,
    }


def test_get_players_summaries(client: TestClient, player_search_response_mock: Mock):
    html_by_player = {
        player_id: read_html_file(f"players/{player_id}.html")
        for player_id in ("TeKrop-2217", "KIRIKO-12460")
    }

    def blizzard_dispatch(url: str, *_args, **_kwargs) -> Mock:
        if settings.search_account_path in url:
            return player_search_response_mock
        html = next(html for pid, html in html_by_player.items() if pid in url)
        return Mock(status_code=status.HTTP_200_OK, text=html, url=url)

    with patch("httpx2.AsyncClient.get", side_effect=blizzard_dispatch):
        response = client.get("/players/summaries?ids=TeKrop-2217,KIRIKO-12460")

    assert response.status_code == status.HTTP_200_OK
    results = response.json()["results"]
    assert [result["player_id"] for result in results] == [
        "TeKrop-2217",
        "KIRIKO-12460",
    ]
    assert [result["error"] for result in results] == [None, None]
    assert [result["summary"]["username"] for result in results] == ["TeKrop", "KIRIKO"]
    assert [result["pending"] for result in results] == [False, False]
    assert response.headers["X-Cache-Status"] == "hit"


def test_get_players_summaries_partial_failure(client: TestClient):
    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "Unknown-1234":
            raise ParserBlizzardError(
                status_code=HTTPStatus.NOT_FOUND.value, message="Player not found"
            )
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect):
        response = client.get("/players/summaries?ids=TeKrop-2217,Unknown-1234")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "results": [
            {
                "player_id": "TeKrop-2217",
                "summary": build_summary("TeKrop"),
                "error": None,
                "pending": False,
            },
            {
                "player_id": "Unknown-1234",
                "summary": None,
                "error": {
                    "status_code": status.HTTP_404_NOT_FOUND,
                    "message": "Player not found",
                },
                "pending": False,
            },
        ]
    }


@pytest.mark.parametrize(
    ("exception", "expected_error"),
    [
        (
            ValueError("boom"),
            {
                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "message": settings.internal_server_error_message,
            },
        ),
        (
            ParserInternalError(
                "https://blizzard.test/career/Broken-1234/", KeyError()
            ),
            {
                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "message": settings.internal_server_error_message,
            },
        ),
        (
            HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Couldn't get Blizzard page (HTTP 503 error)",
            ),
            {
                "status_code": status.HTTP_504_GATEWAY_TIMEOUT,
                "message": "Couldn't get Blizzard page (HTTP 503 error)",
            },
        ),
    ],
)
def test_get_players_summaries_error_is_isolated(
    client: TestClient, exception: Exception, expected_error: dict
):
    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "Broken-1234":
            raise exception
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect):
        response = client.get("/players/summaries?ids=TeKrop-2217,Broken-1234")

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["results"][0]["summary"] == build_summary("TeKrop")
    assert response.json()["results"][1]["error"] == expected_error


def test_get_players_summaries_duplicates_are_collapsed(client: TestClient):
    async def summary_side_effect(**_kwargs) -> tuple:
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect) as summary_mock:
        response = client.get(
            "/players/summaries?ids=TeKrop-2217, TeKrop-2217 ,KIRIKO-12460,TeKrop-2217"
        )

    assert response.status_code == status.HTTP_200_OK
    assert [result["player_id"] for result in response.json()["results"]] == [
        "TeKrop-2217",
        "KIRIKO-12460",
    ]
    assert summary_mock.await_count == len(response.json()["results"])


def test_get_players_summaries_staleness_is_the_stalest_member(client: TestClient):
    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "KIRIKO-12460":
            return build_summary("KIRIKO"), True, 1800
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect):
        response = client.get("/players/summaries?ids=TeKrop-2217,KIRIKO-12460")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["X-Cache-Status"] == "stale"
    assert response.headers["Age"] == "1800"


@pytest.mark.parametrize(
    "ids",
    [
        "",
        " , ",
        ",".join(f"Player-{index}" for index in range(MAX_BATCH_SUMMARIES + 1)),
    ],
)
def test_get_players_summaries_invalid_ids(client: TestClient, ids: str):
    response = client.get("/players/summaries", params={"ids": ids})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_get_players_summaries_missing_ids(client: TestClient):
    response = client.get("/players/summaries")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.parametrize(
    "player_id",
    [
        "TeKrop-2217",
        "d65ba781fe23cdfabe%7C9b3063608098cdbf1b9825d8664f4d96",
    ],
)
def test_get_players_summaries_cache_key_matches_single_route(
    client: TestClient, player_id: str
):
    cache_keys = []

    async def summary_side_effect(cache_key: str, **_kwargs) -> tuple:
        cache_keys.append(cache_key)
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect):
        client.get(f"/players/{player_id}/summary")
        client.get(f"/players/summaries?ids={player_id}")

    single_route_key, batch_key = cache_keys

    assert batch_key == single_route_key


def test_get_players_summaries_cache_key_is_evictable(client: TestClient):
    cache_keys = []

    async def summary_side_effect(cache_key: str, **_kwargs) -> tuple:
        cache_keys.append(cache_key)
        return build_summary("TeKrop"), False, 0

    with patch(SUMMARY_METHOD, side_effect=summary_side_effect):
        client.get("/players/summaries?ids=TeKrop-2217")

    evict_pattern = f"{settings.api_cache_key_prefix}:/players/TeKrop-2217*"

    assert cache_keys == ["/players/TeKrop-2217/summary"]
    assert fnmatch(f"{settings.api_cache_key_prefix}:{cache_keys[0]}", evict_pattern)


def test_get_players_summaries_slow_player_is_pending_not_failed(client: TestClient):
    """A player still in flight when the budget elapses comes back pending,
    not as an error, and the fast player next to it is unaffected."""

    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "Slow-9999":
            await asyncio.sleep(3600)  # never resolves within the test budget
        return build_summary("TeKrop"), False, 0

    with (
        patch(SUMMARY_METHOD, side_effect=summary_side_effect),
        patch("app.api.routers.players.BATCH_SUMMARIES_TIMEOUT_SECONDS", 0.05),
    ):
        response = client.get("/players/summaries?ids=TeKrop-2217,Slow-9999")

    assert response.status_code == status.HTTP_200_OK
    results = {result["player_id"]: result for result in response.json()["results"]}

    assert results["TeKrop-2217"] == {
        "player_id": "TeKrop-2217",
        "summary": build_summary("TeKrop"),
        "error": None,
        "pending": False,
    }
    assert results["Slow-9999"] == {
        "player_id": "Slow-9999",
        "summary": None,
        "error": None,
        "pending": True,
    }


# These two tests call the route coroutine directly rather than through
# TestClient : TestClient tears down its portal/event loop at the end of each
# request (unless entered via `with client:`, which in turn runs the app's
# real lifespan and tries to reach a live Postgres). Calling the coroutine on
# pytest-asyncio's own loop keeps that loop alive past the `await`, which is
# what a background task actually needs to prove it wasn't cancelled.
@pytest.mark.asyncio
async def test_get_players_summaries_slow_player_keeps_running_in_background():
    """The task for a player that missed the budget is not cancelled : it
    keeps running (and completing) after the response has been built."""
    finished = asyncio.Event()

    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "Slow-9999":
            await asyncio.sleep(0.05)
            finished.set()
        return build_summary("TeKrop"), False, 0

    service = Mock()
    service.get_player_summary = AsyncMock(side_effect=summary_side_effect)

    with patch("app.api.routers.players.BATCH_SUMMARIES_TIMEOUT_SECONDS", 0.001):
        result = await get_players_summaries(
            response=Response(), service=service, ids="Slow-9999"
        )

    assert result["results"][0]["pending"] is True

    # Not cancelled : the background task actually completes after
    # get_players_summaries already returned its response.
    await asyncio.wait_for(finished.wait(), timeout=2.0)


@pytest.mark.asyncio
async def test_get_players_summaries_background_failure_is_not_raised():
    """A background task that later raises must not surface as an unhandled
    'exception was never retrieved' warning — this just has to not blow up."""
    raised = asyncio.Event()

    async def summary_side_effect(player_id: str, **_kwargs) -> tuple:
        if player_id == "Broken-9999":
            await asyncio.sleep(0.05)
            raised.set()
            msg = "boom after the budget"
            raise ValueError(msg)
        return build_summary("TeKrop"), False, 0

    service = Mock()
    service.get_player_summary = AsyncMock(side_effect=summary_side_effect)

    with patch("app.api.routers.players.BATCH_SUMMARIES_TIMEOUT_SECONDS", 0.001):
        result = await get_players_summaries(
            response=Response(), service=service, ids="Broken-9999"
        )

    assert result["results"][0]["pending"] is True

    # Let the background task actually raise ; if its exception weren't
    # drained by _forget_background_task this would only ever surface as a
    # logged warning during garbage collection, never a test failure — the
    # real assertion is that awaiting past this point doesn't raise/hang.
    await asyncio.wait_for(raised.wait(), timeout=2.0)
