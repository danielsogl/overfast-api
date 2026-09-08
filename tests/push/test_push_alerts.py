"""Tests for the rank-alert decision and its message catalogue"""

import pytest

from app.domain.push_alerts import format_rank, highest_rank, rank_alert
from app.domain.push_messages import RANK_ALERT_MESSAGES, rank_alert_text


def _snapshot(competitive: dict, taken_at: int = 0) -> dict:
    return {"taken_at": taken_at, "data": {"competitive": competitive}}


def _pc(**roles) -> dict:
    return {"pc": {r: {"division": d, "tier": t} for r, (d, t) in roles.items()}}


@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        ({"division": "diamond", "tier": 2}, "Diamond 2"),
        ({"division": "grandmaster", "tier": 1}, "Grandmaster 1"),
    ],
)
def test_format_rank_matches_the_app_string(rank: dict, expected: str):
    """The app renders this English string in every language; so must we."""
    assert format_rank(rank) == expected


class TestHighestRank:
    def test_picks_the_best_division_across_roles(self):
        result = highest_rank(
            _pc(tank=("gold", 1), damage=("diamond", 5), support=("platinum", 1))
        )
        assert result == {"division": "diamond", "tier": 5}

    def test_tier_one_beats_tier_five_inside_a_division(self):
        result = highest_rank(_pc(tank=("gold", 5), damage=("gold", 1)))
        assert result == {"division": "gold", "tier": 1}

    def test_prefers_pc_over_console(self):
        competitive = {
            "pc": {"tank": {"division": "bronze", "tier": 5}},
            "console": {"tank": {"division": "grandmaster", "tier": 1}},
        }
        assert highest_rank(competitive) == {"division": "bronze", "tier": 5}

    def test_falls_back_to_console_when_pc_is_absent(self):
        competitive = {"console": {"tank": {"division": "gold", "tier": 3}}}
        assert highest_rank(competitive) == {"division": "gold", "tier": 3}

    @pytest.mark.parametrize("competitive", [{}, {"pc": {}}, {"pc": {"season": 18}}])
    def test_unranked_yields_none(self, competitive: dict):
        assert highest_rank(competitive) is None


class TestRankAlert:
    def test_announces_a_move(self):
        alert = rank_alert(
            [
                _snapshot(_pc(tank=("platinum", 5)), 2),
                _snapshot(_pc(tank=("gold", 1)), 1),
            ]
        )
        assert alert == "Platinum 5"

    def test_stays_silent_when_the_highest_rank_is_unchanged(self):
        """A lower role moving is not the rank the app shows, so it is not news."""
        alert = rank_alert(
            [
                _snapshot(_pc(tank=("diamond", 3), damage=("gold", 2)), 2),
                _snapshot(_pc(tank=("diamond", 3), damage=("gold", 4)), 1),
            ]
        )
        assert alert is None

    def test_a_single_snapshot_is_not_a_change(self):
        assert rank_alert([_snapshot(_pc(tank=("gold", 3)))]) is None

    @pytest.mark.parametrize(
        ("newest", "oldest"),
        [({}, _pc(tank=("gold", 3))), (_pc(tank=("gold", 3)), {})],
    )
    def test_placement_and_loss_of_rank_stay_silent(self, newest, oldest):
        assert rank_alert([_snapshot(newest, 2), _snapshot(oldest, 1)]) is None


class TestMessages:
    def test_every_shipped_locale_has_both_placeholders(self):
        for locale, (title, body) in RANK_ALERT_MESSAGES.items():
            assert title, locale
            assert "{name}" in body, locale
            assert "{rank}" in body, locale

    def test_substitutes_name_and_rank(self):
        title, body = rank_alert_text("en-US", "TeKrop", "Diamond 2")
        assert title == "Rank Update"
        assert body == "TeKrop is now Diamond 2"

    def test_falls_back_from_region_to_language(self):
        """A device may report `de-AT`; the app only ships `de`."""
        assert rank_alert_text("de-AT", "X", "Gold 3")[0] == "Rang-Update"

    def test_falls_back_to_english_for_an_unknown_locale(self):
        assert rank_alert_text("cy", "X", "Gold 3")[0] == "Rank Update"
