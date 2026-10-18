"""Tests for app/domain/parsers/player_search.py — error paths and edge cases"""

import pytest

from app.domain.exceptions import ParserParsingError
from app.domain.parsers.player_search import (
    apply_ordering,
    filter_players_by_name,
    prioritize_exact_matches,
    transform_player_search_results,
)

# ── filter_players_by_name ────────────────────────────────────────────────────


class TestFilterPlayersByName:
    def test_returns_matching_public_players(self):
        players = [
            {"name": "Test", "isPublic": True},
            {"name": "Other", "isPublic": True},
            {"name": "Test", "isPublic": False},
        ]
        result = filter_players_by_name(players, "Test")

        assert len(result) == 1
        assert result[0]["name"] == "Test"
        assert result[0]["isPublic"] is True

    def test_empty_list_returns_empty(self):
        actual = filter_players_by_name([], "Test")

        assert actual == []

    def test_no_match_returns_empty(self):
        players = [{"name": "Other", "isPublic": True}]
        actual = filter_players_by_name(players, "Test")

        assert actual == []

    def test_key_error_raises_parser_parsing_error(self):
        """Malformed player dict (missing required key) raises ParserParsingError."""
        players = [{"wrong_key": "value"}]  # missing 'name' and 'isPublic'
        with pytest.raises(
            ParserParsingError, match="Unexpected Blizzard search payload"
        ):
            filter_players_by_name(players, "Test")

    def test_uses_base_name_before_discriminator(self):
        """Name with discriminator: only the part before '-' is matched."""
        players = [{"name": "Player", "isPublic": True}]
        result = filter_players_by_name(players, "Player-1234")

        assert len(result) == 1

    @pytest.mark.parametrize("search", ["tekrop", "TEKROP", "tekrop-2217", "TeKrop"])
    def test_matches_case_insensitively(self, search: str):
        players = [{"name": "TeKrop", "isPublic": True}]

        assert filter_players_by_name(players, search) == players

    def test_casefold_handles_non_ascii_case(self):
        players = [{"name": "Straße", "isPublic": True}]

        assert filter_players_by_name(players, "STRASSE") == players

    def test_cjk_name_matches_as_is(self):
        players = [
            {"name": "天使", "isPublic": True},
            {"name": "天使ちゃん", "isPublic": True},
        ]

        assert filter_players_by_name(players, "天使") == players[:1]

    def test_fullwidth_name_is_not_normalized(self):
        """No NFKC: fullwidth and ASCII "Mercy" stay distinct names."""
        players = [
            {"name": "\uff2d\uff45\uff52\uff43\uff59", "isPublic": True},
            {"name": "Mercy", "isPublic": True},
        ]

        assert (
            filter_players_by_name(players, "\uff4d\uff45\uff52\uff43\uff59")
            == players[:1]
        )
        assert filter_players_by_name(players, "mercy") == players[1:]


# ── prioritize_exact_matches ─────────────────────────────────────────────────


class TestPrioritizeExactMatches:
    def test_exact_casing_first_keeps_order_within_groups(self):
        players = [
            {"name": "MERCY", "player_id": "a"},
            {"name": "mercy", "player_id": "b"},
            {"name": "Mercy", "player_id": "c"},
            {"name": "mercy", "player_id": "d"},
        ]
        result = prioritize_exact_matches(players, "mercy")

        assert [p["player_id"] for p in result] == ["b", "d", "a", "c"]

    def test_discriminator_is_ignored(self):
        players = [{"name": "TEKROP"}, {"name": "TeKrop"}]
        result = prioritize_exact_matches(players, "TeKrop-2217")

        assert [p["name"] for p in result] == ["TeKrop", "TEKROP"]


# ── transform_player_search_results ──────────────────────────────────────────


def _make_player(**overrides) -> dict:
    base = {
        "name": "Test",
        "url": "Test-1234",
        "avatar": "https://example.com/avatar.png",
        "namecard": None,
        "title": None,
        "lastUpdated": 1700000000,
        "isPublic": True,
        "portrait": None,
    }
    base.update(overrides)
    return base


class TestTransformPlayerSearchResults:
    def test_basic_transform(self):
        player = _make_player()
        result = transform_player_search_results([player])

        assert len(result) == 1
        r = result[0]

        assert r["player_id"] == "Test-1234"
        assert r["name"] == "Test"

    def test_portrait_branch_nullifies_avatar_namecard_title(self):
        """When portrait is present, avatar/namecard/title are set to None."""
        player = _make_player(
            portrait="https://example.com/portrait.png",
            avatar="https://example.com/avatar.png",
            namecard="https://example.com/namecard.png",
            title="Philosopher",
        )
        result = transform_player_search_results([player])
        r = result[0]

        assert r["avatar"] is None
        assert r["namecard"] is None
        assert r["title"] is None

    def test_player_id_always_comes_from_blizzard_url(self):
        """Single result is no longer special-cased: player_id = player['url']."""
        player = _make_player(url="abc123%7Cdef456")
        result = transform_player_search_results([player])

        assert result[0]["player_id"] == "abc123%7Cdef456"
        assert result[0]["blizzard_id"] == "abc123%7Cdef456"
        assert result[0]["career_url"].endswith("/players/abc123%7Cdef456")

    def test_multiple_results_use_url_as_player_id(self):
        p1 = _make_player(name="Test", url="Test-0001")
        p2 = _make_player(name="Test", url="Test-0002")
        results = transform_player_search_results([p1, p2])

        assert results[0]["player_id"] == "Test-0001"
        assert results[1]["player_id"] == "Test-0002"

    def test_missing_required_field_raises_parser_parsing_error(self):
        """Missing 'url' key raises ParserParsingError."""
        player = {"name": "Test"}  # missing required fields
        with pytest.raises(ParserParsingError, match="Missing required field"):
            transform_player_search_results([player])


# ── apply_ordering ────────────────────────────────────────────────────────────


class TestApplyOrdering:
    def test_sorts_ascending(self):
        players = [{"name": "Beta"}, {"name": "Alpha"}]
        result = apply_ordering(players, "name:asc")

        assert [p["name"] for p in result] == ["Alpha", "Beta"]

    def test_sorts_descending(self):
        players = [{"name": "Alpha"}, {"name": "Beta"}]
        result = apply_ordering(players, "name:desc")

        assert [p["name"] for p in result] == ["Beta", "Alpha"]

    def test_invalid_format_raises_parser_parsing_error(self):
        """Missing ':' in order_by raises ParserParsingError."""
        with pytest.raises(ParserParsingError, match="Invalid ordering"):
            apply_ordering([{"name": "X"}], "name_asc")

    def test_invalid_field_raises_parser_parsing_error(self):
        """Non-existent sort field raises ParserParsingError."""
        players = [{"name": "X"}]
        with pytest.raises(ParserParsingError, match="Invalid ordering"):
            apply_ordering(players, "nonexistent_field:asc")
