"""Hitpoints appear nowhere on Blizzard's site, so heroes.csv is hand-maintained
and rots after balance patches. The *changes* are published though, and this is
the logic that reads them. Every sample below is real patch-note wording.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx2
import pytest
import scripts.check_blizzard_drift as drift
from scripts.check_blizzard_drift import (
    check_hero_stats_recording,
    hero_sections,
    hitpoint_findings,
    parse_rotation_maps,
    parse_rotation_tiers,
)

from app.domain.enums import CompetitiveDivisionFilter

_ROWS = {
    "Reaper": {"role": "damage", "health": "275", "armor": "0", "shields": "0"},
    "Sigma": {"role": "tank", "health": "350", "armor": "0", "shields": "250"},
    "Junkrat": {"role": "damage", "health": "200", "armor": "0", "shields": "0"},
    "Reinhardt": {"role": "tank", "health": "400", "armor": "300", "shields": "0"},
}

# Verbatim from https://overwatch.blizzard.com/en-us/news/patch-notes/live/2026/
_REAPER = (
    "Reaper",
    (
        "Dire Triggers has increased Reaper's strengths across many matchups. "
        "Reducing his health lowers his survivability while preserving the "
        "lethality Dire Triggers provides. Health reduced from 300 to 275."
    ),
)
_SIGMA = (
    "Sigma",
    (
        "Sigma remains durable when successfully rotating through his defensive "
        "tools. Shield health reduced from 275 to 250."
    ),
)


def _levels(findings: list[tuple[str, str]]) -> list[str]:
    return [level for level, _ in findings]


class TestStaleValueIsAFailure:
    """The high-signal case: our value is still the pre-patch one."""

    def test_stale_health_fails(self):
        rows = {**_ROWS, "Reaper": {**_ROWS["Reaper"], "health": "300"}}

        findings = hitpoint_findings([_REAPER], rows)

        assert _levels(findings) == ["fail"]
        assert "Reaper health is 300" in findings[0][1]
        assert "changed it to 275" in findings[0][1]

    def test_stale_shields_fails(self):
        rows = {**_ROWS, "Sigma": {**_ROWS["Sigma"], "shields": "275"}}

        findings = hitpoint_findings([_SIGMA], rows)

        assert _levels(findings) == ["fail"]
        assert "Sigma shields is 275" in findings[0][1]

    def test_tank_health_is_compared_in_role_passive_space(self):
        """Tanks carry +150 from the role passive and heroes.csv stores the
        total, so a note's base value must be lifted before comparing. Without
        this D.Va's 175 -> 200 only warned, and the row stayed stale."""
        rows = {
            "D.Va": {"role": "tank", "health": "325", "armor": "325", "shields": "0"}
        }
        section = ("D.Va", "Mech base health increased from 175 to 200.")

        findings = hitpoint_findings([section], rows)

        assert _levels(findings) == ["fail"]
        assert "D.Va health is 325" in findings[0][1]
        assert "changed it to 350" in findings[0][1]
        assert "175 to 200" in findings[0][1]


class TestCurrentValueIsSilent:
    """A run that reports nothing is the normal state; noise gets ignored."""

    @pytest.mark.parametrize("section", [_REAPER, _SIGMA])
    def test_up_to_date_value_produces_no_finding(self, section: tuple[str, str]):
        findings = hitpoint_findings([section], _ROWS)

        assert findings == []


class TestImplausibleDeltasAreIgnored:
    def test_barrier_health_is_not_hero_health(self):
        """Reinhardt's barrier is described as "Health" too, but 1500 is not a
        number any hero row holds, so it must not produce noise."""
        section = (
            "Reinhardt",
            (
                "Barrier Field is too forgiving at the current value. "
                "Health reduced from 1500 to 1100."
            ),
        )

        findings = hitpoint_findings([section], _ROWS)

        assert findings == []

    def test_ability_shields_are_not_hero_shields(self):
        section = ("Sigma", "Kinetic Grasp Shields reduced from 25 to 15.")

        findings = hitpoint_findings([section], _ROWS)

        assert findings == []


class TestAmbiguousDeltasWarnRatherThanFail:
    def test_value_matching_neither_side_warns(self):
        """Could be an ability inside the hero's own section, or a value that
        was already wrong before the patch — worth a look, not a red run."""
        section = ("Junkrat", "Frag Launcher Health increased from 250 to 300.")
        rows = {**_ROWS, "Junkrat": {**_ROWS["Junkrat"], "health": "200"}}

        findings = hitpoint_findings([section], rows)

        assert _levels(findings) == ["warn"]


class TestAttribution:
    def test_delta_belongs_to_its_heading_not_a_hero_named_in_the_prose(self):
        """The regression that cost a day of red runs: D.Mon's section opens
        "to match D.Va's", and attributing to the nearest name in the flattened
        prose blamed D.Va — whose armor was the same 325 — for D.Mon's change."""
        rows = {
            "D.Mon": {"role": "tank", "health": "425", "armor": "325", "shields": "0"},
            "D.Va": {"role": "tank", "health": "350", "armor": "325", "shields": "0"},
        }
        section = (
            "D.Mon",
            (
                "We are reducing Call Mech's transformation time to match "
                "D.Va's while adjusting Portable Fusion Repeater. "
                "Armor reduced from 325 to 300."
            ),
        )

        findings = hitpoint_findings([section], rows)

        assert _levels(findings) == ["fail"]
        assert findings[0][1].startswith("D.Mon armor is 325")

    def test_unknown_hero_heading_is_skipped(self):
        """A hero we do not carry yet is the heroes-index check's business."""
        findings = hitpoint_findings(
            [("Nobody", "Health reduced from 300 to 275.")], _ROWS
        )

        assert findings == []


class TestHeroSections:
    def test_headings_split_the_page_and_survive_blizzards_own_typos(self):
        html = (
            '<h4 class="PatchNotes-sectionTitle">Tank</h4>'
            '<h5 class="PatchNotesHeroUpdate-name">D.Mon</h5>'
            "<p>Armor reduced from 325 to 300.</p>"
            '<h5 class="PatchNotesHeroUpdate-name">wrecking Ball</h5>'
            "<p>Adaptive Shield duration reduced from 7 to 6 seconds.</p>"
        )

        sections = hero_sections(html)

        assert [hero for hero, _ in sections] == ["D.Mon", "wrecking Ball"]
        assert "Armor reduced from 325 to 300." in sections[0][1]
        assert "Armor" not in sections[1][1]


class TestRotationMapParsing:
    """The rates page is the only structured map list Blizzard publishes, and it
    is served only when query parameters are present — the bare URL is a shell.
    """

    # Trimmed from the live page; the class attribute before `label` is real and
    # broke my first regex.
    _SELECT = (
        '<select class="blz-dropdown" data-label="map" id="filter-map-select">'
        '<option data-rqs="1,0" data-title="all_maps" value="all-maps">All Maps</option>'
        '<optgroup class="blz-subheading-text-lg" label="Control">'
        '<option data-rqs="1,0" data-title="Busan" value="busan">Busan</option>'
        '<option data-rqs="1,0" data-title="Ilios" value="ilios">Ilios</option>'
        "</optgroup>"
        '<optgroup class="blz-subheading-text-lg" label="Hybrid">'
        '<option data-rqs="1,0" data-title="King\'s Row" value="kings-row">'
        "King's Row</option>"
        "</optgroup></select>"
    )

    def test_maps_are_grouped_by_gamemode(self):
        result = parse_rotation_maps(self._SELECT)

        assert result["busan"] == ("Busan", "control")
        assert result["ilios"] == ("Ilios", "control")
        assert result["kings-row"] == ("King's Row", "hybrid")

    def test_the_all_maps_sentinel_is_not_a_map(self):
        """It sits outside every optgroup, so it must not be collected."""
        result = parse_rotation_maps(self._SELECT)

        assert "all-maps" not in result

    def test_shell_page_yields_nothing_rather_than_guessing(self):
        """Without query parameters Blizzard serves a page with no dropdown at
        all. That must warn, not report every map as missing."""
        result = parse_rotation_maps("<html><body>no dropdown here</body></html>")

        assert result == {}


class TestRotationTierParsing:
    """A division Blizzard adds and the enum lacks is a ValueError on every
    profile ranked there, so this dropdown is the earliest warning available."""

    # Verbatim from the live page, including the grandmaster data-title.
    _SELECT = (
        '<select class="blz-dropdown" data-label="tier" id="filter-tier-select">'
        '<option class="blz-subheading-text-lg" data-title="all_tiers" '
        'selected="selected" value="All">All Tiers</option>'
        '<option class="blz-subheading-text-lg" data-title="bronze" '
        'value="Bronze">Bronze</option>'
        '<option class="blz-subheading-text-lg" '
        'data-title="grandmaster_and_champion" '
        'value="Grandmaster">Grandmaster</option>'
        "</select>"
    )

    def test_divisions_are_read_as_lowercase_keys(self):
        assert parse_rotation_tiers(self._SELECT) == {"bronze", "grandmaster"}

    def test_grandmaster_is_read_from_value_not_data_title(self):
        """data-title on that option is "grandmaster_and_champion" — a label for
        the fact that grandmaster includes champion, not a division. Reading it
        reported a Blizzard rank that does not exist."""
        assert "grandmaster_and_champion" not in parse_rotation_tiers(self._SELECT)

    def test_the_all_tiers_sentinel_is_not_a_division(self):
        assert "all" not in parse_rotation_tiers(self._SELECT)

    def test_shell_page_yields_nothing_rather_than_guessing(self):
        """Without query parameters Blizzard serves a page with no dropdown.
        That must warn, not report every division as retired."""
        assert parse_rotation_tiers("<html><body>no dropdown</body></html>") == set()

    def test_an_unknown_division_fails(self):
        """The whole reason this check exists: a rank we cannot construct is a
        ValueError on every profile that holds it."""
        drift.failures.clear()
        drift.warnings.clear()
        html = self._SELECT.replace(
            "</select>",
            '<option data-title="champion" value="Champion">Champion</option></select>',
        )

        drift.check_competitive_divisions(html)

        assert len(drift.failures) == 1
        assert "champion" in drift.failures[0]

    def test_a_division_missing_from_the_dropdown_only_warns(self):
        """Dropping one we still parse would break profiles rather than fix
        anything, so this direction must never fail the run."""
        drift.failures.clear()
        drift.warnings.clear()

        drift.check_competitive_divisions(self._SELECT)

        assert drift.failures == []
        assert len(drift.warnings) == 1

    def test_a_missing_dropdown_warns_rather_than_retiring_every_division(self):
        drift.failures.clear()
        drift.warnings.clear()

        drift.check_competitive_divisions("<html><body>no dropdown</body></html>")

        assert drift.failures == []
        assert len(drift.warnings) == 1

    def test_the_live_dropdown_matches_the_enum(self):
        """The check compares against CompetitiveDivisionFilter, so the fixture
        of what Blizzard serves today must be a subset of it — otherwise the
        daily run is red for a reason that has nothing to do with Blizzard."""
        blizzard_today = {
            "bronze",
            "silver",
            "gold",
            "platinum",
            "emerald",
            "diamond",
            "master",
            "grandmaster",
        }

        assert blizzard_today <= {d.value for d in CompetitiveDivisionFilter}


def _days_ago(days: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _api_response(snapshots: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"region": "europe", "snapshots": snapshots}
    return response


class TestHeroStatsRecording:
    """The daily snapshot job can stop writing and nothing notices: a failed
    region is a log line nobody reads. This queries the live API instead of the
    table, so it covers cron -> storage -> endpoint in one request.
    """

    @pytest.fixture(autouse=True)
    def _reset_findings(self):
        drift.failures.clear()
        drift.warnings.clear()

    # 1 day is the normal state: the job and this check both run at 05:00 UTC,
    # so today's reading often does not exist yet. 2 absorbs one failed run.
    @pytest.mark.parametrize("age", [0, 1, 2])
    def test_fresh_reading_passes(self, age: int):
        response = _api_response([{"taken_on": _days_ago(age), "stats": [{}]}])

        with patch("httpx2.get", return_value=response):
            check_hero_stats_recording()

        assert drift.failures == []
        assert drift.warnings == []

    def test_stale_reading_fails(self):
        response = _api_response([{"taken_on": _days_ago(5), "stats": [{}]}])

        with patch("httpx2.get", return_value=response):
            check_hero_stats_recording()

        assert len(drift.failures) == 1
        assert "has stopped" in drift.failures[0]

    def test_empty_history_warns_rather_than_fails(self):
        """Empty is the normal state until the first cron run after a fresh
        deployment, and there is nothing to backfill."""
        response = _api_response([])

        with patch("httpx2.get", return_value=response):
            check_hero_stats_recording()

        assert drift.failures == []
        assert len(drift.warnings) == 1

    def test_unreachable_api_warns_rather_than_fails(self):
        """The canary being unreachable is not recording having stopped."""
        with patch("httpx2.get", side_effect=httpx2.ConnectError("no route to host")):
            check_hero_stats_recording()

        assert drift.failures == []
        assert len(drift.warnings) == 1
