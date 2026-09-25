"""Tests for the push-alert decisions and their message catalogues"""

import pytest

from app.domain.push_alerts import (
    changed_heroes,
    format_rank,
    hero_alert,
    highest_rank,
    rank_alert,
    weekly_recap,
)
from app.domain.push_messages import (
    HERO_ALERT_MESSAGES,
    RANK_ALERT_MESSAGES,
    WEEKLY_RECAP_MESSAGES,
    hero_alert_text,
    rank_alert_text,
    weekly_recap_text,
)


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


def _entry(hero: str | None, *, details=None, abilities=None) -> dict:
    return {
        "title": hero or "Map",
        "hero": hero,
        "details": details or [],
        "abilities": abilities or [],
    }


def _patch(*entries: dict) -> dict:
    return {"date": "2026-09-08", "sections": [{"entries": list(entries)}]}


def _played(**by_hero: int) -> dict:
    """One snapshot whose pc/quickplay heroes carry the given time_played."""
    return {
        "data": {
            "heroes": {
                "pc": {
                    "quickplay": {
                        hero: {"time_played": seconds}
                        for hero, seconds in by_hero.items()
                    }
                }
            }
        }
    }


class TestChangedHeroes:
    def test_collects_heroes_with_details_or_abilities(self):
        patch = _patch(
            _entry("ana", details=["Biotic Rifle damage reduced."]),
            _entry("dva", abilities=[{"name": "Boosters", "details": ["Faster."]}]),
        )

        assert changed_heroes(patch) == {"ana", "dva"}

    def test_ignores_an_entry_the_parser_could_not_resolve(self):
        """A hero released the same day has no key yet — never badge a guess."""
        assert changed_heroes(_patch(_entry(None, details=["New hero!"]))) == set()

    def test_ignores_an_entry_with_no_text(self):
        """A map update is a pair of screenshots; it changes no hero."""
        assert changed_heroes(_patch(_entry("ana"))) == set()

    def test_a_patch_with_no_sections_changes_nothing(self):
        assert changed_heroes({"date": "2026-09-08"}) == set()


class TestHeroAlert:
    def test_names_the_changed_heroes_most_played_first(self):
        heroes = hero_alert({"ana", "dva"}, [_played(ana=100, dva=900, genji=9999)])

        assert heroes == ["dva", "ana"]

    def test_sums_playtime_across_players_platforms_and_gamemodes(self):
        """One device, two watched players who share a main: one ranked list."""
        console = {
            "data": {
                "heroes": {"console": {"competitive": {"ana": {"time_played": 50}}}}
            }
        }

        heroes = hero_alert({"ana", "dva"}, [_played(ana=30, dva=60), console])

        assert heroes == ["ana", "dva"]

    def test_caps_the_list(self):
        changed = {"ana", "dva", "genji", "mercy"}

        heroes = hero_alert(changed, [_played(ana=4, dva=3, genji=2, mercy=1)], limit=3)

        assert heroes == ["ana", "dva", "genji"]

    def test_says_nothing_when_the_player_plays_none_of_them(self):
        assert hero_alert({"ana"}, [_played(genji=900)]) == []

    def test_says_nothing_without_a_snapshot(self):
        assert hero_alert({"ana"}, []) == []


class TestHeroMessages:
    def test_every_shipped_locale_carries_the_placeholder(self):
        for locale, (title, body) in HERO_ALERT_MESSAGES.items():
            assert title, locale
            assert "{heroes}" in body, locale

    def test_the_two_catalogues_ship_the_same_locales(self):
        assert HERO_ALERT_MESSAGES.keys() == RANK_ALERT_MESSAGES.keys()

    def test_joins_the_hero_names(self):
        title, body = hero_alert_text("en-US", ["D.Va", "Ana"])

        assert title == "Hero Update"
        assert body == "Changes to D.Va, Ana in the latest patch"

    def test_falls_back_to_english_for_an_unknown_locale(self):
        assert hero_alert_text("cy", ["Ana"])[0] == "Hero Update"


# ─── Weekly recap ──────────────────────────────────────────────────────────────


def _general(games_played: int, games_won: int, time_played: int = 0) -> dict:
    return {
        "pc": {
            "quickplay": {
                "games_played": games_played,
                "games_won": games_won,
                "games_lost": games_played - games_won,
                "time_played": time_played,
            }
        }
    }


def _heroes(**by_hero: int) -> dict:
    return {
        "pc": {
            "quickplay": {
                hero: {"time_played": seconds, "games_won": 0}
                for hero, seconds in by_hero.items()
            }
        }
    }


def _week_snapshot(
    taken_at: int,
    games_played: int,
    games_won: int,
    competitive: dict | None = None,
    **by_hero: int,
) -> dict:
    return {
        "taken_at": taken_at,
        "data": {
            "general": _general(games_played, games_won),
            "heroes": _heroes(**by_hero),
            "competitive": competitive or {},
        },
    }


class TestWeeklyRecap:
    def test_reports_games_played_and_won(self):
        recap = weekly_recap(
            [
                _week_snapshot(2, games_played=12, games_won=8, dva=100),
                _week_snapshot(1, games_played=5, games_won=3, dva=50),
            ]
        )

        assert recap == {
            "games_played": 7,
            "games_won": 5,
            "hero": "dva",
            "rank": None,
        }

    def test_names_the_top_hero_by_time_played_delta(self):
        recap = weekly_recap(
            [
                _week_snapshot(2, 5, 3, ana=1000, dva=9000),
                _week_snapshot(1, 1, 1, ana=100, dva=100),
            ]
        )

        assert recap is not None
        assert recap["hero"] == "dva"

    def test_includes_a_rank_move_when_the_highest_rank_changed(self):
        recap = weekly_recap(
            [
                _week_snapshot(2, 5, 3, competitive=_pc(tank=("diamond", 2))),
                _week_snapshot(1, 1, 1, competitive=_pc(tank=("gold", 1))),
            ]
        )

        assert recap is not None
        assert recap["rank"] == "Diamond 2"

    def test_stays_silent_with_fewer_than_two_snapshots(self):
        assert weekly_recap([_week_snapshot(1, 5, 3)]) is None

    def test_stays_silent_for_a_week_without_games(self):
        recap = weekly_recap(
            [
                _week_snapshot(2, games_played=5, games_won=3),
                _week_snapshot(1, games_played=5, games_won=3),
            ]
        )

        assert recap is None

    def test_no_hero_played_yields_none(self):
        recap = weekly_recap(
            [
                _week_snapshot(2, 5, 3),
                _week_snapshot(1, 1, 1),
            ]
        )

        assert recap is not None
        assert recap["hero"] is None


class TestWeeklyRecapMessages:
    def test_every_shipped_locale_carries_the_placeholders(self):
        for locale, (title, stats, top_hero) in WEEKLY_RECAP_MESSAGES.items():
            assert title, locale
            assert "{name}" in stats, locale
            assert "{games}" in stats, locale
            assert "{wins}" in stats, locale
            assert "{hero}" in top_hero, locale

    def test_the_three_catalogues_ship_the_same_locales(self):
        assert WEEKLY_RECAP_MESSAGES.keys() == RANK_ALERT_MESSAGES.keys()

    def test_builds_the_full_sentence(self):
        title, body = weekly_recap_text("en-US", "TeKrop", 7, 5, "D.Va", "Diamond 2")

        assert title == "Weekly Recap"
        assert body == "TeKrop: Games: 7 · Wins: 5 · Top hero: D.Va · Diamond 2"

    def test_drops_the_hero_part_when_there_is_no_hero(self):
        _, body = weekly_recap_text("en-US", "TeKrop", 1, 0, None, None)

        assert body == "TeKrop: Games: 1 · Wins: 0"

    def test_drops_the_rank_part_when_it_did_not_move(self):
        _, body = weekly_recap_text("en-US", "TeKrop", 3, 2, "D.Va", None)

        assert body == "TeKrop: Games: 3 · Wins: 2 · Top hero: D.Va"

    def test_is_plural_safe_for_a_single_game(self):
        """`Games: 1`, never `1 games`."""
        _, body = weekly_recap_text("en-US", "TeKrop", 1, 1, None, None)

        assert "1 games" not in body

    def test_falls_back_to_english_for_an_unknown_locale(self):
        assert weekly_recap_text("cy", "X", 1, 1, None, None)[0] == "Weekly Recap"
