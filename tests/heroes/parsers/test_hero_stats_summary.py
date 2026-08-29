from copy import deepcopy
from unittest.mock import Mock, patch

import pytest

from app.adapters.blizzard import BlizzardClient
from app.domain.enums import (
    CompetitiveDivision,
    HeroKey,
    PlayerGamemode,
    PlayerPlatform,
    PlayerRegion,
)
from app.domain.exceptions import (
    InvalidGamemodeFilterError,
    ParserBlizzardError,
    ParserParsingError,
)
from app.domain.parsers.hero_stats_summary import (
    PLATFORM_MAPPING,
    parse_hero_stats_json,
    parse_hero_stats_summary,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_kwargs", "raises_error"),
    [
        ({}, False),
        ({"competitive_division": CompetitiveDivision.DIAMOND}, False),
        ({"map_filter": "hanaoka"}, True),
    ],
)
async def test_parse_hero_stats_summary(
    extra_kwargs: dict,
    raises_error: bool,
    hero_stats_response_mock: Mock,
):
    base_kwargs = {
        "platform": PlayerPlatform.PC,
        "gamemode": PlayerGamemode.COMPETITIVE,
        "gamemode_filter": "1",
        "region": PlayerRegion.EUROPE,
        "order_by": "hero:asc",
    }

    with patch("httpx2.AsyncClient.get", return_value=hero_stats_response_mock):
        client = BlizzardClient()
        if raises_error:
            with pytest.raises(ParserBlizzardError):
                await parse_hero_stats_summary(client, **base_kwargs, **extra_kwargs)
        else:
            result = await parse_hero_stats_summary(
                client,
                **base_kwargs,
                **extra_kwargs,
            )
            assert isinstance(result, list)
            assert len(result) > 0
            assert "hero" in result[0]


@pytest.mark.asyncio
async def test_parse_hero_stats_summary_query_params(hero_stats_response_mock: Mock):
    """Verify the exact Blizzard query parameters built by the parser."""
    platform = PlayerPlatform.PC
    gamemode = PlayerGamemode.COMPETITIVE
    region = PlayerRegion.EUROPE
    division = CompetitiveDivision.DIAMOND
    map_key = "all-maps"

    with patch(
        "httpx2.AsyncClient.get", return_value=hero_stats_response_mock
    ) as mock_get:
        client = BlizzardClient()
        await parse_hero_stats_summary(
            client,
            platform=platform,
            gamemode=gamemode,
            gamemode_filter="1",
            region=region,
            competitive_division=division,
            map_filter=map_key,
            order_by="hero:asc",
        )

    mock_get.assert_called_once()
    _, kwargs = mock_get.call_args
    params = kwargs.get("params", {})

    assert params["input"] == PLATFORM_MAPPING[platform]
    assert params["rq"] == "1"
    assert params["region"] == region.capitalize()
    assert params["map"] == map_key
    assert params["tier"] == division.capitalize()


@pytest.mark.asyncio
async def test_parse_hero_stats_summary_invalid_map_error_message(
    hero_stats_response_mock: Mock,
):
    """ParserBlizzardError message should name the incompatible map."""
    with patch("httpx2.AsyncClient.get", return_value=hero_stats_response_mock):
        client = BlizzardClient()
        with pytest.raises(ParserBlizzardError) as exc_info:
            await parse_hero_stats_summary(
                client,
                platform=PlayerPlatform.PC,
                gamemode=PlayerGamemode.COMPETITIVE,
                gamemode_filter="1",
                region=PlayerRegion.EUROPE,
                map_filter="hanaoka",
            )

    message = str(exc_info.value.message)

    assert "hanaoka" in message
    assert "compatible" in message.lower()


def test_parse_hero_stats_json_raises_invalid_gamemode_filter_error():
    json_data = {"rates": {"selected": {"map": "all-maps", "rq": "1"}, "rates": []}}

    with pytest.raises(InvalidGamemodeFilterError) as exc_info:
        parse_hero_stats_json(
            json_data,
            map_filter="all-maps",
            gamemode=PlayerGamemode.COMPETITIVE,
            gamemode_filter="2",
        )

    assert "2" in exc_info.value.message
    assert "1" in exc_info.value.message


def _build_stats_json(columns: list[dict] | None, banrate: float = 12.7) -> dict:
    """Build a minimal Blizzard-shaped payload with a single hero entry."""
    payload = {
        "rates": {
            "selected": {"map": "all-maps", "rq": "2"},
            "rates": [
                {
                    "id": "ana",
                    "cells": {
                        "name": "Ana",
                        "pickrate": 25.1,
                        "winrate": 48.5,
                        "banrate": banrate,
                    },
                    "hero": {"role": "SUPPORT"},
                }
            ],
        }
    }
    if columns is not None:
        payload["columns"] = columns
    return payload


@pytest.mark.parametrize(
    ("columns", "expected_banrate"),
    [
        # Blizzard declares banrate for gamemodes featuring hero bans
        (
            [{"id": "name"}, {"id": "pickrate"}, {"id": "winrate"}, {"id": "banrate"}],
            12.7,
        ),
        # Quickplay : cells still carry a constant 0 banrate, but it's meaningless
        ([{"id": "name"}, {"id": "pickrate"}, {"id": "winrate"}], None),
        # Defensive : older/unexpected payloads without usable columns
        ([], None),
        (None, None),
    ],
)
def test_parse_hero_stats_json_banrate(
    columns: list[dict] | None, expected_banrate: float | None
):
    result = parse_hero_stats_json(
        _build_stats_json(columns),
        map_filter="all-maps",
        gamemode=PlayerGamemode.COMPETITIVE,
        gamemode_filter="2",
    )

    assert result[0]["banrate"] == expected_banrate


def test_parse_hero_stats_json_banrate_normalizes_unavailable_value():
    """Blizzard uses -1 for unavailable data, like it does for other rates."""
    result = parse_hero_stats_json(
        _build_stats_json([{"id": "banrate"}], banrate=-1),
        map_filter="all-maps",
        gamemode=PlayerGamemode.COMPETITIVE,
        gamemode_filter="2",
    )

    assert result[0]["banrate"] == 0.0


@pytest.mark.parametrize(
    "json_data",
    [
        {},
        {"rates": None},
        {"rates": {"selected": {}, "rates": []}},
        {"rates": {"selected": {"map": "all-maps"}, "rates": [{"id": "ana"}]}},
    ],
)
def test_parse_hero_stats_json_raises_parsing_error_on_unexpected_structure(
    json_data: dict,
):
    with pytest.raises(ParserParsingError):
        parse_hero_stats_json(
            json_data,
            map_filter="all-maps",
            gamemode=PlayerGamemode.QUICKPLAY,
            gamemode_filter="0",
        )


class TestOrderingWithNullBanrate:
    """banrate is None in gamemodes without hero bans, and None is not
    comparable to a float — ordering by it must not raise."""

    @staticmethod
    def _two_heroes(columns: list[dict] | None) -> dict:
        payload = _build_stats_json(columns)
        payload["rates"]["rates"].append(
            {
                "id": "genji",
                "cells": {
                    "name": "Genji",
                    "pickrate": 10.0,
                    "winrate": 45.0,
                    "banrate": 5.0,
                },
                "hero": {"role": "DAMAGE"},
            }
        )
        return payload

    @staticmethod
    def _parse(payload: dict, order_by: str) -> list[dict]:
        return parse_hero_stats_json(
            payload,
            map_filter="all-maps",
            gamemode=PlayerGamemode.COMPETITIVE,
            gamemode_filter="2",
            order_by=order_by,
        )

    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_ordering_by_undeclared_banrate_does_not_raise(self, direction: str):
        """Quickplay shape: no banrate column, so every value is None."""
        payload = self._two_heroes([{"id": "pickrate"}, {"id": "winrate"}])

        result = self._parse(payload, f"banrate:{direction}")

        assert [stat["hero"] for stat in result] == ["ana", "genji"]
        assert all(stat["banrate"] is None for stat in result)

    def test_ordering_by_banrate_descending(self):
        payload = self._two_heroes([{"id": "banrate"}])

        result = self._parse(payload, "banrate:desc")

        assert [stat["hero"] for stat in result] == ["ana", "genji"]

    def test_ordering_by_banrate_ascending(self):
        payload = self._two_heroes([{"id": "banrate"}])

        result = self._parse(payload, "banrate:asc")

        assert [stat["hero"] for stat in result] == ["genji", "ana"]


def test_unknown_hero_is_skipped_not_fatal(hero_stats_json_data: dict):
    """HeroStatsSummary.hero is typed HeroKey, so one hero Blizzard ranks before
    we add the CSV row used to 500 the whole endpoint."""
    payload = deepcopy(hero_stats_json_data)
    payload["rates"]["rates"][0]["id"] = "brandnew"

    result = parse_hero_stats_json(
        payload,
        map_filter="all-maps",
        gamemode=PlayerGamemode.COMPETITIVE,
        gamemode_filter=payload["rates"]["selected"]["rq"],
    )
    keys = [row["hero"] for row in result]

    assert "brandnew" not in keys
    assert len(keys) == len(payload["rates"]["rates"]) - 1
    assert all(key in iter(HeroKey) for key in keys)
