"""Tests for the player snapshot payload builder and the snapshot diff"""

import pytest

from app.domain.parsers.player_profile import parse_player_profile_html
from app.domain.parsers.player_snapshot import (
    build_player_snapshot,
    diff_player_snapshots,
)
from tests.helpers import players_ids, read_html_file


def _snapshot(taken_at: int, data: dict) -> dict:
    return {"taken_at": taken_at, "last_updated_blizzard": taken_at - 10, "data": data}


def _profile(competitive: dict | None = None, stats: dict | None = None) -> dict:
    return {
        "summary": {
            "endorsement": {"level": 3, "frame": "https://example.com/3.svg"},
            "competitive": competitive,
        },
        "stats": stats,
    }


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
        assert set(result) == {"endorsement", "competitive", "heroes"}
        assert result["heroes"]

    def test_returns_none_for_a_private_profile(self):
        parsed = _profile(competitive=None, stats=None)

        result = build_player_snapshot(parsed)

        assert result is None

    def test_returns_none_when_summary_and_stats_are_absent(self):
        result = build_player_snapshot({})

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
            "heroes": {},
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
