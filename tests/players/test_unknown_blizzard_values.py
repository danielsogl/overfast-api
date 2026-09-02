"""Blizzard ships a value we cannot name yet — degrade, never 500.

Every case here 500ed before: the response models type these fields as enums or
as StrictInt | StrictFloat, and a rejected field fails the whole request rather
than costing one row. See app/domain/parsers/player_profile.py.
"""

import pytest

from app.domain.enums import (
    CareerStatCategory,
    CompetitiveRole,
    HeroKey,
)
from app.domain.parsers.player_helpers import (
    get_computed_stat_value,
    get_role_key_from_icon,
)
from app.domain.parsers.player_profile import parse_player_profile_html
from tests.helpers import read_html_file

_HTML = read_html_file("players/TeKrop-2217.html") or ""


class TestUnknownCompetitiveRole:
    def test_known_role_still_resolves(self):
        result = get_role_key_from_icon("https://x/icons/role/tank.abc.svg#icon")

        assert result is CompetitiveRole.TANK

    def test_legacy_offense_maps_to_damage(self):
        result = get_role_key_from_icon("https://x/icons/role/offense.abc.svg")

        assert result is CompetitiveRole.DAMAGE

    def test_unknown_role_returns_none_instead_of_raising(self):
        """A fourth competitive role used to raise KeyError, which _parse_summary
        catches and turns into a 500 for every ranked player."""
        result = get_role_key_from_icon("https://x/icons/role/duelist.abc.svg")

        assert result is None


class TestUnreadableStatValues:
    # One case below carries a NO-BREAK SPACE inside the number on purpose —
    # Blizzard formatting that slips past every regex in the helper.
    @pytest.mark.parametrize(
        "raw",
        ["1.2K", "1 234", "∞", "not-a-number"],  # noqa: RUF001
    )
    def test_unrecognised_formats_come_back_as_strings(self, raw: str):
        """get_computed_stat_value passes unknown formats through unchanged, so
        the parser — not the model — has to reject them."""
        result = get_computed_stat_value(raw)

        assert isinstance(result, str)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1,234", 1234), ("50%", 50), ("1:02:03", 3723), ("--", 0)],
    )
    def test_recognised_formats_still_convert(self, raw: str, expected: int):
        result = get_computed_stat_value(raw)

        assert result == expected


def _pc_quickplay(html: str) -> dict:
    """Parse the fixture and return its PC/quickplay stats block.

    The `stats` key is `None` for a profile Blizzard exposes no data for, so
    reaching into it unguarded would turn a fixture regression into an
    AttributeError rather than a failed assertion — and worse, a fixture that
    silently lost its stats would make every test below pass over an empty list.
    """
    profile = parse_player_profile_html(html)
    stats = profile["stats"]
    assert stats is not None, "fixture parsed to a profile with no stats at all"

    return stats["pc"]["quickplay"]


class TestProfileSurvivesUnknownValues:
    """End to end: the parser output must stay model-clean."""

    def test_every_comparison_hero_is_a_known_key(self):
        comparisons = _pc_quickplay(_HTML)["heroes_comparisons"]
        heroes = [
            row["hero"]
            for category in comparisons.values()
            if category
            for row in category["values"]
        ]

        assert heroes
        assert all(h in HeroKey for h in heroes)

    def test_every_comparison_value_is_numeric(self):
        comparisons = _pc_quickplay(_HTML)["heroes_comparisons"]
        values = [
            row["value"]
            for category in comparisons.values()
            if category
            for row in category["values"]
        ]

        assert values
        assert all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in values
        )

    def test_every_career_category_is_a_known_category(self):
        career = _pc_quickplay(_HTML)["career_stats"]
        categories = [c["category"] for hero in career.values() for c in hero]

        assert categories
        assert all(c in CareerStatCategory for c in categories)

    def test_every_career_stat_value_is_numeric(self):
        career = _pc_quickplay(_HTML)["career_stats"]
        values = [
            stat["value"]
            for hero in career.values()
            for category in hero
            for stat in category["stats"]
        ]

        assert values
        assert all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in values
        )
