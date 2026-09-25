"""Tests for the player snapshot payload builder and the snapshot diff"""

from typing import TYPE_CHECKING, cast

import pytest

from app.domain.enums import PlayerGamemode, PlayerPlatform
from app.domain.parsers.player_profile import parse_player_profile_html
from app.domain.parsers.player_snapshot import (
    SESSION_GAP_SECONDS,
    SNAPSHOT_GENERAL_KEYS,
    build_player_sessions,
    build_player_snapshot,
    diff_games,
    diff_player_snapshots,
)
from app.domain.parsers.player_stats import process_player_stats_summary
from tests.helpers import players_ids, read_html_file

if TYPE_CHECKING:
    from app.domain.models.player import PlayerProfileData


def _snapshot(taken_at: int, data: dict) -> dict:
    return {"taken_at": taken_at, "last_updated_blizzard": taken_at - 10, "data": data}


def _profile(
    competitive: dict | None = None, stats: dict | None = None
) -> PlayerProfileData:
    """A minimal stand-in for a parsed profile.

    Cast rather than completed: ``build_player_snapshot`` reads only the
    endorsement, the competitive ranks and the stats, so filling in the rest of
    the seven summary keys would add noise to every case here and assert
    nothing the summary parser's own tests do not already cover.
    """
    return cast(
        "PlayerProfileData",
        {
            "summary": {
                "endorsement": {"level": 3, "frame": "https://example.com/3.svg"},
                "competitive": competitive,
            },
            "stats": stats,
        },
    )


_PC_QUICKPLAY_STATS = {
    "pc": {
        "quickplay": {
            "heroes_comparisons": {
                "time_played": {
                    "label": "Time Played",
                    "values": [{"hero": "ana", "value": 3600}],
                },
                "games_won": {
                    "label": "Games Won",
                    "values": [{"hero": "ana", "value": 10}],
                },
                "win_percentage": {
                    "label": "Win Percentage",
                    "values": [{"hero": "ana", "value": 50}],
                },
                "eliminations_per_life": {
                    "label": "Eliminations per Life",
                    "values": [{"hero": "ana", "value": 2.5}],
                },
            },
            "career_stats": {},
        },
        "competitive": None,
    },
    "console": None,
}


class TestBuildPlayerSnapshot:
    @pytest.mark.parametrize("player_id", players_ids)
    def test_real_profile_yields_a_snapshot(self, player_id: str):
        html = read_html_file(f"players/{player_id}.html") or ""
        parsed = parse_player_profile_html(html, {"lastUpdated": 1700000000})

        result = build_player_snapshot(parsed)

        assert result is not None
        assert set(result) == {
            "endorsement",
            "competitive",
            "seasons",
            "heroes",
            "general",
        }
        assert result["heroes"]

    @pytest.mark.parametrize("player_id", players_ids)
    def test_general_totals_match_the_stats_summary_route(self, player_id: str):
        """The stored totals must equal a live read of the same version.

        Both go through `process_player_stats_summary`, so this pins the
        snapshot to that computation rather than to a copy of its output.
        """
        html = read_html_file(f"players/{player_id}.html") or ""
        parsed = parse_player_profile_html(html, {"lastUpdated": 1700000000})

        general = (build_player_snapshot(parsed) or {})["general"]

        for platform, gamemodes in general.items():
            for gamemode, stats in gamemodes.items():
                expected = process_player_stats_summary(
                    parsed, PlayerGamemode(gamemode), PlayerPlatform(platform)
                )["general"]
                assert set(stats) == set(SNAPSHOT_GENERAL_KEYS)
                for key in SNAPSHOT_GENERAL_KEYS:
                    assert stats[key] == expected[key]

    def test_general_is_empty_without_stats(self):
        parsed = _profile(
            competitive={"pc": {"tank": {"division": "gold", "tier": 3}}}, stats=None
        )

        assert (build_player_snapshot(parsed) or {})["general"] == {}

    def test_returns_none_for_a_private_profile(self):
        parsed = _profile(competitive=None, stats=None)

        result = build_player_snapshot(parsed)

        assert result is None

    def test_returns_none_when_summary_and_stats_are_absent(self):
        result = build_player_snapshot(cast("PlayerProfileData", {}))

        assert result is None

    def test_ranks_only_profile_is_recorded(self):
        parsed = _profile(
            competitive={
                "pc": {
                    "tank": {
                        "division": "diamond",
                        "tier": 3,
                        "role_icon": "https://example.com/role.svg",
                        "rank_icon": "https://example.com/rank.png",
                        "tier_icon": "https://example.com/tier.png",
                    },
                    "damage": None,
                    "season": 14,
                },
                "console": None,
            }
        )

        result = build_player_snapshot(parsed)

        assert result == {
            "endorsement": 3,
            "competitive": {"pc": {"tank": {"division": "diamond", "tier": 3}}},
            "seasons": {"pc": 14},
            "heroes": {},
            "general": {},
        }

    def test_keeps_only_the_cumulative_hero_counters(self):
        parsed = _profile(stats=_PC_QUICKPLAY_STATS)

        result = build_player_snapshot(parsed) or {}

        assert result["heroes"] == {
            "pc": {
                "quickplay": {
                    "ana": {
                        "time_played": 3600,
                        "games_won": 10,
                        "win_percentage": 50,
                    }
                }
            }
        }

    def test_a_category_blizzard_left_empty_is_skipped(self):
        stats = {
            "pc": {
                "quickplay": {
                    "heroes_comparisons": {
                        "time_played": {
                            "label": "Time Played",
                            "values": [{"hero": "ana", "value": 3600}],
                        },
                        # Blizzard regularly serves a category with no rows at
                        # all; the parser reports it as None.
                        "games_won": None,
                    },
                    "career_stats": {},
                },
                "competitive": None,
            },
            "console": None,
        }
        parsed = _profile(stats=stats)

        result = build_player_snapshot(parsed) or {}

        assert result["heroes"]["pc"]["quickplay"] == {"ana": {"time_played": 3600}}

    def test_missing_endorsement_is_reported_as_none(self):
        parsed = _profile(stats=_PC_QUICKPLAY_STATS)
        parsed["summary"]["endorsement"] = None

        result = build_player_snapshot(parsed) or {}

        assert result["endorsement"] is None


class TestDiffGames:
    def test_sums_every_platform_and_gamemode(self):
        before = {
            "general": {
                "pc": {
                    "competitive": {
                        "games_played": 10,
                        "games_won": 6,
                        "games_lost": 4,
                        "time_played": 3600,
                    },
                    "quickplay": {
                        "games_played": 5,
                        "games_won": 2,
                        "games_lost": 3,
                        "time_played": 1200,
                    },
                }
            }
        }
        after = {
            "general": {
                "pc": {
                    "competitive": {
                        "games_played": 13,
                        "games_won": 8,
                        "games_lost": 5,
                        "time_played": 5400,
                    },
                    "quickplay": {
                        "games_played": 6,
                        "games_won": 3,
                        "games_lost": 3,
                        "time_played": 1800,
                    },
                }
            }
        }

        result = diff_games(before, after)

        assert result == {
            "games_played": 4,
            "games_won": 3,
            "games_lost": 1,
            "time_played": 2400,
        }

    def test_a_gamemode_missing_before_contributes_nothing(self):
        before = {}
        after = {
            "general": {
                "pc": {
                    "competitive": {
                        "games_played": 900,
                        "games_won": 450,
                        "games_lost": 450,
                        "time_played": 999999,
                    }
                }
            }
        }

        result = diff_games(before, after)

        assert result == {
            "games_played": 0,
            "games_won": 0,
            "games_lost": 0,
            "time_played": 0,
        }


class TestDiffPlayerSnapshots:
    def test_no_history_returns_empty_deltas(self):
        result = diff_player_snapshots([])

        assert result == {
            "snapshots_compared": 0,
            "compared_from": None,
            "compared_to": None,
            "ranks": [],
            "heroes": [],
            "totals": {"time_played": 0, "games_won": 0},
        }

    def test_single_snapshot_has_nothing_to_compare(self):
        snapshots = [_snapshot(2000, {"heroes": {}, "competitive": {}})]

        result = diff_player_snapshots(snapshots)

        assert result["snapshots_compared"] == 1
        assert result["compared_from"] is None
        assert result["heroes"] == []

    def test_hero_deltas_and_totals(self):
        older = {
            "competitive": {},
            "heroes": {
                "pc": {
                    "competitive": {
                        "ana": {
                            "time_played": 3600,
                            "games_won": 10,
                            "win_percentage": 50,
                        },
                        "mercy": {
                            "time_played": 600,
                            "games_won": 1,
                            "win_percentage": 20,
                        },
                    }
                }
            },
        }
        newer = {
            "competitive": {},
            "heroes": {
                "pc": {
                    "competitive": {
                        "ana": {
                            "time_played": 5400,
                            "games_won": 14,
                            "win_percentage": 56,
                        },
                        "mercy": {
                            "time_played": 600,
                            "games_won": 1,
                            "win_percentage": 20,
                        },
                    }
                }
            },
        }

        result = diff_player_snapshots([_snapshot(2000, newer), _snapshot(1000, older)])

        assert result["snapshots_compared"] == 2  # noqa: PLR2004
        assert result["compared_from"] == 1000  # noqa: PLR2004
        assert result["compared_to"] == 2000  # noqa: PLR2004
        assert result["heroes"] == [
            {
                "platform": "pc",
                "gamemode": "competitive",
                "hero": "ana",
                "time_played": 1800,
                "games_won": 4,
                "win_percentage_before": 50,
                "win_percentage_after": 56,
            }
        ]
        assert result["totals"] == {"time_played": 1800, "games_won": 4}

    def test_a_hero_played_for_the_first_time_counts_from_zero(self):
        older = {"competitive": {}, "heroes": {}}
        newer = {
            "competitive": {},
            "heroes": {
                "console": {
                    "quickplay": {
                        "kiriko": {
                            "time_played": 900,
                            "games_won": 2,
                            "win_percentage": 66,
                        }
                    }
                }
            },
        }

        result = diff_player_snapshots([_snapshot(2000, newer), _snapshot(1000, older)])

        assert result["heroes"][0]["time_played"] == 900  # noqa: PLR2004
        assert result["heroes"][0]["win_percentage_before"] is None
        assert result["heroes"][0]["win_percentage_after"] == 66  # noqa: PLR2004

    def test_rank_movement_is_reported_only_when_it_changed(self):
        older = {
            "heroes": {},
            "competitive": {
                "pc": {
                    "tank": {"division": "gold", "tier": 2},
                    "support": {"division": "platinum", "tier": 1},
                }
            },
        }
        newer = {
            "heroes": {},
            "competitive": {
                "pc": {
                    "tank": {"division": "platinum", "tier": 5},
                    "support": {"division": "platinum", "tier": 1},
                }
            },
        }

        result = diff_player_snapshots([_snapshot(2000, newer), _snapshot(1000, older)])

        assert result["ranks"] == [
            {
                "platform": "pc",
                "role": "tank",
                "before": {"division": "gold", "tier": 2},
                "after": {"division": "platinum", "tier": 5},
            }
        ]

    def test_newly_ranked_role_reports_a_null_before(self):
        older = {"heroes": {}, "competitive": {}}
        newer = {
            "heroes": {},
            "competitive": {"pc": {"damage": {"division": "bronze", "tier": 5}}},
        }

        result = diff_player_snapshots([_snapshot(2000, newer), _snapshot(1000, older)])

        assert result["ranks"] == [
            {
                "platform": "pc",
                "role": "damage",
                "before": None,
                "after": {"division": "bronze", "tier": 5},
            }
        ]

    def test_only_the_two_ends_of_the_series_are_compared(self):
        def data(time_played: int) -> dict:
            return {
                "competitive": {},
                "heroes": {
                    "pc": {
                        "quickplay": {
                            "ana": {
                                "time_played": time_played,
                                "games_won": 0,
                                "win_percentage": 0,
                            }
                        }
                    }
                },
            }

        snapshots = [
            _snapshot(3000, data(300)),
            _snapshot(2000, data(200)),
            _snapshot(1000, data(100)),
        ]

        result = diff_player_snapshots(snapshots)

        assert result["snapshots_compared"] == 3  # noqa: PLR2004
        assert result["totals"]["time_played"] == 200  # noqa: PLR2004


def _session_snapshot(last_updated: int, games_played: int, time_played: int) -> dict:
    return {
        "taken_at": last_updated + 10,
        "last_updated_blizzard": last_updated,
        "data": {
            "general": {
                "pc": {
                    "quickplay": {
                        "games_played": games_played,
                        "games_won": 0,
                        "games_lost": 0,
                        "time_played": time_played,
                    }
                }
            },
            "heroes": {},
            "competitive": {},
        },
    }


class TestBuildPlayerSessions:
    def test_baseline_only_yields_no_session(self):
        snapshots = [_session_snapshot(0, games_played=0, time_played=0)]

        assert build_player_sessions(snapshots, limit=10) == []

    def test_no_history_yields_no_session(self):
        assert build_player_sessions([], limit=10) == []

    def test_splits_on_a_gap_and_reports_newest_first(self):
        baseline = _session_snapshot(0, games_played=0, time_played=0)
        s1 = _session_snapshot(1800, games_played=1, time_played=600)
        s2 = _session_snapshot(3600, games_played=3, time_played=1800)
        gap = SESSION_GAP_SECONDS + 3600
        s3 = _session_snapshot(3600 + gap, games_played=4, time_played=2100)
        # storage returns newest first
        snapshots = [s3, s2, s1, baseline]

        result = build_player_sessions(snapshots, limit=10)

        assert len(result) == 2  # noqa: PLR2004
        newest, oldest = result
        assert oldest["since"] == 0
        assert oldest["ended_at"] == 3600  # noqa: PLR2004
        assert oldest["snapshots"] == 2  # noqa: PLR2004
        assert oldest["games"]["games_played"] == 3  # noqa: PLR2004
        assert oldest["games"]["time_played"] == 1800  # noqa: PLR2004
        assert newest["since"] == 3600  # noqa: PLR2004
        assert newest["ended_at"] == 3600 + gap
        assert newest["snapshots"] == 1
        assert newest["games"]["games_played"] == 1
        assert newest["games"]["time_played"] == 300  # noqa: PLR2004

    def test_empty_session_is_dropped(self):
        baseline = _session_snapshot(0, games_played=0, time_played=0)
        unchanged = _session_snapshot(1800, games_played=0, time_played=0)

        result = build_player_sessions([unchanged, baseline], limit=10)

        assert result == []

    def test_limit_keeps_only_the_newest_sessions(self):
        baseline = _session_snapshot(0, games_played=0, time_played=0)
        gap = SESSION_GAP_SECONDS + 3600
        s1 = _session_snapshot(gap, games_played=1, time_played=600)
        s2 = _session_snapshot(2 * gap, games_played=2, time_played=1200)
        snapshots = [s2, s1, baseline]

        result = build_player_sessions(snapshots, limit=1)

        assert len(result) == 1
        assert result[0]["since"] == gap
